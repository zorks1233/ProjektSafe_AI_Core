/* ============ UI-Helfer: Markdown-Renderer (XSS-sicher), Toast, Contextmenü ============ */
"use strict";
const UI = (() => {

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Minimaler, sicherer Markdown-Renderer: escaped ALLES zuerst, baut dann
     Codeblöcke, Inline-Code, Fett/Kursiv, Links (nur http/https), Listen,
     Tabellen und Blockquotes als eigenes DOM-HTML. Kein rohes HTML durchlässig. */
  function renderMarkdown(src) {
    const codeBlocks = [];
    let text = String(src || "");
    // ```code``` extrahieren
    text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const i = codeBlocks.push({ lang, code }) - 1;
      return "\u0000CB" + i + "\u0000";
    });
    let html = esc(text);
    // Überschriften
    html = html.replace(/^### (.*)$/gm, "<h4>$1</h4>")
               .replace(/^## (.*)$/gm, "<h3>$1</h3>")
               .replace(/^# (.*)$/gm, "<h2>$1</h2>");
    // Inline-Code
    html = html.replace(/`([^`\n]+)`/g, (_, c) => "<code>" + c + "</code>");
    // Fett / Kursiv
    html = html.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
               .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
    // Links nur http(s)
    html = html.replace(/\[([^\]]+)\]\((https?:[^\)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // Blockquote
    html = html.replace(/^&gt; ?(.*)$/gm, "<blockquote>$1</blockquote>");
    // Listen (ul/ol) – einfache Zeilen-Matching-Version
    html = html.replace(/(?:^(?:[-*] .*)\n?)+/gm, (block) => {
      const items = block.trim().split("\n").map(l => "<li>" + l.replace(/^[-*] /, "") + "</li>").join("");
      return "<ul>" + items + "</ul>\n";
    });
    html = html.replace(/(?:^(?:\d+\. .*)\n?)+/gm, (block) => {
      const items = block.trim().split("\n").map(l => "<li>" + l.replace(/^\d+\. /, "") + "</li>").join("");
      return "<ol>" + items + "</ol>\n";
    });
    // Markdown-Tabelle (| a | b |)
    html = html.replace(/(?:^\|.+\|\s*\n)+/gm, (block) => {
      const rows = block.trim().split("\n").map(r => r.split("|").slice(1, -1).map(c => c.trim()));
      if (rows.length < 2) return block;
      const isSep = rows[1].every(c => /^:?-{2,}:?$/.test(c));
      if (!isSep) return block;
      const head = rows[0], body = rows.slice(2);
      return "<table><thead><tr>" + head.map(h => "<th>" + h + "</th>").join("") + "</tr></thead><tbody>" +
        body.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("") + "</tbody></table>\n";
    });
    // Absätze
    html = html.split(/\n{2,}/).map(p => p.includes("<") && !p.startsWith("<") ? "<p>" + p + "</p>" : "<p>" + p + "</p>")
      .join("").replace(/<p><\/p>/g, "").replace(/\n/g, "<br>");
    // Codeblöcke wieder einsetzen (mit Kopieren-Button)
    html = html.replace(/\u0000CB(\d+)\u0000/g, (_, i) => {
      const cb = codeBlocks[+i];
      return `<pre data-lang="${esc(cb.lang || "text")}"><button class="copy-code" type="button">Kopieren</button><code>${esc(cb.code)}</code></pre>`;
    });
    return html;
  }

  let toastTimer = null;
  function toast(msg, ms) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), ms || 2600);
  }

  /* Kontextmenü (für Session-Einträge & Nutzer-Menü) */
  function ctxMenu(x, y, items) {
    const menu = document.getElementById("ctx-menu");
    menu.innerHTML = "";
    for (const it of items) {
      const b = document.createElement("button");
      b.textContent = it.label;
      if (it.danger) b.className = "danger";
      b.addEventListener("click", () => { hideCtx(); it.action(); });
      menu.appendChild(b);
    }
    menu.classList.remove("hidden");
    const w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.min(x, innerWidth - w - 8) + "px";
    menu.style.top = Math.min(y, innerHeight - h - 8) + "px";
  }
  function hideCtx() { document.getElementById("ctx-menu").classList.add("hidden"); }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#ctx-menu")) hideCtx();
  });

  /* Text in Zwischenablage (mit Fallback) */
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("In die Zwischenablage kopiert"); }
    catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove(); toast("Kopiert");
    }
  }

  function timeAgo(iso) {
    const d = new Date(iso).getTime() / 1000;
    const s = Math.max(0, Date.now() / 1000 - d);
    if (s < 60) return "gerade eben";
    if (s < 3600) return `vor ${Math.floor(s / 60)} Min.`;
    if (s < 86400) return `vor ${Math.floor(s / 3600)} Std.`;
    if (s < 7 * 86400) return `vor ${Math.floor(s / 86400)} T.`;
    return new Date(iso).toLocaleDateString("de-DE");
  }

  return { esc, renderMarkdown, toast, ctxMenu, hideCtx, copy, timeAgo };
})();
