/* Memory tab: a transparency window onto the recall() index — search it
   exactly as the agent does, see how much of each source is indexed,
   rebuild on demand. */

import { esc } from "./md.js";

const SOURCE_ICONS = { schema: "⛁", audit: "◉", chat: "💬", context: "▤" };

export function initMemory({ paneEl, urls, csrf }) {
  let loaded = false;

  paneEl.innerHTML = `
    <div class="mem-bar">
      <input type="search" id="mem-q" placeholder="Search the project's memory — tables, decisions, chats, docs…"
             autocomplete="off" aria-label="Search memory">
    </div>
    <div id="mem-results" class="stack" style="margin-top:0.7rem"></div>
    <div class="row-actions" style="margin-top:1rem">
      <span class="muted" id="mem-stats"></span>
      <button type="button" class="ghost" id="mem-reindex">Reindex</button>
    </div>`;

  const input = paneEl.querySelector("#mem-q");
  const resultsEl = paneEl.querySelector("#mem-results");
  const statsEl = paneEl.querySelector("#mem-stats");
  const reindexBtn = paneEl.querySelector("#mem-reindex");

  let poller = null;

  function renderStats(stats, indexing) {
    const total = Object.values(stats).reduce((a, b) => a + b, 0);
    statsEl.textContent =
      (indexing ? "indexing… " : "") +
      `${total} chunks — ` +
      Object.entries(stats)
        .map(([s, n]) => `${s} ${n}`)
        .join(" · ");
  }

  /* while a background reindex runs, the stats line ticks along with it */
  function setPolling(active) {
    reindexBtn.disabled = active;
    reindexBtn.textContent = active ? "Reindexing…" : "Reindex";
    if (active && !poller) poller = setInterval(() => load(input.value.trim()), 1200);
    if (!active && poller) {
      clearInterval(poller);
      poller = null;
    }
  }

  function renderResults(results) {
    if (!results) {
      resultsEl.innerHTML = '<p class="dim-note">Type to search what the agent can recall.</p>';
      return;
    }
    if (!results.length) {
      resultsEl.innerHTML = '<p class="dim-note">No matches.</p>';
      return;
    }
    resultsEl.innerHTML = results
      .map(
        (r) => `
      <div class="mem-hit">
        <div class="mem-head">
          <span aria-hidden="true">${SOURCE_ICONS[r.source] || "•"}</span>
          <span class="fname">${esc(r.title || r.ref)}</span>
          <span class="pill">${esc(r.source)}</span>
          <span class="muted">${esc(r.ref)}</span>
        </div>
        <pre class="mem-snippet">${esc(r.snippet)}</pre>
      </div>`
      )
      .join("");
  }

  async function load(query) {
    const url = query ? `${urls.memoryUrl}?q=${encodeURIComponent(query)}` : urls.memoryUrl;
    try {
      const data = await fetch(url).then((r) => r.json());
      renderStats(data.stats, data.indexing);
      renderResults(data.results ?? null);
      setPolling(Boolean(data.indexing));
    } catch (e) {
      resultsEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(e.message)}</p>`;
    }
  }

  let debounce = null;
  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => load(input.value.trim()), 250);
  });

  reindexBtn.addEventListener("click", async () => {
    setPolling(true);
    try {
      const r = await fetch(urls.memoryReindexUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrf() },
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "reindex failed");
      // started (or already running): either way the poller tracks it
    } catch (e) {
      setPolling(false);
      window.alert("Reindex failed: " + e.message);
    }
  });

  function shown() {
    loaded = true;
    load(input.value.trim());
  }
  function refreshIfVisible(visible) {
    if (loaded && visible) load(input.value.trim());
  }

  return { shown, refreshIfVisible };
}
