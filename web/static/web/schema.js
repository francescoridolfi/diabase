/* Schema canvas: auto-layered FK graph on a pan/zoom viewport.
   Input is unified Pointer Events — mouse drag, touch drag and two-finger
   pinch all work; wheel scrolls, ctrl/cmd+wheel (trackpad pinch) zooms. */

import { esc } from "./md.js";

const CARD_W = 252, GAP_X = 96, GAP_Y = 22;
const MIN_ZOOM = 0.25, MAX_ZOOM = 2.5;

/* Rank each table by dependency depth: a table with no FKs of its own is
   rank 0 (a "leaf" other tables point at); a table that references others
   is 1 + the deepest rank among its targets. Cycle-safe via a visiting set. */
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
   a layer by the average position of its neighbors in the layer just fixed. */
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

/* Hub-weighted layout: the most-referenced table sits in the CENTER and
   its satellites spread to both sides by BFS distance, greedily balanced
   by subtree size. Chain-like graphs keep the layered flow instead. */
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

export function initSchema({ viewport, canvas, grid, url }) {
  let knownTables = null;
  const view = { x: 0, y: 0, scale: 1 };
  const zoomPct = viewport.querySelector(".zoom-pct");

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

  function renderSchema(schema, shouldFit) {
    const { tables, edges, colKeys, layers } = computeLayout(schema);
    grid.querySelectorAll(".table-card, .fk-svg").forEach((el) => el.remove());
    if (!tables.length) {
      grid.innerHTML = '<p class="dim-note">No tables yet — ask the agent to create one.</p>';
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

    // measure pass, then place by column — colKeys may be signed (hub
    // layout), so x comes from the key's ORDER; columns are vertically
    // centered so the hub sits at the graph's middle
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
        const sx = srcCard.offsetLeft + srcCard.offsetWidth, dx = dstCard.offsetLeft;
        const bend = Math.max(36, (dx - sx) * 0.4);
        d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${dx - bend} ${dy}, ${dx} ${dy}`;
      } else if (gap < -20) {
        const sx = srcCard.offsetLeft, dx = dstCard.offsetLeft + dstCard.offsetWidth;
        const bend = Math.max(36, (sx - dx) * 0.4);
        d = `M ${sx} ${sy} C ${sx - bend} ${sy}, ${dx + bend} ${dy}, ${dx} ${dy}`;
      } else {
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

  /* ---------- input: unified pointers (mouse + touch + pinch) ---------- */
  const pointers = new Map(); // pointerId → {x, y}
  let pinchStart = null; // {dist, scale}
  let panStart = null; // {mx, my, vx, vy}

  viewport.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".zoom-controls")) return;
    viewport.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) {
      panStart = { mx: e.clientX, my: e.clientY, vx: view.x, vy: view.y };
      viewport.classList.add("panning");
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchStart = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale: view.scale };
      panStart = null;
    }
  });
  viewport.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2 && pinchStart) {
      const [a, b] = [...pointers.values()];
      const rect = viewport.getBoundingClientRect();
      const cx = (a.x + b.x) / 2 - rect.left, cy = (a.y + b.y) / 2 - rect.top;
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const target = pinchStart.scale * (dist / pinchStart.dist);
      zoomAt(cx, cy, target / view.scale);
    } else if (panStart) {
      view.x = panStart.vx + (e.clientX - panStart.mx);
      view.y = panStart.vy + (e.clientY - panStart.my);
      applyView();
    }
  });
  const endPointer = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinchStart = null;
    if (pointers.size === 0) { panStart = null; viewport.classList.remove("panning"); }
    else if (pointers.size === 1) {
      const [p] = [...pointers.values()];
      panStart = { mx: p.x, my: p.y, vx: view.x, vy: view.y };
    }
  };
  viewport.addEventListener("pointerup", endPointer);
  viewport.addEventListener("pointercancel", endPointer);
  viewport.style.touchAction = "none"; // we own every gesture inside

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
  viewport.querySelector("#zoom-in").addEventListener("click", () => {
    zoomAt(viewport.clientWidth / 2, viewport.clientHeight / 2, 1.25);
  });
  viewport.querySelector("#zoom-out").addEventListener("click", () => {
    zoomAt(viewport.clientWidth / 2, viewport.clientHeight / 2, 0.8);
  });
  viewport.querySelector("#zoom-fit").addEventListener("click", () => {
    fitToView(parseFloat(canvas.style.width) || 0, parseFloat(canvas.style.height) || 0);
  });

  async function load(shouldFit) {
    const r = await fetch(url);
    const data = await r.json();
    if (data.error) grid.innerHTML = `<p class="dim-note" style="color:var(--danger)">${esc(data.error)}</p>`;
    else renderSchema(data.schema, shouldFit);
  }

  let loadedOnce = false;
  /* called when the schema pane becomes visible */
  function shown() {
    load(!loadedOnce);
    loadedOnce = true;
  }
  /* called when a turn finishes: refresh in place if visible, else forget
     the diff state so the next open re-fits and highlights nothing stale */
  function refreshIfVisible(visible) {
    if (visible) load(false);
    else { knownTables = null; loadedOnce = false; }
  }

  return { shown, refreshIfVisible };
}
