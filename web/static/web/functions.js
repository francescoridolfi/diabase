/* Functions tab: the deployed list, and a Monaco editor over the source
   Diabase tracks locally (see instances.EdgeFunctionSource).

   Source-of-truth model: deploys go out as bundles (dashboard/CLI
   compatible), the exact source stays on OUR side; every read serves the
   local copy. Drift with the live version (someone deployed outside
   Diabase) is detected via version numbers and shown, never hidden.
   The user's own deploys from here are audited with them as actor. */

import { esc } from "./md.js";

let monacoReady = null;

/* Monaco is vendored (no CDN — self-hosted product) and AMD-loaded
   lazily the first time the editor opens. */
function ensureMonaco(base) {
  if (monacoReady) return monacoReady;
  monacoReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `${base}/loader.js`;
    s.onload = () => {
      window.require.config({ paths: { vs: base } });
      window.require(["vs/editor/editor.main"], () => resolve(window.monaco), reject);
    };
    s.onerror = () => reject(new Error("Monaco failed to load"));
    document.head.appendChild(s);
  });
  return monacoReady;
}

export function initFunctions({ listEl, urls, csrf }) {
  const modal = document.getElementById("fn-editor");
  const title = document.getElementById("fn-editor-title");
  const meta = document.getElementById("fn-editor-meta");
  const drift = document.getElementById("fn-editor-drift");
  const status = document.getElementById("fn-editor-status");
  const deployBtn = document.getElementById("fn-editor-deploy");
  const host = document.getElementById("monaco-host");
  const openLink = document.getElementById("fn-editor-open");

  let editor = null;
  let current = null; // {slug, drift, tracked, verify_jwt}
  let loaded = false;

  async function ensureEditor() {
    const monaco = await ensureMonaco(urls.monacoBase);
    if (!editor) {
      host.innerHTML = "";
      editor = monaco.editor.create(host, {
        language: "typescript",
        theme: "vs-dark",
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 13,
        scrollBeyondLastLine: false,
        padding: { top: 10 },
      });
    }
    return editor;
  }

  async function openEditor(fn) {
    current = { slug: fn.slug, drift: fn.drift, tracked: fn.tracked, verify_jwt: true };
    title.textContent = fn.slug;
    meta.textContent = "loading…";
    status.textContent = "";
    drift.hidden = !fn.drift;
    deployBtn.disabled = true;
    if (urls.supabaseDashboard) openLink.href = `${urls.supabaseDashboard}/functions/${fn.slug}/details`;
    document.body.classList.add("overlay-open");
    modal.classList.add("open");

    const [ed, resp] = await Promise.all([
      ensureEditor(),
      fetch(`${urls.functionsUrl}${fn.slug}/source/`).then((r) => r.json()),
    ]);
    if (resp.error) {
      ed.setValue(`// ${resp.error}\n// This function was deployed outside Diabase as a bundle:\n// its source is not readable. Redeploy it through Diabase to track it.`);
      ed.updateOptions({ readOnly: true });
      meta.textContent = "source unavailable";
      return;
    }
    current.verify_jwt = resp.verify_jwt ?? true;
    current.tracked = resp.tracked;
    ed.updateOptions({ readOnly: false });
    ed.setValue(resp.body);
    meta.textContent = resp.tracked
      ? `tracked · v${resp.deployed_version ?? "?"}${resp.deployed_by ? " · by " + resp.deployed_by : ""}`
      : "untracked — deploying will start tracking it";
    deployBtn.disabled = false;
  }

  deployBtn.addEventListener("click", async () => {
    if (!current || !editor) return;
    const warning = current.drift
      ? "The live version was changed outside Diabase — your deploy will OVERWRITE it. Continue?"
      : `Deploy ${current.slug}? The change goes live immediately and is audited in your name.`;
    if (!window.confirm(warning)) return;
    deployBtn.disabled = true;
    status.textContent = "deploying…";
    try {
      const r = await fetch(`${urls.functionsUrl}${current.slug}/deploy/`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ body: editor.getValue(), verify_jwt: current.verify_jwt }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "deploy failed");
      status.textContent = `deployed · v${data.version ?? "?"}`;
      drift.hidden = true;
      current.drift = false;
      load(); // the list behind the modal refreshes
    } catch (e) {
      status.textContent = "";
      window.alert("Deploy failed: " + e.message);
    } finally {
      deployBtn.disabled = false;
    }
  });

  function badge(f) {
    if (!f.tracked) return '<span class="pill">untracked</span>';
    if (f.drift) return '<span class="pill warn" title="live version ≠ Diabase version">drift</span>';
    return '<span class="pill ok">tracked</span>';
  }

  function render(functions) {
    if (!functions.length) {
      listEl.innerHTML = '<p class="dim-note">No edge functions yet — ask the agent to create one.</p>';
      return;
    }
    listEl.innerHTML = functions
      .map(
        (f) => `
      <button type="button" class="file-row fn-row" data-fn='${esc(JSON.stringify(f))}'>
        <span aria-hidden="true">ƒ</span>
        <span class="fname">${esc(f.slug)}</span>
        <span class="pill ${f.status === "ACTIVE" ? "ok" : "warn"}">${esc(String(f.status || "?"))}</span>
        <span class="muted">v${esc(String(f.version ?? "?"))}</span>
        ${badge(f)}
        <span class="muted">${f.verify_jwt ? "JWT" : "public"}</span>
      </button>`
      )
      .join("");
    listEl.querySelectorAll(".fn-row").forEach((row) =>
      row.addEventListener("click", () => openEditor(JSON.parse(row.dataset.fn)))
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
