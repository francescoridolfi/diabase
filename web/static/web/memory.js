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

  function renderStats(stats) {
    const total = Object.values(stats).reduce((a, b) => a + b, 0);
    statsEl.textContent =
      `${total} chunks — ` +
      Object.entries(stats)
        .map(([s, n]) => `${s} ${n}`)
        .join(" · ");
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
      renderStats(data.stats);
      renderResults(data.results ?? null);
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
    reindexBtn.disabled = true;
    reindexBtn.textContent = "Reindexing…";
    try {
      const r = await fetch(urls.memoryReindexUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrf() },
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "reindex failed");
      await load(input.value.trim());
      if (data.errors) window.alert("Reindexed with warnings: " + JSON.stringify(data.errors));
    } catch (e) {
      window.alert("Reindex failed: " + e.message);
    } finally {
      reindexBtn.disabled = false;
      reindexBtn.textContent = "Reindex";
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
