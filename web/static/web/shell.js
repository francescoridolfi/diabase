/* App shell: the mobile sidebar drawer and the generic modal openers,
   shared by every page. */

const shell = document.getElementById("app-shell");
const burger = document.getElementById("burger");
const sidebar = document.getElementById("sidebar");

if (burger) {
  burger.addEventListener("click", (e) => {
    e.stopPropagation();
    shell.dataset.drawer = shell.dataset.drawer === "1" ? "0" : "1";
  });
  document.addEventListener("click", (e) => {
    if (shell.dataset.drawer === "1" && !sidebar.contains(e.target)) shell.dataset.drawer = "0";
  });
}

/* [data-modal-open="id"] buttons open the matching .modal-overlay;
   the veil, Esc or any [data-close] inside it close it again */
const veil = document.getElementById("veil");

function closeModals() {
  document.querySelectorAll(".modal-overlay.open").forEach((m) => m.classList.remove("open"));
  document.body.classList.remove("overlay-open");
}

document.querySelectorAll("[data-modal-open]").forEach((btn) =>
  btn.addEventListener("click", () => {
    const modal = document.getElementById(btn.dataset.modalOpen);
    if (!modal) return;
    document.body.classList.add("overlay-open");
    modal.classList.add("open");
    modal.querySelector("input, select, textarea")?.focus();
  })
);
document.querySelectorAll(".modal-overlay [data-close]").forEach((b) => b.addEventListener("click", closeModals));
veil?.addEventListener("click", closeModals);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (shell.dataset.drawer === "1") shell.dataset.drawer = "0";
  closeModals();
});
