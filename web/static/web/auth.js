/* Auth tab: a read-only view over the instance's GoTrue configuration —
   what governs sign-in, not who signed up (users are data, they live in
   the dashboard). Secrets arrive masked from the server ("***set***"):
   this module never sees a credential. Email templates preview in a
   sandboxed iframe (srcdoc, no scripts) — NOT the markdown mini-renderer,
   these are arbitrary HTML documents. Edits go through agent plans. */

import { esc } from "./md.js";

/* the six standard GoTrue mail flows, in dashboard order */
const TEMPLATES = [
  ["confirmation", "Confirm sign-up"],
  ["invite", "Invite user"],
  ["magic_link", "Magic link"],
  ["recovery", "Reset password"],
  ["email_change", "Change email address"],
  ["reauthentication", "Reauthentication"],
];

function providerPills(config) {
  const on = Object.keys(config)
    .filter((k) => /^external_[a-z0-9_]+_enabled$/.test(k) && config[k] === true)
    .map((k) => k.replace(/^external_/, "").replace(/_enabled$/, ""));
  if (config.external_email_enabled !== false && !on.includes("email")) on.unshift("email");
  return on;
}

function row(label, value) {
  return `<div class="file-row" style="cursor:default">
    <span class="fname" style="flex:0 1 auto">${esc(label)}</span>
    <span class="muted" style="margin-left:auto; overflow:hidden; text-overflow:ellipsis">${value}</span>
  </div>`;
}

function pill(on, yes, no) {
  return on ? `<span class="pill ok">${yes}</span>` : `<span class="pill warn">${no}</span>`;
}

export function initAuth({ paneEl, urls }) {
  const modal = document.getElementById("tpl-preview");
  const title = document.getElementById("tpl-preview-title");
  const subject = document.getElementById("tpl-preview-subject");
  const frame = document.getElementById("tpl-preview-frame");
  const openLink = document.getElementById("tpl-preview-open");
  let loaded = false;
  let config = null;

  function openPreview(key, label) {
    const html = config[`mailer_templates_${key}_content`];
    title.textContent = label;
    subject.textContent = config[`mailer_subjects_${key}`] || "";
    frame.srcdoc = html || "<p style='font:14px system-ui;color:#888;padding:1rem'>Default Supabase template (not customized).</p>";
    if (urls.supabaseDashboard) openLink.href = `${urls.supabaseDashboard}/auth/templates`;
    document.body.classList.add("overlay-open");
    modal.classList.add("open");
  }

  function render() {
    const c = config;
    const providers = providerPills(c)
      .map((p) => `<span class="pill ok">${esc(p)}</span>`)
      .join(" ");
    const templates = TEMPLATES.map(([key, label]) => {
      const subj = c[`mailer_subjects_${key}`] || "";
      return `<button type="button" class="file-row tpl-row" data-key="${key}" data-label="${esc(label)}">
        <span aria-hidden="true">✉</span>
        <span class="fname">${esc(label)}</span>
        <span class="muted" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${esc(subj)}</span>
      </button>`;
    }).join("");
    paneEl.innerHTML = `
      <h2 class="pane-label">Sign-in</h2>
      <div class="stack">
        ${row("Site URL", esc(String(c.site_url || "—")))}
        ${row("Signups", pill(!c.disable_signup, "open", "disabled"))}
        ${row("Confirm email", pill(!c.mailer_autoconfirm, "required", "auto-confirm"))}
        ${row("JWT expiry", `${esc(String(c.jwt_exp ?? "?"))} s`)}
        ${row("Min password length", esc(String(c.password_min_length ?? "?")))}
      </div>
      <h2 class="pane-label" style="margin-top:1.1rem">Providers</h2>
      <div class="stack" style="flex-direction:row; flex-wrap:wrap; gap:0.4rem">${providers || '<span class="muted">none enabled</span>'}</div>
      <h2 class="pane-label" style="margin-top:1.1rem">Email templates <span class="dim-note">— click to preview; the agent edits them through plans</span></h2>
      <div class="stack">${templates}</div>
      ${row("SMTP", pill(Boolean(c.smtp_host), c.smtp_host ? esc(String(c.smtp_host)) : "on", "Supabase default"))}`;
    paneEl.querySelectorAll(".tpl-row").forEach((r) =>
      r.addEventListener("click", () => openPreview(r.dataset.key, r.dataset.label))
    );
  }

  async function load() {
    try {
      const r = await fetch(urls.authConfigUrl);
      const data = await r.json();
      if (data.error) {
        paneEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(data.error)}</p>`;
        return;
      }
      config = data.config;
      render();
    } catch (e) {
      paneEl.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(e.message)}</p>`;
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
