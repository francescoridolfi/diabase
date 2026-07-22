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
import { initFunctions } from "./functions.js";
import { initStorage } from "./storage.js";
import { initAuth } from "./auth.js";
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

const functions = (urls.serverCaps || []).includes("functions")
  ? initFunctions({ listEl: document.getElementById("fn-list"), urls, csrf })
  : null;

const storage = (urls.serverCaps || []).includes("storage")
  ? initStorage({ listEl: document.getElementById("bucket-list"), urls })
  : null;

const auth = (urls.serverCaps || []).includes("auth_config")
  ? initAuth({ paneEl: document.getElementById("auth-view"), urls })
  : null;

// each pane can lazy-load when it first becomes visible
const paneHooks = {
  "pane-schema": () => schema.shown(),
  "pane-audit": () => timeline.ensureLogLoaded(),
  "pane-functions": () => functions?.shown(),
  "pane-storage": () => storage?.shown(),
  "pane-auth": () => auth?.shown(),
};
const workspace = initWorkspace({
  shellEl: document.getElementById("room-shell"),
  workspaceEl: document.getElementById("workspace"),
  orbEl: document.getElementById("orb"),
  mTabsEl: document.getElementById("m-tabs"),
  onPaneShown: (paneId) => paneHooks[paneId]?.(),
});

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
      refreshPanes();
    },
    onPlanProposed: (planId) => plan.loadPlanCard(planId),
  },
});

const refreshPanes = () => {
  schema.refreshIfVisible(workspace.paneVisible("pane-schema"));
  functions?.refreshIfVisible(workspace.paneVisible("pane-functions"));
  storage?.refreshIfVisible(workspace.paneVisible("pane-storage"));
  auth?.refreshIfVisible(workspace.paneVisible("pane-auth"));
};

const plan = initPlan({
  log: document.getElementById("chatlog"),
  chat,
  orb,
  timeline,
  urls,
  csrf,
  onApplySettled: refreshPanes,
});

initContext({ chat, urls, csrf });
// the sidebar drawer is handled globally by shell.js

// sparks on both orbits: the workspace frame and the mini orb — the same
// swarm at two scales, both hurrying while the agent works
const workspaceEl = document.getElementById("workspace");
initParticles({
  el: workspaceEl,
  shape: "rect",
  isWorking: () => workspaceEl.classList.contains("working"),
});
const orbEl = document.getElementById("orb");
initParticles({
  el: orbEl,
  shape: "circle",
  count: 10,
  margin: 8,
  sizeRange: [0.5, 1.4],
  wobbleRange: [0.8, 2.5],
  idleSpeed: 14,
  isWorking: () => ["thinking", "tool", "write"].includes(orbEl.dataset.state),
});

// a turn already running when this page loaded (started before a refresh,
// or from another tab) — reconnect from the beginning: every event is
// persisted, so replaying from 0 rebuilds the tool chips and partial
// reply exactly as they'd look if we'd never left, then keeps streaming live
if (urls.activeTurnId) chat.streamTurn(urls.activeTurnId, 0);
// likewise a plan waiting for a decision (or mid-apply) is rebuilt from data
if (urls.activePlanId) plan.loadPlanCard(urls.activePlanId);
