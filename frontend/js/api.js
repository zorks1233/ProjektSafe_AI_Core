/* ============ SafeAI API-Client (Auth, Fetch, SSE-Streaming, Upload) ============ */
"use strict";
const API = (() => {
  const TOKEN_KEY = "safeai_token";
  let token = localStorage.getItem(TOKEN_KEY) || "";
  let onUnauthorized = null;

  function setToken(t) {
    token = t || "";
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  }
  const hasToken = () => !!token;

  async function request(method, path, body, isForm) {
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    let payload;
    if (isForm) payload = body;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    const res = await fetch(path, { method, headers, body: payload });
    if (res.status === 401 && !path.startsWith("/api/auth")) {
      setToken("");
      if (onUnauthorized) onUnauthorized();
      throw new Error("Sitzung abgelaufen – bitte neu anmelden");
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      let msg = (data && (typeof data.detail === "string" ? data.detail : (data.detail && data.detail.detail))) || ("Fehler " + res.status);
      if (msg.includes && msg.includes("Sicherheitsfilter")) msg = "Anfrage vom Sicherheitsfilter blockiert";
      throw new Error(msg);
    }
    return data;
  }

  return {
    setToken, get token() { return token; }, hasToken,
    setOnUnauthorized(cb) { onUnauthorized = cb; },

    register: (email, password) => request("POST", "/api/auth/register", { email, password }),
    login:    (email, password) => request("POST", "/api/auth/login",    { email, password }),
    me:       () => request("GET", "/api/auth/me"),

    sessions:     () => request("GET", "/api/sessions"),
    newSession:   (title) => request("POST", "/api/sessions", { title: title || "New chat" }),
    sessionAction:(id, action, extra) => request("POST", `/api/sessions/${id}/action`, Object.assign({ action }, extra || {})),
    messages:     (id) => request("GET", `/api/sessions/${id}/messages`),

    chat: (message, sessionId, model, localOnly, attachments) =>
      request("POST", "/api/chat", { message, session_id: sessionId, model, local_only: localOnly, attachments }),

    /* SSE-Stream über fetch (POST möglich, AbortController zum Stoppen).
       onChunk(textStück), liefert done-Objekt {session_id, feedback_ref, message_id}. */
    stream: async (message, sessionId, model, localOnly, attachments, onChunk, signal) => {
      const headers = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = "Bearer " + token;
      const res = await fetch("/api/chat/stream", {
        method: "POST", headers, signal,
        body: JSON.stringify({ message, session_id: sessionId, model, local_only: localOnly, attachments })
      });
      if (res.status === 401) { setToken(""); if (onUnauthorized) onUnauthorized(); throw new Error("Nicht angemeldet"); }
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        throw new Error((d && typeof d.detail === "string" && d.detail.includes("blockiert")) ? "Anfrage vom Sicherheitsfilter blockiert" : "Fehler " + res.status);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "", doneInfo = null, error = null;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const p of parts) {
          if (!p.startsWith("data:")) continue;
          try {
            const obj = JSON.parse(p.slice(5).trim());
            if (obj.c) onChunk(obj.c);
            else if (obj.error) error = obj.error;
            else if (obj.done) doneInfo = obj;
          } catch (_) { /* unvollständiges Fragment ignorieren */ }
        }
      }
      if (error) throw new Error(error);
      return doneInfo || {};
    },

    upload: (file) => {
      const fd = new FormData();
      fd.append("file", file);
      return request("POST", "/api/upload", fd, true);
    },

    models:    () => request("GET", "/api/models"),
    resources: () => request("GET", "/api/resources"),
    plugins:   () => request("GET", "/api/plugins"),
    invokePlugin: (plugin, action, payload) => request("POST", "/api/plugins/invoke", { plugin, action, payload }),
    skills:    () => request("GET", "/api/skills"),
    teach:     (trigger, response) => request("POST", "/api/skills/teach", { trigger, response }),
    feedback:  (ref, good) => request("POST", "/api/feedback", { ref, good }),
  };
})();
