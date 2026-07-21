/* Plan card: propose → approve / revise / reject → apply progress.
   Apply runs server-side on a background thread: the card polls the plan
   until it settles, then attaches to the continuation turn the runtime
   started (the incremental-apply loop). */

import { esc } from "./md.js";

const STEP_ICONS = { pending: "○", applied: "✓", failed: "✕", skipped: "⤼" };
const PLAN_LABELS = {
  proposed: "waiting for your decision",
  applying: "applying…",
  applied: "applied",
  failed: "apply failed",
  rejected: "rejected",
  superseded: "superseded",
};

export function initPlan({ log, chat, orb, timeline, urls, csrf }) {
  function planCardEl(planId) {
    let el = document.getElementById("plan-" + planId);
    if (!el) {
      el = document.createElement("div");
      el.id = "plan-" + planId;
      el.className = "plan-card";
      log.appendChild(el);
    }
    return el;
  }

  function renderPlanCard(plan) {
    const stick = chat.nearBottom();
    const el = planCardEl(plan.id);
    el.dataset.status = plan.status;
    const steps = plan.steps
      .map((s) => {
        const sql = s.payload && s.payload.sql ? `<pre><code>${esc(s.payload.sql)}</code></pre>` : "";
        const err = s.output && s.output.error ? `<div class="step-err">${esc(String(s.output.error))}</div>` : "";
        return `<div class="plan-step" data-status="${s.status}">
          <span class="step-mark">${STEP_ICONS[s.status] || "○"}</span>
          <div class="step-body"><span class="step-title">Step ${s.order} · ${esc(s.tool)}</span>${sql}${err}</div>
        </div>`;
      })
      .join("");
    const decidable = plan.status === "proposed";
    el.innerHTML = `
      <div class="plan-head">
        <span class="plan-title">Plan #${plan.id}</span>
        <span class="pill ${plan.status === "applied" ? "ok" : plan.status === "failed" || plan.status === "rejected" ? "err" : "warn"}">${PLAN_LABELS[plan.status] || plan.status}</span>
      </div>
      <div class="plan-steps">${steps}</div>
      ${plan.error ? `<div class="step-err">${esc(plan.error)}</div>` : ""}
      ${decidable ? `
      <div class="plan-actions">
        <button class="plan-approve">Approve &amp; apply</button>
        <button class="ghost plan-revise">Revise</button>
        <button class="ghost plan-reject" style="color:var(--danger)">Reject</button>
      </div>
      <form class="plan-revise-form" hidden>
        <textarea rows="2" placeholder="What should change?"></textarea>
        <button>Send revision</button>
      </form>` : ""}`;
    chat.followIf(stick);
    if (decidable) wirePlanActions(el, plan.id);
    return el;
  }

  async function planFetch(planId, action, body) {
    const resp = await fetch(`${urls.planBase}${planId}/${action}`, {
      method: action ? "POST" : "GET",
      headers: action ? { "Content-Type": "application/json", "X-CSRFToken": csrf() } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "plan request failed");
    return data;
  }

  function wirePlanActions(el, planId) {
    el.querySelector(".plan-approve").addEventListener("click", async () => {
      try {
        const plan = await planFetch(planId, "approve/");
        renderPlanCard(plan);
        orb.set("write", "applying plan…");
        pollPlanApply(planId);
      } catch (err) { chat.add("msg assistant", "⚠ " + esc(err.message)); }
    });
    el.querySelector(".plan-reject").addEventListener("click", async () => {
      try {
        const plan = await planFetch(planId, "reject/");
        renderPlanCard(plan);
        chat.add("msg plan-note", `Plan #${planId} rejected — nothing was executed.`, true);
        timeline.refresh();
      } catch (err) { chat.add("msg assistant", "⚠ " + esc(err.message)); }
    });
    const reviseForm = el.querySelector(".plan-revise-form");
    el.querySelector(".plan-revise").addEventListener("click", () => {
      reviseForm.hidden = !reviseForm.hidden;
      if (!reviseForm.hidden) reviseForm.querySelector("textarea").focus();
    });
    reviseForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const comment = reviseForm.querySelector("textarea").value.trim();
      if (!comment) return;
      try {
        const data = await planFetch(planId, "revise/", { comment });
        renderPlanCard(data);
        chat.add("msg user", comment, true);
        chat.streamTurn(data.turn_id, 0);
      } catch (err) { chat.add("msg assistant", "⚠ " + esc(err.message)); }
    });
  }

  async function pollPlanApply(planId) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 600));
      let plan;
      try { plan = await planFetch(planId, ""); } catch { continue; }
      renderPlanCard(plan);
      timeline.refreshSoon(); // each applied step is already in the trail
      if (plan.status === "applying") continue;
      timeline.refresh();
      if (plan.continuation_turn_id) chat.streamTurn(plan.continuation_turn_id, 0);
      else orb.set("idle", "");
      return;
    }
  }

  async function loadPlanCard(planId) {
    try {
      const plan = await planFetch(planId, "");
      renderPlanCard(plan);
      if (plan.status === "applying") { orb.set("write", "applying plan…"); pollPlanApply(planId); }
    } catch { /* plan may be gone; nothing to render */ }
  }

  return { loadPlanCard };
}
