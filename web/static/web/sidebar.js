/* Sidebar chat list: inline rename. The pencil (or a double-click on
   the active chat) swaps the row's link for an input — Enter saves,
   Esc or blur cancels. The save is a plain POST the server audits
   as chat.renamed. */

export function initSidebar({ urls, csrf }) {
  document.querySelectorAll(".chat-item[data-chat-id]").forEach((item) => {
    const link = item.querySelector("a");
    const nameEl = item.querySelector(".chat-name");
    const editBtn = item.querySelector(".chat-edit");
    if (!link || !nameEl || !editBtn) return;

    function startEditing() {
      if (item.querySelector(".chat-rename")) return;
      const input = document.createElement("input");
      input.className = "chat-rename";
      input.maxLength = 80;
      input.value = nameEl.textContent.trim();
      input.setAttribute("aria-label", "Chat title");
      link.hidden = true;
      item.classList.add("renaming");
      item.insertBefore(input, link);
      input.focus();
      input.select();

      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        input.remove();
        item.classList.remove("renaming");
        link.hidden = false;
      };

      input.addEventListener("keydown", async (e) => {
        if (e.key === "Escape") {
          e.stopPropagation(); // keep shell.js from closing the drawer
          finish();
        } else if (e.key === "Enter") {
          e.preventDefault();
          const title = input.value.trim();
          if (!title || title === nameEl.textContent.trim()) return finish();
          const r = await fetch(`${urls.chatRenameBase}${item.dataset.chatId}/rename/`, {
            method: "POST",
            headers: { "X-CSRFToken": csrf() },
            body: new URLSearchParams({ title }),
          });
          if (r.ok) {
            const data = await r.json();
            nameEl.textContent = data.title;
            if (item.classList.contains("active")) {
              const h1 = document.querySelector(".chat-title h1");
              if (h1) h1.textContent = data.title;
            }
          }
          finish();
        }
      });
      input.addEventListener("blur", finish);
    }

    editBtn.addEventListener("click", startEditing);
    if (item.classList.contains("active")) {
      // clicking the active chat would only reload the page; swallow it
      // so a double-click can reach us and start the rename instead
      link.addEventListener("click", (e) => e.preventDefault());
      link.addEventListener("dblclick", startEditing);
    }
  });
}
