/* Context tab: system prompt preview, drag&drop uploads, and the
   source | preview editor modal (files + system prompt). */

import { md } from "./md.js";

const CFILE_MAX_BYTES = 100 * 1024;

export function initContext({ chat, urls, csrf }) {
  const veil = document.getElementById("veil");
  const overlay = document.getElementById("editor-overlay");
  const editorTitle = document.getElementById("editor-title");
  const editorMeta = document.getElementById("editor-meta");
  const editorSrc = document.getElementById("editor-src");
  const editorPreview = document.getElementById("editor-preview");
  const editorDelete = document.getElementById("editor-delete");
  const editorGutter = document.getElementById("editor-gutter");
  let editorMode = null; // {kind: "file", name} | {kind: "prompt"}

  function openPanel() {
    document.body.classList.add("overlay-open");
    overlay.classList.add("open");
  }
  function closePanels() {
    document.body.classList.remove("overlay-open");
    overlay.classList.remove("open");
  }
  veil.addEventListener("click", closePanels);
  overlay.querySelector("[data-close]").addEventListener("click", closePanels);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanels(); });

  function updateEditorPreview() {
    editorPreview.innerHTML = md(editorSrc.value) || '<p class="dim-note">Nothing to preview yet.</p>';
    const bytes = new Blob([editorSrc.value]).size;
    const lines = editorSrc.value ? editorSrc.value.split("\n").length : 1;
    editorMeta.textContent = `${lines} lines · ${bytes} B`;
    editorGutter.textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
    editorGutter.scrollTop = editorSrc.scrollTop;
  }
  editorSrc.addEventListener("input", updateEditorPreview);
  editorSrc.addEventListener("scroll", () => (editorGutter.scrollTop = editorSrc.scrollTop));

  function openEditor(mode, content) {
    editorMode = mode;
    editorTitle.textContent = mode.kind === "prompt" ? "System prompt" : mode.name;
    editorDelete.style.display = mode.kind === "file" ? "" : "none";
    editorSrc.value = content;
    updateEditorPreview();
    openPanel();
    editorSrc.focus();
    editorSrc.setSelectionRange(0, 0);
    editorSrc.scrollTop = 0;
    editorGutter.scrollTop = 0;
    editorPreview.scrollTop = 0;
  }

  document.querySelectorAll(".file-row[data-name]").forEach((row) =>
    row.addEventListener("click", async () => {
      const r = await fetch(`${urls.contextFileGetUrl}?name=${encodeURIComponent(row.dataset.name)}`);
      const data = await r.json();
      if (!r.ok) return;
      openEditor({ kind: "file", name: data.name }, data.content);
    })
  );

  const promptPreview = document.getElementById("prompt-preview");
  const sysPrompt = JSON.parse(document.getElementById("sysprompt-data").textContent);
  const openPromptEditor = () => openEditor({ kind: "prompt" }, sysPrompt);
  promptPreview.addEventListener("click", openPromptEditor);
  promptPreview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPromptEditor(); }
  });

  document.getElementById("editor-save").addEventListener("click", async () => {
    if (!editorMode) return;
    const body =
      editorMode.kind === "prompt"
        ? new URLSearchParams({ system_prompt: editorSrc.value, csrfmiddlewaretoken: csrf() })
        : new URLSearchParams({ name: editorMode.name, content: editorSrc.value, csrfmiddlewaretoken: csrf() });
    const url = editorMode.kind === "prompt" ? urls.projectUpdateUrl : urls.contextFileSaveUrl;
    await fetch(url, { method: "POST", body });
    window.location.reload();
  });
  editorDelete.addEventListener("click", async () => {
    if (!editorMode || editorMode.kind !== "file") return;
    if (!window.confirm(`Delete ${editorMode.name}?`)) return;
    await fetch(urls.contextFileDeleteUrl, {
      method: "POST",
      body: new URLSearchParams({ name: editorMode.name, csrfmiddlewaretoken: csrf() }),
    });
    window.location.reload();
  });

  /* ---------- drag & drop uploader ---------- */
  const dropzone = document.getElementById("dropzone");
  const dzInput = document.getElementById("dz-input");

  function readAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(file);
    });
  }

  async function uploadContextFiles(fileList) {
    for (const file of fileList) {
      if (file.size > CFILE_MAX_BYTES) {
        chat.add("msg assistant", `⚠ ${file.name} is ${Math.round(file.size / 1024)} KB — the limit is 100 KB.`, true);
        continue;
      }
      const content = await readAsText(file);
      const body = new URLSearchParams({ name: file.name, content, csrfmiddlewaretoken: csrf() });
      await fetch(urls.contextFileSaveUrl, { method: "POST", body });
    }
    window.location.reload(); // simplest way to reflect the new files list + audit trail
  }

  if (dropzone) {
    dropzone.addEventListener("click", () => dzInput.click());
    dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dzInput.click(); } });
    dzInput.addEventListener("change", () => { if (dzInput.files.length) uploadContextFiles(dzInput.files); });
    ["dragenter", "dragover"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag-over"); })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag-over"); })
    );
    dropzone.addEventListener("drop", (e) => {
      const files = e.dataTransfer?.files;
      if (files && files.length) uploadContextFiles(files);
    });
  }
}
