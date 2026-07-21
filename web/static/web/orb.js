/* Aura orb state machine. The orb is the agent's ambient status light
   (idle / thinking / tool / write / error); the workspace panel's orbit
   border follows the same state via its .working class. */

export function initOrb({ orbEl, labelEl, workspaceEl }) {
  function set(state, label) {
    orbEl.dataset.state = state;
    labelEl.textContent = label || "";
    workspaceEl.classList.toggle("working", state === "thinking" || state === "tool" || state === "write");
  }
  function pulse(state, label) {
    set(state, label);
    orbEl.style.animation = "none";
    void orbEl.offsetWidth; /* restart the pulse keyframe */
    orbEl.style.animation = "";
  }
  return { set, pulse };
}
