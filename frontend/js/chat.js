/* ============ Chat-Logik: Nachrichten, Streaming, Feedback, Anhänge ============ */
"use strict";
const Chat = (() => {
  const $ = (id) => document.getElementById(id);
  let sessionId = null;         // aktuelle Session (public_id) oder null (= neu)
  let streaming = false;
  let aborter = null;
  let lastUserText = "";        // für "Neu generieren"
  let pendingAttachments = [];  // [{filename, modality, summary, context_text, analysis:{}, preview_base64}]

  /* ---------- Rendern ---------- */
  function bubbleEl(role, text, attachments) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    const b = document.createElement("div");
    b.style.maxWidth = "85%";
    if (role === "assistant") b.className = "bubble md";
    else { b.className = "bubble"; b.textContent = text; }
    if (role === "assistant") b.innerHTML = UI.renderMarkdown(text);
    if (attachments && attachments.length) {
      const strip = document.createElement("div");
      for (const a of attachments) {
        const chip = document.createElement("span");
        chip.className = "attach-chip";
        chip.title = a.summary || "";
        chip.textContent = (a.modality === "image" ? "🖼️" : a.modality === "audio" ? "🎵" :
          a.modality === "video" ? "🎬" : a.modality === "model3d" ? "🧊" : "📄") + " " + (a.filename || "Datei");
        strip.appendChild(chip);
      }
      b.appendChild(strip);
    }
    wrap.appendChild(b);
    return wrap;
  }

  function metaRow(el, modelTag, feedbackRef, isLastAssistant) {
    const m = document.createElement("div");
    m.className = "meta-line";
    const src = document.createElement("span");
    src.textContent = modelTag || "";
    m.appendChild(src);
    if (feedbackRef) {
      const fb = document.createElement("span");
      fb.className = "fb-btns";
      const up = document.createElement("button"); up.textContent = "👍"; up.title = "Gute Antwort";
      const dn = document.createElement("button"); dn.textContent = "👎"; dn.title = "Schlechte Antwort";
      const vote = async (good, btn) => {
        try { await API.feedback(feedbackRef, good); up.classList.remove("done"); dn.classList.remove("done");
              btn.classList.add("done"); UI.toast(good ? "Danke – wird positiv gelernt 👍" : "Danke – wird optimiert 👎"); }
        catch (e) { UI.toast(e.message); }
      };
      up.onclick = () => vote(true, up);
      dn.onclick = () => vote(false, dn);
      fb.append(up, dn); m.appendChild(fb);
    }
    if (isLastAssistant) {
      const rg = document.createElement("button");
      rg.className = "regen"; rg.textContent = "↻ Neu generieren";
      rg.onclick = () => send(lastUserText, true);
      m.appendChild(rg);
    }
    el.querySelector(".bubble").parentElement.appendChild(m);
    // eigentliche Struktur: meta unter der Bubble im selben Container
    el.lastChild.appendChild(m);
  }

  function showWelcomeIfEmpty() {
    $("welcome").classList.toggle("hidden", $("messages").children.length > 0);
  }

  function sysNote(text) {
    const n = document.createElement("div");
    n.className = "sys-note";
    n.textContent = text;
    $("messages").appendChild(n);
  }

  /* ---------- Anhang-Strip über Composer ---------- */
  function renderAttachStrip() {
    const strip = $("attach-strip");
    strip.innerHTML = "";
    strip.classList.toggle("hidden", pendingAttachments.length === 0);
    pendingAttachments.forEach((a, i) => {
      const el = document.createElement("div");
      el.className = "att";
      if (a.preview_base64) {
        const img = document.createElement("img");
        img.src = "data:image/png;base64," + a.preview_base64;
        el.appendChild(img);
      }
      const label = document.createElement("span");
      label.textContent = `${a.filename} · ${a.modality}${a.analysis_ok === false ? " (nicht lesbar)" : ""}`;
      el.appendChild(label);
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "✕";
      x.onclick = () => { pendingAttachments.splice(i, 1); renderAttachStrip(); };
      el.appendChild(x);
      strip.appendChild(el);
    });
  }

  async function attachFiles(files) {
    for (const f of Array.from(files).slice(0, 4)) {
      if (f.size > 25 * 1024 * 1024) { UI.toast(`„${f.name}“ ist größer als 25 MB`); continue; }
      UI.toast(`Analysiere „${f.name}“ …`, 1200);
      try {
        const res = await API.upload(f);
        pendingAttachments.push(res);
      } catch (e) { UI.toast("Upload fehlgeschlagen: " + e.message); }
    }
    renderAttachStrip();
  }

  /* ---------- Senden / Streamen ---------- */
  async function send(text, isRegen) {
    text = (text ?? $("prompt").value).trim();
    if (!text && !pendingAttachments.length) return;
    if (streaming) return;
    streaming = true;
    if (!isRegen) {
      lastUserText = text;
      $("prompt").value = "";
      autoGrow();
    }
    $("btn-send").classList.add("hidden");
    $("btn-stop").classList.remove("hidden");
    $("welcome").classList.add("hidden");

    const userMsg = bubbleEl("user", text, pendingAttachments.map(a => ({ filename: a.filename, modality: a.modality, summary: a.summary })));
    $("messages").appendChild(userMsg);

    const asstWrap = bubbleEl("assistant", "", []);
    const asstBubble = asstWrap.querySelector(".bubble");
    const cursor = document.createElement("span");
    cursor.className = "cursor";
    asstBubble.appendChild(cursor);
    $("messages").appendChild(asstWrap);
    scrollBottom();

    const attachments = pendingAttachments.map(a => ({
      filename: a.filename, modality: a.modality,
      analysis: { modality: a.modality, summary: a.summary },
      context_text: a.context_text
    }));
    pendingAttachments = [];
    renderAttachStrip();

    aborter = new AbortController();
    let acc = "";
    const modelTag = App.currentModelLabel();
    const localOnly = App.localOnly();
    try {
      const done = await API.stream(text, sessionId, App.currentModel(), localOnly, attachments,
        (chunk) => {
          acc += chunk;
          cursor.remove();
          asstBubble.innerHTML = UI.renderMarkdown(acc);
          asstBubble.appendChild(cursor);
          scrollBottom();
        }, aborter.signal);
      cursor.remove();
      if (!acc) { asstBubble.innerHTML = UI.renderMarkdown("(keine Antwort erhalten)"); }
      if (done.session_id) sessionId = done.session_id;
      addMeta(asstWrap, modelTag, done.feedback_ref, true);
      App.refreshSessions();
    } catch (e) {
      cursor.remove();
      if (e.name === "AbortError") {
        addMeta(asstWrap, modelTag + " · gestoppt", null, false);
      } else {
        asstBubble.innerHTML = "";
        sysNote("⚠️ " + e.message);
        asstWrap.remove();
      }
    } finally {
      streaming = false;
      aborter = null;
      $("btn-stop").classList.add("hidden");
      $("btn-send").classList.remove("hidden");
      showWelcomeIfEmpty();
      scrollBottom();
      $("prompt").focus();
    }
  }

  function addMeta(wrap, modelTag, fbRef, showRegen) {
    const m = document.createElement("div");
    m.className = "meta-line";
    m.style.gridColumn = "1/-1";
    const src = document.createElement("span");
    src.textContent = modelTag || "";
    m.appendChild(src);
    if (fbRef) {
      const fb = document.createElement("span");
      fb.className = "fb-btns";
      const up = document.createElement("button"); up.textContent = "👍"; up.title = "Gute Antwort";
      const dn = document.createElement("button"); dn.textContent = "👎"; dn.title = "Schlechte Antwort";
      const vote = async (good, btn) => {
        try { await API.feedback(fbRef, good); up.classList.remove("done"); dn.classList.remove("done");
              btn.classList.add("done"); UI.toast(good ? "Danke – wird positiv gelernt 👍" : "Notiert – wird optimiert 👎"); }
        catch (e) { UI.toast(e.message); }
      };
      up.onclick = () => vote(true, up);
      dn.onclick = () => vote(false, dn);
      fb.append(up, dn); m.appendChild(fb);
    }
    const cp = document.createElement("button");
    cp.textContent = "⧉ Kopieren"; cp.style.fontSize = "11px"; cp.style.color = "var(--faint)";
    cp.onclick = () => UI.copy(wrap.querySelector(".bubble").innerText);
    m.appendChild(cp);
    if (showRegen) {
      const rg = document.createElement("button");
      rg.className = "regen"; rg.textContent = "↻ Neu generieren";
      rg.onclick = () => {
        // letzte Assistant-Nachricht entfernen und neu senden
        const msgs = $("messages");
        if (msgs.lastElementChild === wrap) wrap.remove();
        send(lastUserText, true);
      };
      m.appendChild(rg);
    }
    const col = document.createElement("div");
    col.style.flex = "1"; col.style.minWidth = "0";
    col.appendChild(wrap.querySelector(".bubble"));
    col.appendChild(m);
    wrap.appendChild(col);
  }

  function stop() { if (aborter) aborter.abort(); }

  function scrollBottom() {
    const area = $("chat-area");
    area.scrollTop = area.scrollHeight;
  }

  /* ---------- Verlauf laden ---------- */
  async function openSession(id) {
    if (streaming) stop();
    sessionId = id;
    App.markActive(id);
    const msgs = await API.messages(id);
    const box = $("messages");
    box.innerHTML = "";
    let lastAsstIdx = -1;
    msgs.forEach((m, i) => { if (m.role === "assistant") lastAsstIdx = i; });
    msgs.forEach((m, i) => {
      const el = bubbleEl(m.role, m.content, m.attachments && m.attachments.files);
      if (m.role === "assistant") {
        box.appendChild(el);
        addMeta(el, m.model || "", null, i === lastAsstIdx && msgs.length > 0);
      } else box.appendChild(el);
    });
    $("welcome").classList.add("hidden");
    if (!msgs.length) showWelcomeIfEmpty();
    App.closeSidebarOnMobile();
    scrollBottom();
  }

  function newChat() {
    if (streaming) stop();
    sessionId = null;
    lastUserText = "";
    $("messages").innerHTML = "";
    pendingAttachments = [];
    renderAttachStrip();
    showWelcomeIfEmpty();
    App.markActive(null);
    App.closeSidebarOnMobile();
    $("prompt").focus();
  }

  function autoGrow() {
    const ta = $("prompt");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }

  return { send, stop, openSession, newChat, autoGrow, attachFiles,
           get sessionId() { return sessionId; }, set sessionId(v) { sessionId = v; } };
})();
