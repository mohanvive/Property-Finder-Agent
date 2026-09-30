// PropertyFinder web UI: a standalone client for the agent's HTTP API (server.py).
(() => {
  "use strict";

  // Defaults come from config.js, which serve.py generates from AGENT_URL / AGENT_API_KEY.
  const defaults = {
    agentUrl: "http://localhost:8000",
    apiKey: "",
    ...(window.PROPERTY_FINDER_CONFIG || {}),
  };
  const store = {
    get(key, fallback) {
      try { return localStorage.getItem(`pf.${key}`) ?? fallback; } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(`pf.${key}`, value); } catch { /* storage unavailable */ }
    },
    remove(key) {
      try { localStorage.removeItem(`pf.${key}`); } catch { /* storage unavailable */ }
    },
  };

  // A connection saved in the panel overrides the configured defaults, but only while those
  // defaults are unchanged: after AGENT_URL changes, the new configured value wins again.
  const defaultsSignature = JSON.stringify([defaults.agentUrl, defaults.apiKey]);
  if (store.get("defaults", defaultsSignature) !== defaultsSignature) {
    ["agentUrl", "apiKey", "sessionId"].forEach(store.remove);
  }
  store.set("defaults", defaultsSignature);

  const conn = {
    url: store.get("agentUrl", defaults.agentUrl),
    apiKey: store.get("apiKey", defaults.apiKey),
    healthy: false,
  };
  let sessionId = store.get("sessionId", "") || null;
  let busy = false;

  const $ = (id) => document.getElementById(id);
  const chat = $("chat"), empty = $("empty"), input = $("input"), sendBtn = $("sendBtn");

  // ---- API ---------------------------------------------------------------

  const endpoint = (path) => conn.url.replace(/\/+$/, "") + path;
  const headers = () => {
    const h = { "Content-Type": "application/json" };
    if (conn.apiKey) h.Authorization = `Bearer ${conn.apiKey}`;
    return h;
  };

  async function checkHealth() {
    setStatus("warn", "Connecting…");
    try {
      const res = await fetch(endpoint("/health"), { headers: headers() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const info = await res.json();
      conn.healthy = true;
      setStatus("ok", `Connected · ${info.model}`);
      renderConnInfo(info);
      return info;
    } catch (err) {
      conn.healthy = false;
      setStatus("err", "Not connected");
      renderConnInfo(null, `Could not reach ${conn.url}: ${err.message}. Is server.py running, and is this page's origin in AGENT_CORS_ORIGINS?`);
      return null;
    }
  }

  async function loadHistory() {
    if (!sessionId || !conn.healthy) return;
    try {
      const res = await fetch(endpoint(`/sessions/${encodeURIComponent(sessionId)}/messages`), { headers: headers() });
      if (!res.ok) return;
      const { messages } = await res.json();
      messages.forEach((m) => addMessage(m.role, m.content));
    } catch { /* history is best-effort */ }
  }

  // POST /chat/stream and parse the Server-Sent Events stream.
  async function streamChat(message, onEvent) {
    const res = await fetch(endpoint("/chat/stream"), {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ message, session_id: sessionId }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
        if (data) onEvent(JSON.parse(data));
      }
    }
  }

  // ---- Rendering ---------------------------------------------------------

  function renderMarkdown(text) {
    if (window.marked && window.DOMPurify) {
      return DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: true }));
    }
    const div = document.createElement("div");
    div.textContent = text;
    return `<p style="white-space:pre-wrap">${div.innerHTML}</p>`;
  }

  function addMessage(role, content) {
    empty.hidden = true;
    const row = document.createElement("div");
    row.className = `msg ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") bubble.textContent = content;
    else if (content) bubble.innerHTML = renderMarkdown(content);
    row.appendChild(bubble);
    chat.appendChild(row);
    scrollDown();
    return bubble;
  }

  const TOOL_LABELS = {
    getInsuranceQuoteByPropertyId: "Quoting insurance",
    getInsuranceQuoteByDetails: "Quoting insurance",
    getInsuranceAddOns: "Checking add-ons",
    compareInsuranceByState: "Comparing states",
  };
  const toolLabel = (name) => TOOL_LABELS[name] || name.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]/g, " ");

  function scrollDown() { chat.scrollTop = chat.scrollHeight; }

  function setStatus(level, text) {
    $("statusDot").className = `dot ${level}`;
    $("statusText").textContent = text;
  }

  function renderConnInfo(info, error) {
    const box = $("connInfo");
    box.innerHTML = "";
    if (error) {
      const p = document.createElement("p");
      p.className = "error";
      p.textContent = error;
      box.appendChild(p);
      return;
    }
    const p = document.createElement("p");
    p.textContent = `Model ${info.model} via ${info.llm_endpoint}. MCP servers:`;
    const ul = document.createElement("ul");
    info.mcp_servers.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<strong></strong>: <code></code>`;
      li.querySelector("strong").textContent = s.name;
      li.querySelector("code").textContent = s.tools.join(", ");
      ul.appendChild(li);
    });
    box.append(p, ul);
  }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    $("newChatBtn").disabled = value;
  }

  // ---- Chat flow ---------------------------------------------------------

  async function send(message) {
    if (busy || !message.trim()) return;
    if (!conn.healthy && !(await checkHealth())) {
      $("connPanel").hidden = false;
      return;
    }
    setBusy(true);
    addMessage("user", message);

    const bubble = addMessage("assistant", "");
    const tools = document.createElement("div");
    tools.className = "tools";
    const body = document.createElement("div");
    body.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
    bubble.append(tools, body);

    let text = "";
    const pending = [];
    let frame = 0;
    const paint = () => {
      frame = 0;
      body.innerHTML = renderMarkdown(text);
      scrollDown();
    };

    try {
      await streamChat(message, (ev) => {
        switch (ev.type) {
          case "session":
            sessionId = ev.session_id;
            store.set("sessionId", sessionId);
            break;
          case "tool_call": {
            const chip = document.createElement("span");
            chip.className = "tool";
            chip.textContent = toolLabel(ev.name);
            chip.title = ev.arguments || "";
            tools.appendChild(chip);
            pending.push({ name: ev.name, chip });
            // A new tool round means earlier text was interim; start the answer fresh.
            text = "";
            body.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
            scrollDown();
            break;
          }
          case "tool_done": {
            const i = pending.findIndex((p) => p.name === ev.name);
            const entry = i >= 0 ? pending.splice(i, 1)[0] : pending.shift();
            if (entry) entry.chip.classList.add("done");
            break;
          }
          case "delta":
            text += ev.text;
            if (!frame) frame = requestAnimationFrame(paint);
            break;
          case "done":
            text = ev.reply || text;
            paint();
            break;
          case "error":
            throw new Error(ev.message);
        }
      });
    } catch (err) {
      bubble.classList.add("error");
      body.textContent = `Error: ${err.message}`;
      if (err instanceof TypeError) checkHealth(); // network failure
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  async function newChat() {
    if (busy) return;
    if (sessionId && conn.healthy) {
      fetch(endpoint(`/sessions/${encodeURIComponent(sessionId)}`), { method: "DELETE", headers: headers() }).catch(() => {});
    }
    sessionId = null;
    store.set("sessionId", "");
    chat.querySelectorAll(".msg").forEach((el) => el.remove());
    empty.hidden = false;
    input.focus();
  }

  // ---- Wiring ------------------------------------------------------------

  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const message = input.value;
    input.value = "";
    input.style.height = "";
    send(message);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("chatForm").requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => send(chip.textContent)));
  $("newChatBtn").addEventListener("click", newChat);

  $("statusBtn").addEventListener("click", () => {
    const panel = $("connPanel");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) $("agentUrl").focus();
  });
  $("closePanel").addEventListener("click", () => { $("connPanel").hidden = true; });
  $("resetConn").addEventListener("click", () => {
    store.remove("agentUrl");
    store.remove("apiKey");
    $("agentUrl").value = defaults.agentUrl;
    $("apiKey").value = defaults.apiKey;
    $("connForm").requestSubmit();
  });
  $("connForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    conn.url = $("agentUrl").value.trim();
    conn.apiKey = $("apiKey").value.trim();
    // Only remember values that differ from the configured defaults.
    if (conn.url !== defaults.agentUrl) store.set("agentUrl", conn.url); else store.remove("agentUrl");
    if (conn.apiKey !== defaults.apiKey) store.set("apiKey", conn.apiKey); else store.remove("apiKey");
    if (await checkHealth()) {
      setTimeout(() => { $("connPanel").hidden = true; }, 1200);
      chat.querySelectorAll(".msg").forEach((el) => el.remove());
      empty.hidden = false;
      await loadHistory();
    }
  });

  $("agentUrl").value = conn.url;
  $("apiKey").value = conn.apiKey;
  checkHealth().then((ok) => {
    if (ok) loadHistory();
    else $("connPanel").hidden = false;
  });
})();
