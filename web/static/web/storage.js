/* Storage tab: the bucket list — structure and access rules, never files.
   Blobs are the dashboard's job: every row links out to the bucket in
   Supabase; Diabase shows what governs it (public flag, size limit, MIME
   whitelist, object count) and the agent manages it through plans. */

import { esc } from "./md.js";

function humanBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i++;
  }
  return `${x >= 10 || i === 0 ? Math.round(x) : x.toFixed(1)} ${units[i]}`;
}

function mimeSummary(raw) {
  if (!raw) return "";
  // the read-only query stringifies postgres arrays: {image/png,image/jpeg}
  const types = String(raw).replace(/^\{|\}$/g, "").split(",").filter(Boolean);
  if (!types.length) return "";
  return types.length > 2 ? `${types.slice(0, 2).join(", ")} +${types.length - 2}` : types.join(", ");
}

export function initStorage({ listEl, urls }) {
  let loaded = false;

  function render(buckets) {
    if (!buckets.length) {
      listEl.innerHTML = '<p class="dim-note">No buckets yet — ask the agent to create one.</p>';
      return;
    }
    const dash = urls.supabaseDashboard;
    listEl.innerHTML = buckets
      .map((b) => {
        const inner = `
        <span aria-hidden="true">▦</span>
        <span class="fname">${esc(b.name || b.id)}</span>
        <span class="pill ${b.public ? "warn" : "ok"}">${b.public ? "public" : "private"}</span>
        ${b.objects != null ? `<span class="muted">${esc(String(b.objects))} obj</span>` : ""}
        ${b.file_size_limit ? `<span class="muted">≤ ${esc(humanBytes(b.file_size_limit))}</span>` : ""}
        ${mimeSummary(b.allowed_mime_types) ? `<span class="muted">${esc(mimeSummary(b.allowed_mime_types))}</span>` : ""}`;
        return dash
          ? `<a class="file-row" href="${esc(dash)}/storage/buckets/${encodeURIComponent(b.id)}"
               target="_blank" rel="noopener" title="Open bucket in Supabase ↗">${inner}</a>`
          : `<div class="file-row" style="cursor:default">${inner}</div>`;
      })
      .join("");
  }

  async function load() {
    try {
      const r = await fetch(urls.storageUrl);
      const data = await r.json();
      if (data.error) listEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(data.error)}</p>`;
      else render(data.buckets);
    } catch (e) {
      listEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(e.message)}</p>`;
    }
  }

  function shown() {
    loaded = true;
    load();
  }
  function refreshIfVisible(visible) {
    if (loaded && visible) load();
  }

  return { shown, refreshIfVisible };
}
