/* App shell: the mobile sidebar drawer, shared by every page. */

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
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") shell.dataset.drawer = "0";
  });
}
