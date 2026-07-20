/* Project room: aura orb state machine, SSE chat, schema overlay. */
(() => {
  const orb = document.getElementById("orb");
  const orbLabel = document.getElementById("orb-label");
  const log = document.getElementById("chatlog");
  const form = document.getElementById("chatform");
  const msgEl = document.getElementById("msg");
  const veil = document.getElementById("veil");
  const overlay = document.getElementById("schema-overlay");
  const grid = document.getElementById("schema-grid");
  let knownTables = null;
  let working = false;
  const sendBtn = form.querySelector("button");
  function setWorking(v) {
    working = v;
    msgEl.disabled = v;
    sendBtn.disabled = v;
  }

  /* ---------- markdown (assistant messages only; input always escaped) ---------- */
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  function md(text) {
    const fences = [];
    text = text.replace(/```\w*\n?([\s\S]*?)```/g, (_, c) => {
      fences.push("<pre><code>" + esc(c.replace(/\n$/, "")) + "</code></pre>");
      return "\x00" + (fences.length - 1) + "\x00";
    });
    const out = [];
    for (const block of text.split(/\n{2,}/)) {
      const lines = block.split("\n").filter((l) => l.trim());
      if (!lines.length) continue;
      const fence = block.trim().match(/^\x00(\d+)\x00$/);
      if (fence) { out.push(fences[+fence[1]]); continue; }
      const UL = /^\s*[-*•] /, OL = /^\s*\d+[.)] /;
      let buf = [], kind = null;
      const flush = () => {
        if (!buf.length) return;
        if (kind === "p") out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
        else if (kind === "table") {
          const rows = buf
            .filter((l) => !/^\s*\|[\s:|-]+\|?\s*$/.test(l))
            .map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
          const head = rows.shift() || [];
          out.push(
            "<table><tr>" + head.map((c) => "<th>" + c + "</th>").join("") + "</tr>" +
            rows.map((r) => "<tr>" + r.map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("") + "</table>"
          );
        } else {
          out.push(`<${kind}>` + buf.map((l) => "<li>" + inline(l) + "</li>").join("") + `</${kind}>`);
        }
        buf = [];
      };
      for (const l of lines) {
        const k = l.trim().startsWith("|") ? "table" : UL.test(l) ? "ul" : OL.test(l) ? "ol" : "p";
        if (k !== kind) { flush(); kind = k; }
        buf.push(k === "ul" ? l.replace(UL, "") : k === "ol" ? l.replace(OL, "") : l);
      }
      flush();
    }
    return out.join("").replace(/\x00(\d+)\x00/g, (_, i) => fences[+i]);
  }
  document.querySelectorAll('#chatlog .msg[data-md="1"]').forEach((el) => (el.innerHTML = md(el.textContent)));

  /* ---------- orb ---------- */
  function setOrb(state, label) {
    orb.dataset.state = state;
    orbLabel.textContent = label || "";
    overlay.classList.toggle("working", state === "thinking" || state === "tool" || state === "write");
  }
  function pulse(state, label) {
    setOrb(state, label);
    orb.style.animation = "none";
    void orb.offsetWidth; /* restart the pulse keyframe */
    orb.style.animation = "";
  }

  /* ---------- chat ---------- */
  const add = (cls, html, textOnly) => {
    const d = document.createElement("div");
    d.className = cls;
    if (textOnly) d.textContent = html; else d.innerHTML = html;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  };
  const csrf = () => DIABASE.csrfToken || form.querySelector("[name=csrfmiddlewaretoken]").value;

  /* A turn's events are persisted server-side the instant they happen, so
     the SSE feed is a plain cursor subscription: starting a turn or
     reconnecting to one already running (e.g. after a page refresh) go
     through the exact same consumer below. Nothing about "am I resuming?"
     lives in this function — the server has the state, not the tab. */
  function streamTurn(turnId, afterCursor) {
    setWorking(true);
    let assistantEl = null, assistantText = "";

    function handleEvent(ev) {
      switch (ev.event) {
        case "TextDelta":
          assistantText += ev.text;
          if (!assistantEl) assistantEl = add("msg assistant", "");
          assistantEl.innerHTML = md(assistantText);
          log.scrollTop = log.scrollHeight;
          break;
        case "ToolCallStarted": {
          const risky = ev.tool === "execute_sql";
          pulse(risky ? "write" : "tool", ev.tool + "…");
          add("chip", `⚙ ${esc(ev.tool)} <span class="risk pill ${risky ? "warn" : "ok"}">${risky ? "write" : "read"}</span>`);
          break;
        }
        case "ToolCallDenied":
          add("chip denied", `⛔ ${esc(ev.tool)} — ${esc(ev.reason)}`);
          pulse("error", "blocked by policy");
          break;
        case "ToolCallFinished":
          setOrb("thinking", "thinking…");
          break;
        case "TurnCompleted":
          setOrb("idle", "");
          setWorking(false);
          refreshTimeline();
          refreshSchemaIfOpen();
          break;
        case "TurnFailed":
          add("msg assistant", "⚠ " + esc(ev.error));
          setOrb("error", "something went wrong");
          setWorking(false);
          refreshTimeline();
          break;
      }
    }

    return (async () => {
      setOrb("thinking", "thinking…");
      try {
        const resp = await fetch(`${DIABASE.turnStreamBase}${turnId}/stream/?after=${afterCursor}`);
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const chunk = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
            if (!chunk.startsWith("data: ")) continue;
            handleEvent(JSON.parse(chunk.slice(6)));
          }
        }
      } catch (err) {
        add("msg assistant", "Network error: " + esc(err.message));
        setOrb("error", "connection lost");
      } finally {
        setWorking(false);
      }
    })();
  }

  async function sendMessage(text) {
    add("msg user", text, true);
    setWorking(true);
    setOrb("thinking", "starting…");
    try {
      const resp = await fetch(DIABASE.turnStartUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ message: text }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "could not start the turn");
      await streamTurn(data.turn_id, 0);
    } catch (err) {
      add("msg assistant", "⚠ " + esc(err.message));
      setOrb("error", "");
      setWorking(false);
    }
  }

  // a turn already running when this page loaded (started before a refresh,
  // or from another tab) — reconnect from the beginning: every event is
  // persisted, so replaying from 0 rebuilds the tool chips and partial
  // reply exactly as they'd look if we'd never left, then keeps streaming live
  if (DIABASE.activeTurnId) streamTurn(DIABASE.activeTurnId, 0);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = msgEl.value.trim();
    if (!text || working) return;
    msgEl.value = "";
    sendMessage(text);
  });
  msgEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  /* ---------- timeline ---------- */
  async function refreshTimeline() {
    const r = await fetch(DIABASE.auditUrl);
    if (r.ok) document.getElementById("timeline").innerHTML = await r.text();
  }

  /* ---------- schema overlay ---------- */
  function renderSchema(schema) {
    const names = Object.keys(schema);
    grid.innerHTML = names.length ? "" : '<p style="color:var(--dim)">No tables yet — ask the agent to create one.</p>';
    names.forEach((t, i) => {
      const rows = schema[t]
        .map((c) => {
          const fk = c.references
            ? ` <span class="fk-hint">→ ${esc(c.references.table)}.${esc(c.references.column)}</span>`
            : "";
          return `<tr data-col="${esc(c.name)}"><td>${esc(c.name)}${c.primary_key ? " 🔑" : ""}${fk}</td><td class="type">${esc(String(c.type))}</td></tr>`;
        })
        .join("");
      const card = document.createElement("div");
      card.className = "table-card" + (knownTables && !knownTables.has(t) ? " new" : "");
      card.dataset.table = t;
      card.style.animationDelay = i * 45 + "ms";
      card.innerHTML = `<h3>▦ ${esc(t)}</h3><table>${rows}</table>`;
      grid.appendChild(card);
    });
    knownTables = new Set(names);
    /* edges appear after the cards' entrance animation settles */
    setTimeout(() => drawFkEdges(schema), 480);
  }

  /* ---------- FK dependency arrows: from the referencing field to its table ---------- */
  /* offset-based coordinates: immune to the overlay's scale transform */
  function offsetWithin(el, ancestor) {
    let x = 0, y = 0;
    while (el && el !== ancestor) { x += el.offsetLeft; y += el.offsetTop; el = el.offsetParent; }
    return { x, y };
  }

  function drawFkEdges(schema) {
    grid.querySelector(".fk-svg")?.remove();
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "fk-svg");
    svg.setAttribute("width", grid.scrollWidth);
    svg.setAttribute("height", grid.scrollHeight);
    svg.innerHTML =
      '<defs><marker id="fkarrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">' +
      '<path d="M0,0 L7,3.5 L0,7 z" fill="var(--aura-2)"/></marker></defs>';
    let edges = 0;
    for (const [t, cols] of Object.entries(schema)) {
      for (const c of cols) {
        if (!c.references) continue;
        const srcRow = grid.querySelector(`.table-card[data-table="${CSS.escape(t)}"] tr[data-col="${CSS.escape(c.name)}"]`);
        const dstCard = grid.querySelector(`.table-card[data-table="${CSS.escape(c.references.table)}"]`);
        if (!srcRow || !dstCard) continue;
        const so = offsetWithin(srcRow, grid);
        const doff = { x: dstCard.offsetLeft, y: dstCard.offsetTop };
        const sameColumn = Math.abs(doff.x - so.x) < 40;
        const sy = so.y + srcRow.offsetHeight / 2;
        const dy = doff.y + 16;
        let sx, dx, bend;
        if (sameColumn) {
          /* stacked cards: leave from the right edge, arc outside, come back */
          sx = so.x + srcRow.offsetWidth;
          dx = doff.x + dstCard.offsetWidth;
          bend = 52;
          var d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${dx + bend} ${dy}, ${dx} ${dy}`;
        } else {
          const goLeft = doff.x < so.x;
          sx = goLeft ? so.x : so.x + srcRow.offsetWidth;
          dx = goLeft ? doff.x + dstCard.offsetWidth : doff.x;
          bend = Math.max(36, Math.abs(dx - sx) * 0.35) * (goLeft ? -1 : 1);
          d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${dx - bend} ${dy}, ${dx} ${dy}`;
        }
        const path = document.createElementNS(svgNS, "path");
        path.setAttribute("d", d);
        path.setAttribute("class", "fk-edge");
        path.setAttribute("marker-end", "url(#fkarrow)");
        svg.appendChild(path);
        edges++;
      }
    }
    if (edges) grid.appendChild(svg);
  }
  window.addEventListener("resize", () => {
    if (document.body.classList.contains("overlay-open")) loadSchema();
  });
  async function loadSchema() {
    const r = await fetch(DIABASE.schemaUrl);
    const data = await r.json();
    if (data.error) grid.innerHTML = `<p style="color:var(--danger)">${esc(data.error)}</p>`;
    else renderSchema(data.schema);
  }
  function refreshSchemaIfOpen() {
    if (overlay.classList.contains("open")) loadSchema();
    else knownTables = null; /* forget diff state when closed */
  }
  function openPanel(panel) {
    document.body.classList.add("overlay-open");
    panel.classList.add("open");
    panel.querySelector("[data-close]")?.focus();
  }
  function closePanels() {
    document.body.classList.remove("overlay-open");
    document.querySelectorAll(".schema-overlay.open").forEach((p) => p.classList.remove("open"));
    orb.focus();
  }
  function openSchemaPanel() {
    const r = orb.getBoundingClientRect();
    overlay.style.setProperty("--orb-x", r.left + r.width / 2 + "px");
    overlay.style.setProperty("--orb-y", r.top + r.height / 2 + "px");
    openPanel(overlay);
    loadSchema();
  }
  orb.addEventListener("click", openSchemaPanel);
  orb.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSchemaPanel(); } });
  veil.addEventListener("click", closePanels);
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closePanels));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanels(); });

  /* ---------- audit log panel ---------- */
  const auditOverlay = document.getElementById("audit-overlay");
  const auditList = document.getElementById("audit-log-list");
  const auditMore = document.getElementById("audit-more");
  let auditCursor = 0;
  async function loadAuditPage(reset) {
    if (reset) { auditList.innerHTML = ""; auditCursor = 0; }
    const url = DIABASE.auditLogUrl + (auditCursor ? "?before=" + auditCursor : "");
    const r = await fetch(url);
    if (!r.ok) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = await r.text();
    const page = tmp.querySelector(".audit-page");
    auditList.append(...page.children);
    auditCursor = +page.dataset.nextBefore;
    auditMore.style.display = page.dataset.hasMore === "1" ? "" : "none";
  }
  document.getElementById("audit-open").addEventListener("click", () => {
    openPanel(auditOverlay);
    loadAuditPage(true);
  });
  auditMore.addEventListener("click", () => loadAuditPage(false));

  /* ---------- context files: drag & drop uploader ---------- */
  const dropzone = document.getElementById("dropzone");
  const dzInput = document.getElementById("dz-input");
  const CFILE_MAX_BYTES = 100 * 1024;

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
        add("msg assistant", `⚠ ${esc(file.name)} is ${Math.round(file.size / 1024)} KB — the limit is 100 KB.`);
        continue;
      }
      const content = await readAsText(file);
      const body = new URLSearchParams({ name: file.name, content, csrfmiddlewaretoken: csrf() });
      await fetch(DIABASE.contextFileSaveUrl, { method: "POST", body });
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

  log.scrollTop = log.scrollHeight;
})();
