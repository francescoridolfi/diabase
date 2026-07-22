/* Project room entry point: wires the modules together.
   Each module owns one concern (md rendering, orb state, chat/stream,
   plan card, audit timeline, schema canvas, context editor, workspace
   tabs); this file only builds the dependency graph. */

import { initOrb } from "./orb.js";
import { initWorkspace } from "./workspace.js";
import { initSchema } from "./schema.js";
import { initTimeline } from "./timeline.js";
import { initChat } from "./chat.js";
import { initPlan } from "./plan.js";
import { initContext } from "./context.js";
import { initParticles } from "./particles.js";

const urls = window.DIABASE;
const csrf = () => urls.csrfToken || document.querySelector("[name=csrfmiddlewaretoken]").value;

const orb = initOrb({
  orbEl: document.getElementById("orb"),
  labelEl: document.getElementById("orb-label"),
  workspaceEl: document.getElementById("workspace"),
});

const schema = initSchema({
  viewport: document.getElementById("schema-viewport"),
  canvas: document.getElementById("schema-canvas"),
  grid: document.getElementById("schema-grid"),
  url: urls.schemaUrl,
});

const timeline = initTimeline({
  liveEl: document.getElementById("timeline"),
  listEl: document.getElementById("audit-log-list"),
  moreBtn: document.getElementById("audit-more"),
  liveUrl: urls.auditUrl,
  logUrl: urls.auditLogUrl,
});

const workspace = initWorkspace({
  shellEl: document.getElementById("room-shell"),
  workspaceEl: document.getElementById("workspace"),
  orbEl: document.getElementById("orb"),
  mTabsEl: document.getElementById("m-tabs"),
  onSchemaShown: () => schema.shown(),
});

// the audit pane loads its full log the first time it opens
document.querySelector('[data-pane="pane-audit"]').addEventListener("click", () => timeline.ensureLogLoaded());
document.querySelector('[data-view="pane-audit"]').addEventListener("click", () => timeline.ensureLogLoaded());

const chat = initChat({
  log: document.getElementById("chatlog"),
  form: document.getElementById("chatform"),
  msgEl: document.getElementById("msg"),
  orb,
  urls,
  csrf,
  hooks: {
    onToolEvent: () => timeline.refreshSoon(),
    onTurnSettled: () => {
      timeline.refresh();
      schema.refreshIfVisible(workspace.schemaVisible());
    },
    onPlanProposed: (planId) => plan.loadPlanCard(planId),
  },
});

const plan = initPlan({ log: document.getElementById("chatlog"), chat, orb, timeline, urls, csrf });

initContext({ chat, urls, csrf });
// the sidebar drawer is handled globally by shell.js

initParticles({ workspaceEl: document.getElementById("workspace") });

// a turn already running when this page loaded (started before a refresh,
// or from another tab) — reconnect from the beginning: every event is
// persisted, so replaying from 0 rebuilds the tool chips and partial
// reply exactly as they'd look if we'd never left, then keeps streaming live
if (urls.activeTurnId) chat.streamTurn(urls.activeTurnId, 0);
// likewise a plan waiting for a decision (or mid-apply) is rebuilt from data
if (urls.activePlanId) plan.loadPlanCard(urls.activePlanId);
