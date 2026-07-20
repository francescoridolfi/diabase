"""GUI views: server-rendered shell, SSE turn streaming, HTMX partials."""

import json
import time

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

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


def _agent_status():
    try:
        backend = get_backend()
        return {"backend": backend.name, "ok": True, "reason": ""}
    except (RuntimeError, ValueError) as e:
        return {"backend": "", "ok": False, "reason": str(e)}


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
    return render(
        request,
        "web/project.html",
        {
            "project": project,
            "chat": project.messages.all(),
            "audit_entries": project.audit_entries.all()[:10],
            "context_files": project.context_files.all(),
            "autonomy_choices": Project.AUTONOMY_LEVELS,
            "agent": _agent_status(),
            "backends": sorted(BACKENDS),
            "active_turn_id": active_turn.pk if active_turn else None,
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
