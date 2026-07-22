/* Functions tab: what's deployed, readable on the spot.

   Diabase is where you REVIEW; deploys happen through agent plans and
   manual editing lives in the Supabase dashboard — so this tab is a
   list plus a read-only source viewer, nothing more. */

import { esc } from "./md.js";

export function initFunctions({ listEl, urls }) {
  const viewer = document.getElementById("fn-viewer");
  let loaded = false;

  function openViewer(slug) {
    const title = document.getElementById("fn-viewer-title");
    const meta = document.getElementById("fn-viewer-meta");
    const body = document.getElementById("fn-viewer-body");
    const open = document.getElementById("fn-viewer-open");
    title.textContent = slug;
    meta.textContent = "loading…";
    body.textContent = "";
    if (urls.supabaseDashboard) open.href = `${urls.supabaseDashboard}/functions/${slug}/details`;
    document.body.classList.add("overlay-open");
    viewer.classList.add("open");
    fetch(`${urls.functionsUrl}${slug}/body/`)
      .then((r) => r.json())
      .then((data) => {
        if (data.error) {
          meta.textContent = "";
          body.textContent = data.error;
          return;
        }
        const lines = data.body.split("\n").length;
        meta.textContent = `${lines} lines · read-only`;
        body.textContent = data.body;
      })
      .catch((e) => (body.textContent = "Network error: " + e.message));
  }

  function render(functions) {
    if (!functions.length) {
      listEl.innerHTML = '<p class="dim-note">No edge functions yet — ask the agent to create one.</p>';
      return;
    }
    listEl.innerHTML = functions
      .map(
        (f) => `
      <button type="button" class="file-row fn-row" data-slug="${esc(f.slug)}">
        <span aria-hidden="true">ƒ</span>
        <span class="fname">${esc(f.slug)}</span>
        <span class="pill ${f.status === "ACTIVE" ? "ok" : "warn"}">${esc(String(f.status || "?"))}</span>
        <span class="muted">v${esc(String(f.version ?? "?"))}</span>
        <span class="muted">${f.verify_jwt ? "JWT" : "public"}</span>
      </button>`
      )
      .join("");
    listEl.querySelectorAll(".fn-row").forEach((row) =>
      row.addEventListener("click", () => openViewer(row.dataset.slug))
    );
  }

  async function load() {
    try {
      const r = await fetch(urls.functionsUrl);
      const data = await r.json();
      if (data.error) listEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(data.error)}</p>`;
      else render(data.functions);
    } catch (e) {
      listEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(e.message)}</p>`;
    }
  }

  /* every open reloads (the list is one cheap call and deploys happen
     behind your back by design); a finished turn or an applied plan
     refreshes in place while the pane is visible */
  function shown() {
    loaded = true;
    load();
  }
  function refreshIfVisible(visible) {
    if (loaded && visible) load();
  }

  return { shown, refreshIfVisible };
}
