/* Audit tab: a live "last 10" timeline that follows the stream, plus the
   full cursor-paginated log below it (loaded lazily, appended on demand). */

export function initTimeline({ liveEl, listEl, moreBtn, liveUrl, logUrl }) {
  let timer = null;
  let cursor = 0;
  let logLoaded = false;

  async function refresh() {
    const r = await fetch(liveUrl);
    if (r.ok) liveEl.innerHTML = await r.text();
  }
  /* debounced: tool events arrive in bursts — one fetch per burst */
  function refreshSoon() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 500);
  }

  async function loadPage(reset) {
    if (reset) { listEl.innerHTML = ""; cursor = 0; }
    const r = await fetch(logUrl + (cursor ? "?before=" + cursor : ""));
    if (!r.ok) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = await r.text();
    const page = tmp.querySelector(".audit-page");
    listEl.append(...page.children);
    cursor = +page.dataset.nextBefore;
    moreBtn.style.display = page.dataset.hasMore === "1" ? "" : "none";
  }

  /* first time the audit pane opens: pull page one of the full log */
  function ensureLogLoaded() {
    if (logLoaded) return;
    logLoaded = true;
    loadPage(true);
  }

  moreBtn.addEventListener("click", () => loadPage(false));

  return { refresh, refreshSoon, ensureLogLoaded };
}
