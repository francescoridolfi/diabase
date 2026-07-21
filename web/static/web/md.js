/* Markdown mini-renderer for assistant messages. Input is ALWAYS escaped
   first: the only HTML that reaches the DOM is what this module emits. */

export const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const inline = (s) =>
  esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");

export function md(text) {
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
