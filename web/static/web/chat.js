/* Chat column: message log, SSE turn streaming, composer.

   A turn's events are persisted server-side the instant they happen, so
   the SSE feed is a plain cursor subscription: starting a turn or
   reconnecting to one already running (e.g. after a page refresh) go
   through the exact same consumer. The server has the state, not the tab. */

import { md, esc } from "./md.js";

export function initChat({ log, form, msgEl, orb, urls, csrf, hooks }) {
  const sendBtn = form.querySelector("button");
  let working = false;

  function setWorking(v) {
    working = v;
    msgEl.disabled = v;
    sendBtn.disabled = v;
  }

  /* Stick-to-bottom, not force-to-bottom: autoscroll only follows new
     content while the user is already at (or near) the end — scrolling
     up to reread must never be yanked back down by a stream update. */
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const followIf = (stick) => { if (stick) log.scrollTop = log.scrollHeight; };

  const add = (cls, html, textOnly) => {
    const stick = nearBottom();
    const d = document.createElement("div");
    d.className = cls;
    if (textOnly) d.textContent = html; else d.innerHTML = html;
    log.appendChild(d);
    followIf(stick);
    return d;
  };

  function streamTurn(turnId, afterCursor) {
    setWorking(true);
    let assistantEl = null, assistantText = "";

    function handleEvent(ev) {
      switch (ev.event) {
        case "TextDelta": {
          const stick = nearBottom();
          assistantText += ev.text;
          if (!assistantEl) assistantEl = add("msg assistant", "");
          assistantEl.innerHTML = md(assistantText);
          followIf(stick);
          break;
        }
        case "ToolCallStarted": {
          const risky = ev.tool === "execute_sql";
          orb.pulse(risky ? "write" : "tool", ev.tool + "…");
          add("chip", `⚙ ${esc(ev.tool)} <span class="risk pill ${risky ? "warn" : "ok"}">${risky ? "write" : "read"}</span>`);
          break;
        }
        case "ToolCallPlanned":
          orb.pulse("write", "planning…");
          add("chip", `⊕ ${esc(ev.tool)} → step ${ev.step} <span class="risk pill warn">planned</span>`);
          orb.set("thinking", "planning…");
          hooks.onToolEvent();
          break;
        case "PlanProposed":
          hooks.onPlanProposed(ev.plan_id);
          break;
        case "ToolCallDenied":
          add("chip denied", `⛔ ${esc(ev.tool)} — ${esc(ev.reason)}`);
          orb.pulse("error", "blocked by policy");
          hooks.onToolEvent();
          break;
        case "ToolCallFinished":
          orb.set("thinking", "thinking…");
          hooks.onToolEvent();
          break;
        case "TurnCompleted":
          orb.set("idle", "");
          setWorking(false);
          hooks.onTurnSettled();
          break;
        case "TurnFailed":
          add("msg assistant", "⚠ " + esc(ev.error));
          orb.set("error", "something went wrong");
          setWorking(false);
          hooks.onTurnSettled();
          break;
      }
    }

    return (async () => {
      orb.set("thinking", "thinking…");
      try {
        const resp = await fetch(`${urls.turnStreamBase}${turnId}/stream/?after=${afterCursor}`);
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
        orb.set("error", "connection lost");
      } finally {
        setWorking(false);
      }
    })();
  }

  async function sendMessage(text) {
    add("msg user", text, true);
    setWorking(true);
    orb.set("thinking", "starting…");
    try {
      const resp = await fetch(urls.turnStartUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ message: text, conversation_id: urls.conversationId }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "could not start the turn");
      await streamTurn(data.turn_id, 0);
    } catch (err) {
      add("msg assistant", "⚠ " + esc(err.message));
      orb.set("error", "");
      setWorking(false);
    }
  }

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

  // server-rendered assistant messages get the markdown treatment on load
  log.querySelectorAll('.msg[data-md="1"]').forEach((el) => (el.innerHTML = md(el.textContent)));
  log.scrollTop = log.scrollHeight;

  return { add, streamTurn, nearBottom, followIf, setWorking };
}
