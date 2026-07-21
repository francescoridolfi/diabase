/* Workspace panel: tab switching, the orb toggle, and the mobile view
   strip. Open/closed state and the active tab persist per browser via
   localStorage — everyone keeps the arrangement they prefer. */

const LS_OPEN = "diabase.ws.open";
const LS_TAB = "diabase.ws.tab";
const MOBILE = window.matchMedia("(max-width: 980px)");

export function initWorkspace({ shellEl, workspaceEl, orbEl, mTabsEl, onSchemaShown }) {
  const tabs = [...workspaceEl.querySelectorAll(".tab")];
  const panes = [...workspaceEl.querySelectorAll(".pane")];
  let activePane = localStorage.getItem(LS_TAB) || "pane-schema";
  let mobileView = "chat"; // mobile only: which strip button is active

  function isOpen() {
    return shellEl.dataset.wsOpen === "1";
  }
  function schemaVisible() {
    if (MOBILE.matches) return mobileView === "pane-schema";
    return isOpen() && activePane === "pane-schema";
  }

  function selectTab(paneId) {
    if (!panes.some((p) => p.id === paneId)) return;
    activePane = paneId;
    localStorage.setItem(LS_TAB, paneId);
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.pane === paneId));
    panes.forEach((p) => (p.hidden = p.id !== paneId));
    if (paneId === "pane-schema") onSchemaShown();
  }

  function setOpen(open) {
    shellEl.dataset.wsOpen = open ? "1" : "0";
    orbEl.setAttribute("aria-pressed", String(open));
    localStorage.setItem(LS_OPEN, open ? "1" : "0");
    if (open && activePane === "pane-schema") onSchemaShown();
  }

  function toggle() {
    setOpen(!isOpen());
  }

  /* mobile: one view at a time — chat, or one workspace pane */
  function selectMobileView(view) {
    mobileView = view;
    [...mTabsEl.querySelectorAll(".mt")].forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    shellEl.dataset.mView = view === "chat" ? "chat" : "workspace";
    if (view !== "chat") selectTab(view);
  }

  tabs.forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.pane)));
  mTabsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".mt");
    if (b) selectMobileView(b.dataset.view);
  });

  orbEl.addEventListener("click", () => {
    if (MOBILE.matches) selectMobileView(mobileView === "chat" ? "pane-schema" : "chat");
    else toggle();
  });
  orbEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); orbEl.click(); }
  });

  MOBILE.addEventListener("change", () => {
    // leaving mobile: restore the desktop arrangement; entering: start on chat
    if (!MOBILE.matches) { shellEl.dataset.mView = "chat"; selectTab(activePane); }
    else selectMobileView("chat");
  });

  /* a tab may want attention while closed (e.g. a plan being applied):
     the runtime can ask for it explicitly */
  function reveal(paneId) {
    if (MOBILE.matches) return; // never yank the mobile user out of the chat
    if (!isOpen()) setOpen(true);
    selectTab(paneId);
  }

  // boot: restore persisted state
  selectTab(activePane);
  setOpen(localStorage.getItem(LS_OPEN) !== "0");
  shellEl.dataset.mView = "chat";

  return { toggle, selectTab, reveal, schemaVisible };
}
