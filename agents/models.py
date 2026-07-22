from django.db import models

from workspaces.models import Project

from . import crypto


class AgentConnection(models.Model):
    """A configured way to reach an LLM: family, model, endpoint, key.

    Configured once on the Connections page, then selected by projects.
    The API key is encrypted at rest (see agents.crypto) and is never
    echoed back in full nor written to the audit trail.
    """

    BACKENDS = [
        ("claude_code", "Claude Code (subscription)"),
        ("anthropic_api", "Anthropic API"),
        ("openai_compat", "OpenAI-compatible endpoint"),
    ]

    name = models.CharField(max_length=100, unique=True)
    backend = models.CharField(max_length=20, choices=BACKENDS)
    model = models.CharField(max_length=100, blank=True)
    base_url = models.CharField(max_length=300, blank=True)
    api_key_encrypted = models.TextField(blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_backend_display()})"

    @property
    def api_key(self) -> str:
        return crypto.decrypt(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str):
        self.api_key_encrypted = crypto.encrypt(value or "")

    @property
    def masked_key(self) -> str:
        return crypto.mask(self.api_key)


class Turn(models.Model):
    """Operational metadata for one agent execution.

    The per-tool detail lives in the audit trail; the Turn records what
    the trail doesn't: which backend/model ran, how long it took, how it
    ended. Fine-grained history is audit's job — Turn rows may be pruned.
    """

    STATUSES = [("running", "Running"), ("completed", "Completed"), ("failed", "Failed")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="turns")
    # deleting a chat deletes its turns (and their plans); audit rows survive
    conversation = models.ForeignKey(
        "workspaces.Conversation", null=True, on_delete=models.CASCADE, related_name="turns"
    )
    backend = models.CharField(max_length=30)
    model = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="running")
    error = models.TextField(blank=True)
    user_message = models.TextField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.backend} turn on {self.project} → {self.status}"


class Plan(models.Model):
    """A batch of write steps waiting for the user's decision.

    Created by the plan gate (see tools.BoundToolset) while a turn runs:
    at the "plan" autonomy level, write tools queue their calls here
    instead of executing. The turn that built it proposes it; the user
    approves, revises or rejects it from the room. Applying executes the
    steps through the same audited path a live tool call would take.
    """

    STATUSES = [
        ("draft", "Draft"),  # still being accumulated by a running turn
        ("proposed", "Proposed"),  # waiting for the user's decision
        ("applying", "Applying"),
        ("applied", "Applied"),
        ("failed", "Failed"),  # apply stopped on a step error
        ("rejected", "Rejected"),
        ("superseded", "Superseded"),  # user asked for a revision, or the turn died
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="plans")
    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="plans")
    status = models.CharField(max_length=10, choices=STATUSES, default="draft")
    decided_by = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    # the turn started automatically after apply, carrying the real step
    # results back to the agent (the "incremental apply" loop)
    continuation_turn = models.ForeignKey(
        Turn, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Plan #{self.pk} ({self.status}) on {self.project}"


class PlanStep(models.Model):
    """One queued tool call inside a Plan, in proposal order."""

    STATUSES = [
        ("pending", "Pending"),
        ("applied", "Applied"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),  # a previous step failed
    ]

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveIntegerField()
    tool = models.CharField(max_length=50)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="pending")
    output = models.JSONField(default=dict, blank=True)
    # derived review context computed at queue time, e.g. the unified diff
    # of a deploy_function step against the live source — what the user
    # actually decides on
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(fields=["plan", "order"], name="unique_step_order_per_plan")]

    def __str__(self):
        return f"{self.plan_id}#{self.order} {self.tool} ({self.status})"


class TurnEvent(models.Model):
    """One persisted runtime event, in emission order.

    This is what makes a turn survive a page refresh: the background
    worker (see runtime.start_turn) writes each event here the moment
    it's produced, and the SSE view streams from a cursor — reconnecting
    mid-turn just resumes from the last event id the client saw.
    """

    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return f"{self.turn_id}:{self.pk} {self.kind}"
