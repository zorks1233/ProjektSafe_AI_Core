/* ============ App-Controller: Auth, Sidebar, Sessions, Modelle, Modals ============ */
"use strict";
const App = (() => {
  const $ = (id) => document.getElementById(id);
  let sessions = [];
  let activeId = null;
  let backends = [];
  let localOnly = false;
  let authMode = "login";

  /* ---------- Modell-Auswahl ---------- */
  function currentModel() { return $("model-select").value || "auto"; }
  function currentModelLabel() {
    const sel = $("model-select");
    return sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent.trim() : "auto";
  }
  async function loadModels() {
    try {
      const data = await API.models();
      backends = data.backends || [];
      const sel = $("model-select");
      sel.innerHTML = "";
      const optAuto = document.createElement("option");
      optAuto.value = "auto"; optAuto.textContent = "SafeAI Auto-Routing";
      sel.appendChild(optAuto);
      for (const b of backends) {
        const o = document.createElement("option");
        o.value = b.id;
        o.textContent = b.id + (b.provider && b.provider !== "local" ? " · " + b.provider : " · lokal");
        sel.appendChild(o);
      }
    } catch (_) { /* Backend evtl. noch nicht bereit */ }
  }

  /* ---------- Ressourcen-Pill ---------- */
  async function pollResources() {
    try {
      const r = await API.resources();
      $("res-pill").textContent = `CPU ${Math.round(r.cpu_percent)}% · RAM ${Math.round(r.ram_percent)}%${r.gpu && r.gpu.available ? " · GPU ✓" : ""}`;
    } catch (_) {}
  }

  /* ---------- Sidebar / Sessions ---------- */
  function markActive(id) {
    activeId = id;
    document.querySelectorAll(".session-item").forEach(el =>
      el.classList.toggle("active", el.dataset.id === id));
  }
  function closeSidebarOnMobile() {
    if (innerWidth <= 768) setSidebar(false);
  }
  function setSidebar(open) {
    $("sidebar").classList.toggle("collapsed", !open);
    $("scrim").classList.toggle("hidden", open || innerWidth > 768);
  }

  async function refreshSessions() {
    try { sessions = await API.sessions(); } catch (_) { return; }
    renderSessionList($("search-input") ? $("search-input").value : "");
  }

  function groupLabel(iso) {
    const d = new Date(iso).getTime() / 1000, s = Date.now() / 1000 - d;
    if (s < 86400) return "Heute";
    if (s < 7 * 86400) return "Letzte 7 Tage";
    if (s < 30 * 86400) return "Letzter Monat";
    return "Älter";
  }

  function renderSessionList(filter) {
    const list = $("session-list");
    list.innerHTML = "";
    const q = (filter || "").toLowerCase();
    const items = sessions.filter(s => !q || s.title.toLowerCase().includes(q));
    if (!items.length) {
      const e = document.createElement("div");
      e.className = "empty-hist";
      e.textContent = q ? "Keine Treffer" : "Noch keine Chats";
      list.appendChild(e);
      return;
    }
    const pinned = items.filter(s => s.pinned);
    const rest = items.filter(s => !s.pinned).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const groups = [];
    if (pinned.length) groups.push(["Angepinnt", pinned]);
    let cur = null;
    for (const s of rest) {
      const g = groupLabel(s.updated_at);
      if (!cur || cur[0] !== g) { cur = [g, []]; groups.push(cur); }
      cur[1].push(s);
    }
    for (const [label, arr] of groups) {
      const h = document.createElement("div");
      h.className = "history-label"; h.textContent = label;
      list.appendChild(h);
      for (const s of arr) {
        const el = document.createElement("div");
        el.className = "session-item" + (s.id === activeId ? " active" : "");
        el.dataset.id = s.id;
        el.innerHTML = `${s.pinned ? '<span class="pin">📌</span>' : ""}<span class="t"></span><button class="more">⋯</button>`;
        el.querySelector(".t").textContent = s.title || "New chat";
        el.addEventListener("click", (e) => {
          if (e.target.closest(".more")) return;
          Chat.openSession(s.id);
        });
        el.querySelector(".more").addEventListener("click", (e) => {
          e.stopPropagation();
          const r = e.target.getBoundingClientRect();
          UI.ctxMenu(r.left, r.bottom + 4, [
            { label: "✏️ Umbenennen", action: async () => {
                const t = prompt("Neuer Titel:", s.title);
                if (t) { await API.sessionAction(s.id, "rename", { title: t }); refreshSessions(); }
              } },
            { label: s.pinned ? "📌 Lösen" : "📌 Anheften", action: async () => {
                await API.sessionAction(s.id, "pin", { pinned: !s.pinned }); refreshSessions();
              } },
            { label: "🗑 Löschen", danger: true, action: async () => {
                if (!confirm(`Chat „${s.title}“ wirklich löschen?`)) return;
                await API.sessionAction(s.id, "delete");
                if (activeId === s.id) Chat.newChat();
                refreshSessions();
              } },
          ]);
        });
        list.appendChild(el);
      }
    }
  }

  /* ---------- Auth ---------- */
  function showAuth() { $("modal-auth").classList.remove("hidden"); }
  function hideAuth() { $("modal-auth").classList.add("hidden"); }

  async function afterLogin(user) {
    hideAuth();
    $("user-email").textContent = user.email;
    $("user-avatar").textContent = (user.email[0] || "?").toUpperCase();
    localStorage.setItem("safeai_user", JSON.stringify(user));
    await Promise.all([refreshSessions(), loadModels()]);
    Chat.newChat();
  }

  function logout() {
    API.setToken("");
    localStorage.removeItem("safeai_user");
    location.reload();
  }

  function bindAuth() {
    const setMode = (m) => {
      authMode = m;
      $("tab-login").classList.toggle("active", m === "login");
      $("tab-register").classList.toggle("active", m === "register");
      $("auth-submit").textContent = m === "login" ? "Anmelden" : "Konto erstellen";
      $("auth-error").textContent = "";
    };
    $("tab-login").onclick = () => setMode("login");
    $("tab-register").onclick = () => setMode("register");
    $("auth-form").onsubmit = async (e) => {
      e.preventDefault();
      const email = $("auth-email").value.trim(), pass = $("auth-pass").value;
      $("auth-error").textContent = "";
      try {
        const res = authMode === "login" ? await API.login(email, pass) : await API.register(email, pass);
        API.setToken(res.token);
        await afterLogin(res.user);
      } catch (err) { $("auth-error").textContent = err.message; }
    };
    $("btn-user").addEventListener("click", (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      UI.ctxMenu(r.left, r.top - 90, [
        { label: "⚡ Skills verwalten", action: openSkills },
        { label: "🔒 Abmelden", danger: true, action: logout },
      ]);
    });
  }

  /* ---------- Skills-Modal ---------- */
  async function openSkills() {
    $("modal-skills").classList.remove("hidden");
    try {
      const d = await API.skills();
      const body = $("skills-body");
      body.innerHTML = "";
      const head = document.createElement("div");
      head.className = "skill-row";
      head.innerHTML = `<span class="k">Aktive Skills gesamt</span><span class="s">${d.total_skills} (davon gelernt: ${d.learned_skills})</span>`;
      body.appendChild(head);
      for (const t of d.task_skills || []) {
        const row = document.createElement("div");
        row.className = "skill-row";
        row.innerHTML = `<span class="k">🧩 ${UI.esc(t.name)}</span><span class="s">${UI.esc(t.description)}</span>`;
        body.appendChild(row);
      }
      for (const s of d.top || []) {
        const row = document.createElement("div");
        row.className = "skill-row";
        row.innerHTML = `<span class="k">💬 ${UI.esc(s.key)}</span><span class="s">Score ${s.score} · ${s.uses}× genutzt</span>`;
        body.appendChild(row);
      }
    } catch (e) { $("skills-body").textContent = e.message; }
  }

  /* ---------- Plugins-Modal ---------- */
  async function openPlugins() {
    $("modal-plugins").classList.remove("hidden");
    const body = $("plugins-body");
    body.innerHTML = "Lade …";
    try {
      const list = await API.plugins();
      body.innerHTML = "";
      if (!list.length) body.textContent = "Keine Plugins gefunden.";
      for (const p of list) {
        const card = document.createElement("div");
        card.className = "plugin-card";
        card.innerHTML = `<b>${UI.esc(p.name)}</b> <small style="color:var(--faint)">v${UI.esc(p.version || "1.0")}</small><br>${UI.esc(p.description || "")}`;
        const acts = document.createElement("div"); acts.className = "acts";
        for (const a of (p.actions || [])) {
          const b = document.createElement("button");
          b.textContent = a;
          b.onclick = async () => {
            const val = prompt(`Parameter für „${p.name}.${a}“ (JSON oder Text):`, "{}");
            let payload; try { payload = JSON.parse(val || "{}"); } catch (_) { payload = { input: val }; }
            try {
              const res = await API.invokePlugin(p.name, a, payload);
              alert(typeof res.output === "string" ? res.output : JSON.stringify(res.output, null, 2));
            } catch (e) { UI.toast(e.message); }
          };
          acts.appendChild(b);
        }
        card.appendChild(acts);
        body.appendChild(card);
      }
    } catch (e) { body.textContent = e.message; }
  }

  /* ---------- Init ---------- */
  function bindGlobal() {
    $("btn-new-chat").onclick = () => Chat.newChat();
    $("btn-menu").onclick = () => setSidebar($("sidebar").classList.contains("collapsed"));
    $("scrim").onclick = () => setSidebar(false);
    $("btn-search").onclick = () => {
      $("search-box").classList.toggle("hidden");
      if (!$("search-box").classList.contains("hidden")) $("search-input").focus();
    };
    $("search-input").addEventListener("input", (e) => renderSessionList(e.target.value));
    $("btn-skills").onclick = openSkills;
    $("btn-plugins").onclick = openPlugins;
    $("btn-upgrade").onclick = () => UI.toast("Plus-Abo ist in dieser Instanz bereits aktiv 🙂");
    document.querySelectorAll(".modal-close").forEach(b =>
      b.onclick = () => $(b.dataset.close).classList.add("hidden"));
    document.querySelectorAll(".modal").forEach(m =>
      m.addEventListener("click", (e) => { if (e.target === m && m.id !== "modal-auth") m.classList.add("hidden"); }));

    const ta = $("prompt");
    ta.addEventListener("input", Chat.autoGrow);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); Chat.send(); }
    });
    $("btn-send").onclick = () => Chat.send();
    $("btn-stop").onclick = () => Chat.stop();
    $("btn-attach").onclick = () => $("file-input").click();
    $("file-input").addEventListener("change", (e) => { Chat.attachFiles(e.target.files); e.target.value = ""; });
    document.querySelectorAll(".sug-card").forEach(c =>
      c.addEventListener("click", () => { ta.value = c.dataset.prompt; Chat.autoGrow(); Chat.send(); }));

    // Drag & Drop Upload
    let dz = null, dragDepth = 0;
    addEventListener("dragenter", (e) => { e.preventDefault(); if (++dragDepth === 1) { dz = document.createElement("div"); dz.className = "dropzone"; dz.textContent = "Dateien hier ablegen"; document.body.appendChild(dz); } });
    addEventListener("dragover", (e) => e.preventDefault());
    addEventListener("dragleave", () => { if (--dragDepth <= 0 && dz) { dz.remove(); dz = null; dragDepth = 0; } });
    addEventListener("drop", (e) => { e.preventDefault(); if (dz) { dz.remove(); dz = null; dragDepth = 0; } if (e.dataTransfer && e.dataTransfer.files.length) Chat.attachFiles(e.dataTransfer.files); });

    addEventListener("resize", () => {
      if (innerWidth > 768) { $("scrim").classList.add("hidden"); }
    });
    document.addEventListener("click", (e) => {
      const pre = e.target.closest(".copy-code");
      if (pre) UI.copy(pre.parentElement.querySelector("code").innerText);
    });
  }

  async function init() {
    bindGlobal();
    bindAuth();
    API.setOnUnauthorized(() => { showAuth(); });
    if (API.hasToken()) {
      try {
        const me = await API.me();
        const saved = JSON.parse(localStorage.getItem("safeai_user") || "null");
        await afterLogin(saved || me);
        setInterval(pollResources, 15000);
        pollResources();
        return;
      } catch (_) { API.setToken(""); }
    }
    showAuth();
    loadModels();
  }

  document.addEventListener("DOMContentLoaded", init);
  return { currentModel, currentModelLabel, localOnly: () => localOnly,
           refreshSessions, markActive, closeSidebarOnMobile };
})();
