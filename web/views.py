"""GUI views: server-rendered shell, SSE turn streaming, HTMX partials."""

import json
import time

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from agents.models import AgentConnection
from agents.runtime import BACKENDS, get_backend, start_turn
from audit.services import record
from instances.adapters import get_adapter
from instances.models import Server
from workspaces.models import Project

# how often the SSE view polls for new persisted events, and how many
# consecutive empty polls it tolerates after the turn stops running
# before closing the stream (covers the race between a Turn's status
# flipping and its final TurnEvent landing in the same worker iteration)
STREAM_POLL_SECONDS = 0.15
STREAM_IDLE_POLLS_BEFORE_CLOSE = 3
STREAM_MAX_SECONDS = 20 * 60


def _agent_status(project=None):
    try:
        backend = get_backend(project=project)
        ok, reason = backend.availability()
        return {"backend": backend.name, "model": getattr(backend, "model", ""), "ok": ok, "reason": reason}
    except (RuntimeError, ValueError) as e:
        return {"backend": "", "model": "", "ok": False, "reason": str(e)}


def home(request):
    return render(
        request,
        "web/home.html",
        {
            "servers": Server.objects.prefetch_related("projects").order_by("-created_at"),
            "projects": Project.objects.select_related("server").order_by("-created_at"),
            "adapter_choices": Server.ADAPTER_CHOICES,
            "agent": _agent_status(),
        },
    )


@require_POST
def server_create(request):
    name = request.POST.get("name", "").strip()
    adapter_type = request.POST.get("adapter_type", "sqlite")
    dsn = request.POST.get("dsn", "").strip()
    if name and dsn:
        server = Server.objects.create(name=name, adapter_type=adapter_type, dsn=dsn)
        project = Project.objects.create(name=name, server=server)
        record(
            action="server.connected",
            actor_type="user",
            project=project,
            payload_in={"name": name, "adapter_type": adapter_type},
        )
        return redirect("project_room", pk=project.pk)
    return redirect("home")


@require_POST
def project_create(request):
    name = request.POST.get("name", "").strip()
    server = get_object_or_404(Server, pk=request.POST.get("server_id"))
    if name:
        project = Project.objects.create(name=name, server=server)
        record(action="project.created", actor_type="user", project=project, payload_in={"name": name})
        return redirect("project_room", pk=project.pk)
    return redirect("home")


def project_room(request, pk):
    project = get_object_or_404(Project.objects.select_related("server"), pk=pk)
    active_turn = project.turns.filter(status="running").order_by("-started_at").first()
    # a plan awaiting a decision (or being applied) must survive a refresh
    active_plan = project.plans.filter(status__in=["proposed", "applying"]).first()
    return render(
        request,
        "web/project.html",
        {
            "project": project,
            "chat": project.messages.all(),
            "audit_entries": project.audit_entries.all()[:10],
            "context_files": project.context_files.all(),
            "autonomy_choices": Project.AUTONOMY_LEVELS,
            "connections": AgentConnection.objects.all(),
            "agent": _agent_status(project),
            "backends": sorted(BACKENDS),
            "active_turn_id": active_turn.pk if active_turn else None,
            "active_plan_id": active_plan.pk if active_plan else None,
        },
    )


def schema_json(request, pk):
    project = get_object_or_404(Project.objects.select_related("server"), pk=pk)
    server = project.server
    try:
        schema = get_adapter(server.adapter_type, server.dsn).get_schema()
        return JsonResponse({"schema": schema})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=502)


def audit_partial(request, pk):
    project = get_object_or_404(Project, pk=pk)
    return render(request, "web/_audit_timeline.html", {"audit_entries": project.audit_entries.all()[:10]})


AUDIT_PAGE_SIZE = 50


def audit_log(request, pk):
    """One page of the full audit log, cursor-based (append-only friendly)."""
    project = get_object_or_404(Project, pk=pk)
    entries = project.audit_entries.all()
    try:
        before = int(request.GET.get("before", 0))
    except ValueError:
        before = 0
    if before:
        entries = entries.filter(pk__lt=before)
    page = list(entries[: AUDIT_PAGE_SIZE + 1])
    has_more = len(page) > AUDIT_PAGE_SIZE
    page = page[:AUDIT_PAGE_SIZE]
    return render(
        request,
        "web/_audit_log_page.html",
        {
            "entries": page,
            "has_more": has_more,
            "next_before": page[-1].pk if page else 0,
        },
    )


def _actor(request) -> str:
    return getattr(request.user, "username", "") or ""


def connections(request):
    return render(
        request,
        "web/connections.html",
        {
            "connections": AgentConnection.objects.all(),
            "backend_choices": AgentConnection.BACKENDS,
            "agent": _agent_status(),
        },
    )


@require_POST
def connection_create(request):
    name = request.POST.get("name", "").strip()
    backend = request.POST.get("backend", "")
    if not name or backend not in dict(AgentConnection.BACKENDS):
        return redirect("connections")
    conn = AgentConnection(
        name=name,
        backend=backend,
        model=request.POST.get("model", "").strip(),
        base_url=request.POST.get("base_url", "").strip(),
    )
    conn.api_key = request.POST.get("api_key", "").strip()  # encrypted by the setter
    conn.save()
    record(
        action="connection.created",
        actor_type="user",
        actor=_actor(request),
        # the key itself NEVER reaches the audit trail — only whether one was set
        payload_in={
            "name": conn.name,
            "backend": conn.backend,
            "model": conn.model,
            "base_url": conn.base_url,
            "api_key_set": bool(conn.api_key_encrypted),
        },
    )
    return redirect("connections")


@require_POST
def connection_delete(request, pk):
    conn = get_object_or_404(AgentConnection, pk=pk)
    record(
        action="connection.deleted",
        actor_type="user",
        actor=_actor(request),
        payload_in={"name": conn.name, "backend": conn.backend},
    )
    conn.delete()
    return redirect("connections")


@require_POST
def project_update(request, pk):
    from audit.services import record
    from workspaces.services import set_system_prompt

    project = get_object_or_404(Project, pk=pk)
    prompt = request.POST.get("system_prompt", "")
    if prompt != project.system_prompt:
        set_system_prompt(project, prompt, user=_actor(request))
    level = request.POST.get("autonomy_level", "")
    if level in dict(Project.AUTONOMY_LEVELS) and level != project.autonomy_level:
        project.autonomy_level = level
        project.save(update_fields=["autonomy_level"])
        record(
            action="project.autonomy_updated",
            actor_type="user",
            actor=_actor(request),
            project=project,
            payload_in={"autonomy_level": level},
        )

    if "agent_connection" in request.POST:
        raw = request.POST.get("agent_connection", "")
        conn = AgentConnection.objects.filter(pk=raw).first() if raw else None
        if (conn.pk if conn else None) != project.agent_connection_id:
            project.agent_connection = conn
            project.save(update_fields=["agent_connection"])
            record(
                action="project.agent_updated",
                actor_type="user",
                actor=_actor(request),
                project=project,
                payload_in={"connection": conn.name if conn else "auto"},
            )
    return redirect("project_room", pk=pk)


@require_POST
def context_file_save(request, pk):
    from workspaces.services import ContextFileTooLarge, save_context_file

    project = get_object_or_404(Project, pk=pk)
    name = request.POST.get("name", "").strip()
    content = request.POST.get("content", "")
    if name:
        try:
            save_context_file(project, name, content, user=_actor(request))
        except ContextFileTooLarge:
            pass  # v1: silently rejected; surfaced properly with form errors later
    return redirect("project_room", pk=pk)


def context_file_json(request, pk):
    """One context file's full content, for the editor modal."""
    project = get_object_or_404(Project, pk=pk)
    file = project.context_files.filter(name=request.GET.get("name", "")).first()
    if file is None:
        return JsonResponse({"error": "No such file"}, status=404)
    return JsonResponse({"name": file.name, "content": file.content, "size": file.size})


@require_POST
def context_file_delete(request, pk):
    from workspaces.services import delete_context_file

    project = get_object_or_404(Project, pk=pk)
    name = request.POST.get("name", "").strip()
    if name and project.context_files.filter(name=name).exists():
        delete_context_file(project, name, user=_actor(request))
    return redirect("project_room", pk=pk)


@require_POST
def turn_start(request, pk):
    """Kick off one agent turn in the background; return its id immediately.

    The turn's lifetime is no longer tied to this request — the client
    subscribes to /turns/<id>/stream/ separately, and can reconnect to
    it (e.g. after a page refresh) at any point while it's running.
    """
    project = get_object_or_404(Project.objects.select_related("server"), pk=pk)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "Empty message"}, status=400)
    turn = start_turn(project, message, user=_actor(request))
    return JsonResponse({"turn_id": turn.pk})


def _plan_payload(plan) -> dict:
    return {
        "id": plan.pk,
        "status": plan.status,
        "error": plan.error,
        "continuation_turn_id": plan.continuation_turn_id,
        "steps": [
            {"order": s.order, "tool": s.tool, "payload": s.payload, "status": s.status, "output": s.output}
            for s in plan.steps.all()
        ],
    }


def plan_json(request, pk, plan_id):
    """One plan with its steps — the room's plan card renders from this,
    both on first load and while polling during an apply."""
    from agents.models import Plan

    project = get_object_or_404(Project, pk=pk)
    plan = get_object_or_404(Plan, pk=plan_id, project=project)
    return JsonResponse(_plan_payload(plan))


def _decidable_plan(pk, plan_id):
    from agents.models import Plan

    project = get_object_or_404(Project, pk=pk)
    return get_object_or_404(Plan, pk=plan_id, project=project)


@require_POST
def plan_approve(request, pk, plan_id):
    from agents.runtime import approve_plan

    plan = _decidable_plan(pk, plan_id)
    if plan.status != "proposed":
        return JsonResponse({"error": f"Plan is {plan.status}, not proposed"}, status=409)
    approve_plan(plan, user=_actor(request))
    return JsonResponse(_plan_payload(plan))


@require_POST
def plan_reject(request, pk, plan_id):
    from agents.runtime import reject_plan

    plan = _decidable_plan(pk, plan_id)
    if plan.status != "proposed":
        return JsonResponse({"error": f"Plan is {plan.status}, not proposed"}, status=409)
    reject_plan(plan, user=_actor(request))
    return JsonResponse(_plan_payload(plan))


@require_POST
def plan_revise(request, pk, plan_id):
    from agents.runtime import revise_plan

    plan = _decidable_plan(pk, plan_id)
    if plan.status != "proposed":
        return JsonResponse({"error": f"Plan is {plan.status}, not proposed"}, status=409)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    comment = (body.get("comment") or "").strip()
    if not comment:
        return JsonResponse({"error": "Empty revision comment"}, status=400)
    turn = revise_plan(plan, comment, user=_actor(request))
    return JsonResponse({**_plan_payload(plan), "turn_id": turn.pk})


def turn_stream(request, pk, turn_id):
    """SSE feed of a turn's persisted events, from an optional cursor.

    Polls TurnEvent instead of pushing, so there's no in-process state
    to lose: any number of clients can attach or reattach to the same
    turn and each gets exactly the events after the cursor it supplies.
    """
    from agents.models import Turn
    from agents.models import TurnEvent as TurnEventRow

    project = get_object_or_404(Project, pk=pk)
    turn = get_object_or_404(Turn, pk=turn_id, project=project)
    try:
        after = int(request.GET.get("after", 0))
    except ValueError:
        after = 0

    def stream():
        cursor = after
        idle_polls = 0
        elapsed = 0.0
        while True:
            events = list(TurnEventRow.objects.filter(turn_id=turn.pk, pk__gt=cursor).order_by("pk"))
            if events:
                idle_polls = 0
                for ev in events:
                    cursor = ev.pk
                    yield f"data: {json.dumps({'event': ev.kind, **ev.data}, default=str)}\n\n"
                continue  # more may already be waiting — check again before sleeping
            turn.refresh_from_db(fields=["status"])
            if turn.status != "running":
                idle_polls += 1
                if idle_polls >= STREAM_IDLE_POLLS_BEFORE_CLOSE:
                    return
            if elapsed >= STREAM_MAX_SECONDS:
                if turn.status == "running":
                    # its worker is gone (crash, deploy, server restart) and
                    # nothing else will ever mark this turn finished — do it
                    # here so it doesn't stay "running" forever
                    turn.status = "failed"
                    turn.error = "Turn timed out: its worker stopped responding (server restart?)."
                    turn.finished_at = timezone.now()
                    turn.save(update_fields=["status", "error", "finished_at"])
                    yield f"data: {json.dumps({'event': 'TurnFailed', 'error': turn.error})}\n\n"
                return
            time.sleep(STREAM_POLL_SECONDS)
            elapsed += STREAM_POLL_SECONDS

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
