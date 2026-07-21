/* Sidebar behaviors. Navigation is plain links (server-rendered);
   the only client concern is the mobile drawer. */

export function initSidebar({ shellEl, burgerEl, sidebarEl }) {
  burgerEl.addEventListener("click", (e) => {
    e.stopPropagation();
    shellEl.dataset.drawer = shellEl.dataset.drawer === "1" ? "0" : "1";
  });
  // tap outside closes the drawer
  document.addEventListener("click", (e) => {
    if (shellEl.dataset.drawer === "1" && !sidebarEl.contains(e.target)) {
      shellEl.dataset.drawer = "0";
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") shellEl.dataset.drawer = "0";
  });
}
