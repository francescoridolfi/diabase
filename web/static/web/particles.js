/* Orbit particles: glowing sparks riding the workspace panel's border,
   trailing the conic band (same clockwise direction). Inspired by Nate
   Wiley's "Particle Orb CSS", rebuilt on canvas: a rounded-rect path is
   parameterized by arc length and each particle travels it with its own
   speed, size, color and radial wobble. While the agent works the swarm
   accelerates with the orbit (idle ~14s → working ~1.4s feel).

   Cheap by construction: one canvas, ~36 dots, no DOM churn; skipped
   entirely under prefers-reduced-motion, paused when the tab is hidden
   or the panel is collapsed. */

const COUNT = 36;
const MARGIN = 12; // canvas bleeds past the panel so the glow isn't clipped
const CORNER = 22; // matches the panel's border-radius
const COLORS = ["--aura-1", "--aura-2", "--aura-3", "--aura-4"];
const IDLE_SPEED = 30; // px/s along the perimeter
const WORKING_MULT = 8; // how much the swarm hurries while the agent works

export function initParticles({ workspaceEl }) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const canvas = document.createElement("canvas");
  canvas.className = "orbit-particles";
  canvas.setAttribute("aria-hidden", "true");
  workspaceEl.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  const palette = COLORS.map((v) => getComputedStyle(workspaceEl).getPropertyValue(v).trim());

  /* ---------- rounded-rect path, parameterized by arc length ---------- */
  let W = 0, H = 0, L = 1, segments = [];

  function buildPath() {
    W = workspaceEl.offsetWidth + MARGIN * 2;
    H = workspaceEl.offsetHeight + MARGIN * 2;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const r = CORNER + MARGIN / 2;
    const x0 = 4, y0 = 4, x1 = W - 4, y1 = H - 4; // the border line, roughly
    const sw = x1 - x0 - 2 * r, sh = y1 - y0 - 2 * r; // straight lengths
    const arc = (Math.PI / 2) * r;
    // clockwise from the top-left corner's end: top, TR arc, right, BR arc,
    // bottom, BL arc, left, TL arc — each segment knows its length and how
    // to emit a point at a distance within it
    segments = [
      { len: sw, at: (d) => [x0 + r + d, y0] },
      { len: arc, at: (d) => arcPoint(x1 - r, y0 + r, r, -Math.PI / 2, d) },
      { len: sh, at: (d) => [x1, y0 + r + d] },
      { len: arc, at: (d) => arcPoint(x1 - r, y1 - r, r, 0, d) },
      { len: sw, at: (d) => [x1 - r - d, y1] },
      { len: arc, at: (d) => arcPoint(x0 + r, y1 - r, r, Math.PI / 2, d) },
      { len: sh, at: (d) => [x0, y1 - r - d] },
      { len: arc, at: (d) => arcPoint(x0 + r, y0 + r, r, Math.PI, d) },
    ];
    L = segments.reduce((s, seg) => s + seg.len, 0);
  }

  function arcPoint(cx, cy, r, startAngle, d) {
    const a = startAngle + d / r;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  }

  function pointAt(dist) {
    let d = ((dist % L) + L) % L;
    for (const seg of segments) {
      if (d <= seg.len) return seg.at(d);
      d -= seg.len;
    }
    return segments[0].at(0);
  }

  /* ---------- the swarm ---------- */
  const rand = (a, b) => a + Math.random() * (b - a);
  const particles = Array.from({ length: COUNT }, (_, i) => ({
    dist: (i / COUNT) * 4000 + rand(0, 90), // spread out, then randomized
    speed: rand(0.6, 1.6), // personal multiplier
    size: rand(0.8, 2.4),
    color: palette[i % palette.length],
    wobblePhase: rand(0, Math.PI * 2),
    wobbleFreq: rand(0.4, 1.2),
    wobbleAmp: rand(1.5, 5),
    twinklePhase: rand(0, Math.PI * 2),
  }));

  let speedMult = 1; // eased toward 1 (idle) or WORKING_MULT
  let last = performance.now();
  let running = false;
  let rafId = 0;

  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    const t = now / 1000;

    const target = workspaceEl.classList.contains("working") ? WORKING_MULT : 1;
    speedMult += (target - speedMult) * Math.min(1, dt * 3);

    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";

    for (const p of particles) {
      p.dist += IDLE_SPEED * p.speed * speedMult * dt;
      const wobble = Math.sin(t * p.wobbleFreq * Math.PI * 2 + p.wobblePhase) * p.wobbleAmp;
      const [x, y] = pointAt(p.dist);
      const [xb, yb] = pointAt(p.dist - 8 - speedMult * 3); // tail anchor
      // push the dot slightly off the path, perpendicular-ish via the tail direction
      const dx = x - xb, dy = y - yb;
      const n = Math.hypot(dx, dy) || 1;
      const px = x + (-dy / n) * wobble;
      const py = y + (dx / n) * wobble;

      const twinkle = 0.55 + 0.45 * Math.sin(t * 2.1 + p.twinklePhase);

      // comet tail: a short fading streak back along the path
      ctx.strokeStyle = p.color;
      ctx.globalAlpha = 0.14 * twinkle;
      ctx.lineWidth = p.size;
      ctx.beginPath();
      ctx.moveTo(xb + (-dy / n) * wobble * 0.4, yb + (dx / n) * wobble * 0.4);
      ctx.lineTo(px, py);
      ctx.stroke();

      // the spark itself, with a soft glow
      ctx.globalAlpha = 0.85 * twinkle;
      ctx.shadowColor = p.color;
      ctx.shadowBlur = 6 + p.size * 3;
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(px, py, p.size, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";

    rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (running || document.hidden || workspaceEl.offsetWidth === 0) return;
    running = true;
    buildPath();
    last = performance.now();
    rafId = requestAnimationFrame(frame);
  }
  function stop() {
    running = false;
    cancelAnimationFrame(rafId);
  }

  new ResizeObserver(() => {
    // a collapsed panel (orb toggle) reports width 0: sleep until reopened
    if (workspaceEl.offsetWidth === 0) stop();
    else if (!running) start();
    else buildPath();
  }).observe(workspaceEl);

  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
}
