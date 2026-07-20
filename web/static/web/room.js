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
        else if (kind === "bq") out.push("<blockquote>" + buf.map(inline).join("<br>") + "</blockquote>");
        else if (kind === "h") {
          for (const l of buf) {
            const m = l.match(/^(#{1,6})\s+(.*)/);
            const level = Math.min(m[1].length, 4);
            out.push(`<h${level}>` + inline(m[2]) + `</h${level}>`);
          }
        } else if (kind === "hr") out.push("<hr>");
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
        const t = l.trim();
        const k = /^#{1,6}\s/.test(t) ? "h"
          : /^([-*_])\1{2,}$/.test(t) ? "hr"
          : t.startsWith("|") ? "table"
          : t.startsWith("> ") || t === ">" ? "bq"
          : UL.test(l) ? "ul" : OL.test(l) ? "ol" : "p";
        if (k !== kind) { flush(); kind = k; }
        buf.push(k === "ul" ? l.replace(UL, "") : k === "ol" ? l.replace(OL, "") : k === "bq" ? t.replace(/^>\s?/, "") : k === "h" ? t : l);
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
        case "ToolCallPlanned":
          pulse("write", "planning…");
          add("chip", `⊕ ${esc(ev.tool)} → step ${ev.step} <span class="risk pill warn">planned</span>`);
          setOrb("thinking", "planning…");
          break;
        case "PlanProposed":
          loadPlanCard(ev.plan_id);
          break;
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

  /* ---------- plan card: propose → approve / revise / reject → apply ---------- */
  const STEP_ICONS = { pending: "○", applied: "✓", failed: "✕", skipped: "⤼" };
  const PLAN_LABELS = {
    proposed: "waiting for your decision",
    applying: "applying…",
    applied: "applied",
    failed: "apply failed",
    rejected: "rejected",
    superseded: "superseded",
  };

  function planCardEl(planId) {
    let el = document.getElementById("plan-" + planId);
    if (!el) {
      el = document.createElement("div");
      el.id = "plan-" + planId;
      el.className = "plan-card";
      log.appendChild(el);
    }
    return el;
  }

  function renderPlanCard(plan) {
    const el = planCardEl(plan.id);
    el.dataset.status = plan.status;
    const steps = plan.steps
      .map((s) => {
        const sql = s.payload && s.payload.sql ? `<pre><code>${esc(s.payload.sql)}</code></pre>` : "";
        const err = s.output && s.output.error ? `<div class="step-err">${esc(String(s.output.error))}</div>` : "";
        return `<div class="plan-step" data-status="${s.status}">
          <span class="step-mark">${STEP_ICONS[s.status] || "○"}</span>
          <div class="step-body"><span class="step-title">Step ${s.order} · ${esc(s.tool)}</span>${sql}${err}</div>
        </div>`;
      })
      .join("");
    const decidable = plan.status === "proposed";
    el.innerHTML = `
      <div class="plan-head">
        <span class="plan-title">Plan #${plan.id}</span>
        <span class="pill ${plan.status === "applied" ? "ok" : plan.status === "failed" || plan.status === "rejected" ? "err" : "warn"}">${PLAN_LABELS[plan.status] || plan.status}</span>
      </div>
      <div class="plan-steps">${steps}</div>
      ${plan.error ? `<div class="step-err">${esc(plan.error)}</div>` : ""}
      ${decidable ? `
      <div class="plan-actions">
        <button class="plan-approve">Approve &amp; apply</button>
        <button class="ghost plan-revise">Revise</button>
        <button class="ghost plan-reject" style="color:var(--danger)">Reject</button>
      </div>
      <form class="plan-revise-form" hidden>
        <textarea rows="2" placeholder="What should change?"></textarea>
        <button>Send revision</button>
      </form>` : ""}`;
    log.scrollTop = log.scrollHeight;
    if (decidable) wirePlanActions(el, plan.id);
    return el;
  }

  async function planFetch(planId, action, body) {
    const resp = await fetch(`${DIABASE.planBase}${planId}/${action}`, {
      method: action ? "POST" : "GET",
      headers: action ? { "Content-Type": "application/json", "X-CSRFToken": csrf() } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "plan request failed");
    return data;
  }

  function wirePlanActions(el, planId) {
    el.querySelector(".plan-approve").addEventListener("click", async () => {
      try {
        const plan = await planFetch(planId, "approve/");
        renderPlanCard(plan);
        setOrb("write", "applying plan…");
        pollPlanApply(planId);
      } catch (err) { add("msg assistant", "⚠ " + esc(err.message)); }
    });
    el.querySelector(".plan-reject").addEventListener("click", async () => {
      try {
        const plan = await planFetch(planId, "reject/");
        renderPlanCard(plan);
        add("msg plan-note", `Plan #${planId} rejected — nothing was executed.`, true);
        refreshTimeline();
      } catch (err) { add("msg assistant", "⚠ " + esc(err.message)); }
    });
    const reviseForm = el.querySelector(".plan-revise-form");
    el.querySelector(".plan-revise").addEventListener("click", () => {
      reviseForm.hidden = !reviseForm.hidden;
      if (!reviseForm.hidden) reviseForm.querySelector("textarea").focus();
    });
    reviseForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const comment = reviseForm.querySelector("textarea").value.trim();
      if (!comment) return;
      try {
        const data = await planFetch(planId, "revise/", { comment });
        renderPlanCard(data);
        add("msg user", comment, true);
        streamTurn(data.turn_id, 0);
      } catch (err) { add("msg assistant", "⚠ " + esc(err.message)); }
    });
  }

  /* apply runs server-side on a background thread: poll the plan until it
     settles, then attach to the continuation turn the runtime started */
  async function pollPlanApply(planId) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 600));
      let plan;
      try { plan = await planFetch(planId, ""); } catch { continue; }
      renderPlanCard(plan);
      if (plan.status === "applying") continue;
      refreshTimeline();
      refreshSchemaIfOpen();
      if (plan.continuation_turn_id) streamTurn(plan.continuation_turn_id, 0);
      else setOrb("idle", "");
      return;
    }
  }

  async function loadPlanCard(planId) {
    try {
      const plan = await planFetch(planId, "");
      renderPlanCard(plan);
      if (plan.status === "applying") { setOrb("write", "applying plan…"); pollPlanApply(planId); }
    } catch { /* plan may be gone; nothing to render */ }
  }

  // a turn already running when this page loaded (started before a refresh,
  // or from another tab) — reconnect from the beginning: every event is
  // persisted, so replaying from 0 rebuilds the tool chips and partial
  // reply exactly as they'd look if we'd never left, then keeps streaming live
  if (DIABASE.activeTurnId) streamTurn(DIABASE.activeTurnId, 0);
  // likewise a plan waiting for a decision (or mid-apply) is rebuilt from data
  if (DIABASE.activePlanId) loadPlanCard(DIABASE.activePlanId);

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

  /* ---------- schema overlay: auto-layered dependency graph on a pan/zoom canvas ---------- */
  const viewport = document.getElementById("schema-viewport");
  const canvas = document.getElementById("schema-canvas");
  const CARD_W = 252, GAP_X = 96, GAP_Y = 22;

  /* Rank each table by dependency depth: a table with no FKs of its own is
     rank 0 (a "leaf" other tables point at); a table that references others
     is 1 + the deepest rank among its targets. Cycle-safe via a visiting set
     (a back-edge just doesn't extend the rank further). */
  function computeRanks(tables, edges) {
    const outgoing = {};
    tables.forEach((t) => (outgoing[t] = []));
    edges.forEach((e) => { if (outgoing[e.from]) outgoing[e.from].push(e.to); });
    const rank = {}, visiting = new Set();
    function rankOf(t) {
      if (rank[t] !== undefined) return rank[t];
      if (visiting.has(t)) return 0;
      visiting.add(t);
      let r = 0;
      for (const to of outgoing[t]) if (to !== t) r = Math.max(r, 1 + rankOf(to));
      visiting.delete(t);
      return (rank[t] = r);
    }
    tables.forEach(rankOf);
    return rank;
  }

  /* Barycenter crossing reduction: a few alternating sweeps, each reordering
     a layer by the average position of its neighbors in the layer just
     fixed. Cheap and good enough for schema-sized graphs. */
  function reduceCrossings(layers, colKeys, edges) {
    const neighborsOf = {};
    edges.forEach((e) => {
      (neighborsOf[e.from] ??= []).push(e.to);
      (neighborsOf[e.to] ??= []).push(e.from);
    });
    const posIndex = {};
    const sync = () => colKeys.forEach((c) => layers[c].forEach((t, i) => (posIndex[t] = i)));
    sync();
    function sweep(order) {
      for (const c of order) {
        if (layers[c].length < 2) continue;
        const scored = layers[c].map((t) => {
          const positions = (neighborsOf[t] || []).map((n) => posIndex[n]).filter((p) => p !== undefined);
          const bc = positions.length ? positions.reduce((a, b) => a + b, 0) / positions.length : posIndex[t];
          return { t, bc };
        });
        scored.sort((a, b) => a.bc - b.bc);
        layers[c] = scored.map((s) => s.t);
        layers[c].forEach((t, i) => (posIndex[t] = i));
      }
    }
    for (let i = 0; i < 3; i++) { sweep(colKeys); sweep([...colKeys].reverse()); }
  }

  /* Hub-weighted layout: when the graph has a gravitational center (a
     table many others reference, e.g. a profiles/users table), the
     layered left-to-right layout degenerates into one endless column.
     Here the heaviest table (most incoming FKs) sits in the CENTER and
     its satellites spread to both sides by BFS distance, greedily
     balanced by subtree size. Chain-like graphs (no real hub) keep the
     layered flow, which reads better for them. */
  function hubLayout(tables, edges, indeg) {
    const und = {};
    tables.forEach((t) => (und[t] = new Set()));
    edges.forEach((e) => { und[e.from].add(e.to); und[e.to].add(e.from); });

    const hubWeight = Math.max(...tables.map((t) => indeg[t] || 0));
    const hubs = tables.filter((t) => (indeg[t] || 0) === hubWeight);

    const dist = {}, parent = {};
    const queue = [...hubs];
    hubs.forEach((h) => (dist[h] = 0));
    while (queue.length) {
      const t = queue.shift();
      for (const n of und[t]) {
        if (dist[n] === undefined) { dist[n] = dist[t] + 1; parent[n] = t; queue.push(n); }
      }
    }
    const reachedMax = Math.max(...Object.values(dist));
    const isolated = tables.filter((t) => dist[t] === undefined);
    isolated.forEach((t) => (dist[t] = reachedMax + 1));

    // subtree sizes over the BFS tree, to balance the two sides by mass
    const subtree = {};
    [...tables].sort((a, b) => (dist[b] || 0) - (dist[a] || 0)).forEach((t) => {
      subtree[t] = 1 + [...(und[t] || [])].filter((n) => parent[n] === t).reduce((s, n) => s + (subtree[n] || 0), 0);
    });

    const side = {};
    hubs.forEach((h) => (side[h] = 0));
    let leftLoad = 0, rightLoad = 0;
    const firstRing = tables.filter((t) => dist[t] === 1).sort((a, b) => subtree[b] - subtree[a]);
    for (const t of firstRing) {
      side[t] = leftLoad <= rightLoad ? -1 : 1;
      if (side[t] === -1) leftLoad += subtree[t]; else rightLoad += subtree[t];
    }
    for (const t of tables) {
      if (side[t] !== undefined) continue;
      let a = t;
      while (parent[a] !== undefined && side[a] === undefined) a = parent[a];
      side[t] = side[a] ?? (leftLoad <= rightLoad ? ((leftLoad += 1), -1) : ((rightLoad += 1), 1));
    }

    const layers = {};
    tables.forEach((t) => {
      const col = dist[t] * (side[t] || 0);
      (layers[col] ??= []).push(t);
    });
    const colKeys = Object.keys(layers).map(Number).sort((a, b) => a - b);
    return { colKeys, layers };
  }

  function computeLayout(schema) {
    const tables = Object.keys(schema);
    const edges = [];
    for (const [t, cols] of Object.entries(schema)) {
      for (const c of cols) {
        if (c.references && schema[c.references.table]) edges.push({ from: t, to: c.references.table, col: c.name });
      }
    }
    const indeg = {};
    edges.forEach((e) => (indeg[e.to] = (indeg[e.to] || 0) + 1));
    const maxIndeg = Math.max(0, ...Object.values(indeg));

    let colKeys, layers;
    if (maxIndeg >= 3 && tables.length >= 5) {
      ({ colKeys, layers } = hubLayout(tables, edges, indeg));
    } else {
      const rank = computeRanks(tables, edges);
      const maxRank = tables.length ? Math.max(...tables.map((t) => rank[t])) : 0;
      layers = {};
      const keys = [...Array(maxRank + 1).keys()];
      keys.forEach((c) => (layers[c] = []));
      tables.forEach((t) => layers[maxRank - rank[t]].push(t));
      colKeys = keys;
    }
    reduceCrossings(layers, colKeys, edges);
    return { tables, edges, colKeys, layers };
  }

  function renderSchema(schema, shouldFit) {
    const { tables, edges, colKeys, layers } = computeLayout(schema);
    grid.querySelectorAll(".table-card, .fk-svg").forEach((el) => el.remove());
    if (!tables.length) {
      grid.innerHTML = '<p style="color:var(--dim)">No tables yet — ask the agent to create one.</p>';
      knownTables = new Set();
      canvas.style.width = canvas.style.height = "";
      return;
    }
    grid.querySelector("p")?.remove();

    const cardsByTable = {};
    tables.forEach((t) => {
      const rows = schema[t]
        .map((c) => {
          const fk = c.references
            ? ` <span class="fk-hint">→ ${esc(c.references.table)}.${esc(c.references.column)}</span>`
            : "";
          return `<tr data-col="${esc(c.name)}"><td>${esc(c.name)}${c.primary_key ? " 🔑" : ""}${fk}</td><td class="type">${esc(String(c.type))}</td></tr>`;
        })
        .join("");
      const card = document.createElement("div");
      card.className = "table-card measuring" + (knownTables && !knownTables.has(t) ? " new" : "");
      card.dataset.table = t;
      card.style.left = card.style.top = "0px";
      card.innerHTML = `<h3 title="${esc(t)}">▦ ${esc(t)}</h3><table>${rows}</table>`;
      grid.appendChild(card);
      cardsByTable[t] = card;
    });

    // measure pass (fixed width already applied via CSS), then place by
    // column — colKeys may be signed (hub layout), so x comes from the
    // key's ORDER, not its value; columns are vertically centered so the
    // hub sits at the graph's middle instead of hanging from the top
    const canvasW = colKeys.length * (CARD_W + GAP_X) - GAP_X;
    const colHeights = colKeys.map((c) =>
      layers[c].reduce((h, t) => h + cardsByTable[t].offsetHeight + GAP_Y, -GAP_Y)
    );
    const canvasH = Math.max(...colHeights);
    colKeys.forEach((c, ci) => {
      let y = (canvasH - colHeights[ci]) / 2;
      layers[c].forEach((t) => {
        const card = cardsByTable[t];
        card.style.left = ci * (CARD_W + GAP_X) + "px";
        card.style.top = y + "px";
        y += card.offsetHeight + GAP_Y;
      });
    });
    tables.forEach((t, i) => {
      cardsByTable[t].classList.remove("measuring");
      cardsByTable[t].style.animationDelay = i * 35 + "ms";
    });
    canvas.style.width = canvasW + "px";
    canvas.style.height = canvasH + "px";

    knownTables = new Set(tables);
    drawFkEdges(edges, cardsByTable);
    if (shouldFit) fitToView(canvasW, canvasH);
  }

  /* ---------- FK dependency arrows: from the referencing field to its table ---------- */
  function drawFkEdges(edges, cardsByTable) {
    if (!edges.length) return;
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "fk-svg");
    svg.setAttribute("width", canvas.offsetWidth);
    svg.setAttribute("height", canvas.offsetHeight);
    svg.innerHTML =
      '<defs><marker id="fkarrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">' +
      '<path d="M0,0 L7,3.5 L0,7 z" fill="var(--aura-2)"/></marker></defs>';
    for (const e of edges) {
      const srcCard = cardsByTable[e.from], dstCard = cardsByTable[e.to];
      const srcRow = srcCard?.querySelector(`tr[data-col="${CSS.escape(e.col)}"]`);
      if (!srcRow || !dstCard) continue;
      const sy = srcCard.offsetTop + srcRow.offsetTop + srcRow.offsetHeight / 2;
      const dy = dstCard.offsetTop + 16;
      const gap = dstCard.offsetLeft - srcCard.offsetLeft;
      let d;
      if (gap > 20) {
        // rightward: out of the row's right edge, into the target's left
        const sx = srcCard.offsetLeft + srcCard.offsetWidth, dx = dstCard.offsetLeft;
        const bend = Math.max(36, (dx - sx) * 0.4);
        d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${dx - bend} ${dy}, ${dx} ${dy}`;
      } else if (gap < -20) {
        // leftward (hub layout, satellites on the right side)
        const sx = srcCard.offsetLeft, dx = dstCard.offsetLeft + dstCard.offsetWidth;
        const bend = Math.max(36, (sx - dx) * 0.4);
        d = `M ${sx} ${sy} C ${sx - bend} ${sy}, ${dx + bend} ${dy}, ${dx} ${dy}`;
      } else {
        // same column: arc out on the right and back in
        const sx = srcCard.offsetLeft + srcCard.offsetWidth, dx = dstCard.offsetLeft + dstCard.offsetWidth;
        d = `M ${sx} ${sy} C ${sx + 56} ${sy}, ${dx + 56} ${dy}, ${dx} ${dy}`;
      }
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "fk-edge");
      path.setAttribute("marker-end", "url(#fkarrow)");
      svg.appendChild(path);
    }
    grid.appendChild(svg);
  }

  /* ---------- pan & zoom ---------- */
  const zoomPct = document.getElementById("zoom-pct");
  const view = { x: 0, y: 0, scale: 1 };
  const MIN_ZOOM = 0.25, MAX_ZOOM = 2.5;
  function applyView() {
    canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    zoomPct.textContent = Math.round(view.scale * 100) + "%";
  }
  function zoomAt(px, py, factor) {
    const newScale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, view.scale * factor));
    const worldX = (px - view.x) / view.scale, worldY = (py - view.y) / view.scale;
    view.scale = newScale;
    view.x = px - worldX * newScale;
    view.y = py - worldY * newScale;
    applyView();
  }
  function fitToView(canvasW, canvasH) {
    if (!canvasW || !canvasH) return;
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    view.scale = Math.min(MAX_ZOOM, Math.min(vw / canvasW, vh / canvasH) * 0.92, 1);
    view.x = (vw - canvasW * view.scale) / 2;
    view.y = (vh - canvasH * view.scale) / 2;
    applyView();
  }

  let panning = false, panStart = null;
  viewport.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest(".zoom-controls")) return;
    panning = true;
    panStart = { mx: e.clientX, my: e.clientY, vx: view.x, vy: view.y };
    viewport.classList.add("panning");
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    view.x = panStart.vx + (e.clientX - panStart.mx);
    view.y = panStart.vy + (e.clientY - panStart.my);
    applyView();
  });
  window.addEventListener("mouseup", () => { panning = false; viewport.classList.remove("panning"); });
  viewport.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const rect = viewport.getBoundingClientRect();
      const px = e.clientX - rect.left, py = e.clientY - rect.top;
      if (e.ctrlKey || e.metaKey) {
        zoomAt(px, py, Math.exp(-e.deltaY * 0.012));
      } else {
        view.x -= e.deltaX;
        view.y -= e.deltaY;
        applyView();
      }
    },
    { passive: false }
  );
  document.getElementById("zoom-in").addEventListener("click", () => {
    zoomAt(viewport.clientWidth / 2, viewport.clientHeight / 2, 1.25);
  });
  document.getElementById("zoom-out").addEventListener("click", () => {
    zoomAt(viewport.clientWidth / 2, viewport.clientHeight / 2, 0.8);
  });
  document.getElementById("zoom-fit").addEventListener("click", () => {
    fitToView(parseFloat(canvas.style.width) || 0, parseFloat(canvas.style.height) || 0);
  });

  window.addEventListener("resize", () => {
    if (document.body.classList.contains("overlay-open")) loadSchema(true);
  });
  async function loadSchema(shouldFit) {
    const r = await fetch(DIABASE.schemaUrl);
    const data = await r.json();
    if (data.error) grid.innerHTML = `<p style="color:var(--danger)">${esc(data.error)}</p>`;
    else renderSchema(data.schema, shouldFit);
  }
  function refreshSchemaIfOpen() {
    // a background refresh (e.g. a turn just completed) must not yank the
    // camera away from wherever the user has it zoomed/panned to
    if (overlay.classList.contains("open")) loadSchema(false);
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
    loadSchema(true);
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

  /* ---------- context editor modal: source | rendered preview ---------- */
  const editorOverlay = document.getElementById("editor-overlay");
  const editorTitle = document.getElementById("editor-title");
  const editorMeta = document.getElementById("editor-meta");
  const editorSrc = document.getElementById("editor-src");
  const editorPreview = document.getElementById("editor-preview");
  const editorDelete = document.getElementById("editor-delete");
  let editorMode = null; // {kind: "file", name} | {kind: "prompt"}

  const editorGutter = document.getElementById("editor-gutter");
  function updateEditorPreview() {
    editorPreview.innerHTML = md(editorSrc.value) || '<p style="color:var(--dim)">Nothing to preview yet.</p>';
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
    openPanel(editorOverlay);
    editorSrc.focus();
    editorSrc.setSelectionRange(0, 0);
    editorSrc.scrollTop = 0;
    editorGutter.scrollTop = 0;
    editorPreview.scrollTop = 0;
  }

  document.querySelectorAll(".file-row").forEach((row) =>
    row.addEventListener("click", async () => {
      const r = await fetch(`${DIABASE.contextFileGetUrl}?name=${encodeURIComponent(row.dataset.name)}`);
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
    const url = editorMode.kind === "prompt" ? DIABASE.projectUpdateUrl : DIABASE.contextFileSaveUrl;
    await fetch(url, { method: "POST", body });
    window.location.reload();
  });
  editorDelete.addEventListener("click", async () => {
    if (!editorMode || editorMode.kind !== "file") return;
    if (!window.confirm(`Delete ${editorMode.name}?`)) return;
    await fetch(DIABASE.contextFileDeleteUrl, {
      method: "POST",
      body: new URLSearchParams({ name: editorMode.name, csrfmiddlewaretoken: csrf() }),
    });
    window.location.reload();
  });

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
