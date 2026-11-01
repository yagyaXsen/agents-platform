/*
 * AgentGrid chat widget. Embed on any website:
 *
 *   <script src="https://YOUR-HOST/static/widget.js" data-key="pk_..." async></script>
 *
 * Optional: data-api="https://YOUR-HOST" (defaults to the script's origin),
 *           data-accent="#0f766e", data-open="true"
 * Renders inside a shadow root so the host page's CSS can't break it.
 */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script || !script.dataset.key) {
    console.warn("[AgentGrid] widget.js needs a data-key attribute");
    return;
  }
  var KEY = script.dataset.key;
  var API = (script.dataset.api || new URL(script.src).origin).replace(/\/$/, "");
  var STORE = "agentgrid:" + KEY;
  var accent = script.dataset.accent || "#111827";

  var convId = null, lastId = 0, status = "bot", pollTimer = null, busy = false, started = false;
  try { convId = localStorage.getItem(STORE); } catch (e) {}

  // ---------- DOM
  var host = document.createElement("div");
  host.id = "agentgrid-widget";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    '<style>' +
    ':host{all:initial}' +
    '*{box-sizing:border-box;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}' +
    '.fab{position:fixed;right:20px;bottom:20px;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.18);display:flex;align-items:center;justify-content:center;z-index:2147483000}' +
    '.fab svg{width:26px;height:26px}' +
    '.panel{position:fixed;right:20px;bottom:90px;width:370px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;box-shadow:0 16px 48px rgba(0,0,0,.2);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;border:1px solid #e5e7eb}' +
    '.panel.open{display:flex}' +
    '.head{padding:14px 16px;color:#fff;display:flex;align-items:center;justify-content:space-between}' +
    '.head b{font-size:15px;display:block}.head small{font-size:12px;opacity:.85}' +
    '.x{background:transparent;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1}' +
    '.banner{display:none;background:#fef3c7;color:#92400e;font-size:12.5px;padding:8px 14px}' +
    '.banner.on{display:block}' +
    '.log{flex:1;overflow-y:auto;padding:14px;background:#f8fafc;display:flex;flex-direction:column;gap:8px}' +
    '.m{max-width:84%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.45;white-space:normal;word-wrap:break-word}' +
    '.user{align-self:flex-end;color:#fff;border-bottom-right-radius:4px}' +
    '.bot,.agent{align-self:flex-start;background:#fff;color:#111827;border:1px solid #e5e7eb;border-bottom-left-radius:4px}' +
    '.agent{border-color:#f59e0b}' +
    '.who{font-size:11px;color:#6b7280;margin:0 0 2px 4px;align-self:flex-start}' +
    '.system{align-self:center;font-size:12px;color:#6b7280;background:transparent;padding:2px}' +
    '.src{display:block;margin-top:6px;font-size:11.5px;color:#6b7280}' +
    '.typing{align-self:flex-start;font-size:12px;color:#6b7280;padding:4px 6px}' +
    'form{display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb;background:#fff}' +
    'input{flex:1;border:1px solid #d1d5db;border-radius:10px;padding:10px 12px;font-size:14px;outline:none}' +
    'input:focus{border-color:' + accent + '}' +
    'button.send{border:0;border-radius:10px;padding:0 14px;color:#fff;font-weight:600;cursor:pointer}' +
    'button.send:disabled{opacity:.6;cursor:wait}' +
    '.foot{font-size:10.5px;color:#9ca3af;text-align:center;padding:0 0 8px;background:#fff}' +
    '</style>' +
    '<button class="fab" aria-label="Open chat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>' +
    '<section class="panel" role="dialog" aria-label="Chat">' +
    '  <div class="head"><div><b class="title">Assistant</b><small>Usually replies instantly</small></div><button class="x" aria-label="Close">&times;</button></div>' +
    '  <div class="banner">You\'re now chatting with a member of the team.</div>' +
    '  <div class="log" aria-live="polite"></div>' +
    '  <form><input name="t" autocomplete="off" placeholder="Type your message" maxlength="2000" aria-label="Message"><button class="send" type="submit">Send</button></form>' +
    '  <div class="foot">AI assistant · type "human" to reach the team</div>' +
    '</section>';

  var $ = function (s) { return root.querySelector(s); };
  var fab = $(".fab"), panel = $(".panel"), log = $(".log"), form = $("form"), input = $("input"),
      sendBtn = $(".send"), banner = $(".banner"), title = $(".title"), head = $(".head");

  function paint(color) {
    accent = color || accent;
    fab.style.background = accent; head.style.background = accent; sendBtn.style.background = accent;
  }
  paint(accent);

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(msgs) {
    var typing = log.querySelector(".typing");
    if (typing) typing.remove();
    msgs.forEach(function (m) {
      if (m.id <= lastId) return;
      lastId = m.id;
      if (m.role === "agent") {
        var who = document.createElement("div"); who.className = "who"; who.textContent = "Team";
        log.appendChild(who);
      }
      var el = document.createElement("div");
      el.className = "m " + m.role;
      var text = esc(m.text);
      var parts = text.split(/\n\nSource: /);
      var html = parts[0].replace(/\n/g, "<br>");
      if (parts[1]) html += '<span class="src">Source: ' + parts[1] + "</span>";
      el.innerHTML = html;
      if (m.role === "user") el.style.background = accent;
      log.appendChild(el);
    });
    log.scrollTop = log.scrollHeight;
  }

  function note(text) {
    var typing = log.querySelector(".typing");
    if (typing) typing.remove();
    var el = document.createElement("div");
    el.className = "m system"; el.textContent = text;
    log.appendChild(el); log.scrollTop = log.scrollHeight;
  }

  function setStatus(s) {
    status = s;
    banner.classList.toggle("on", s === "handover");
  }

  function api(method, path, body) {
    return fetch(API + path, {
      method: method,
      headers: { "Content-Type": "application/json", "X-Agent-Key": KEY },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      if (!r.ok) {
        var err = new Error("HTTP " + r.status);
        err.status = r.status; err.retryAfter = Number(r.headers.get("Retry-After")) || 0;
        throw err;
      }
      return r.json();
    });
  }

  function startConversation() {
    return api("POST", "/v1/conversations").then(function (d) {
      convId = d.conversation_id;
      try { localStorage.setItem(STORE, convId); } catch (e) {}
      setStatus(d.status); render(d.messages);
    });
  }

  function restore() {
    return api("GET", "/v1/conversations/" + convId + "/messages?after_id=0")
      .then(function (d) { setStatus(d.status); render(d.messages); })
      .catch(function () { convId = null; return startConversation(); });
  }

  function poll() {
    if (!convId || busy) return;
    api("GET", "/v1/conversations/" + convId + "/messages?after_id=" + lastId)
      .then(function (d) { setStatus(d.status); render(d.messages); })
      .catch(function () {});
  }

  function open() {
    panel.classList.add("open");
    input.focus();
    if (!started) {
      started = true;
      api("GET", "/v1/agent").then(function (a) {
        title.textContent = a.name;
        if (a.theme && a.theme.accent && !script.dataset.accent) paint(a.theme.accent);
      }).catch(function () {});
      (convId ? restore() : startConversation()).catch(function () {
        started = false; note("Chat is unavailable right now. Please try again later.");
      });
    }
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, 4000);
  }

  function close() {
    panel.classList.remove("open");
    clearInterval(pollTimer);
  }

  fab.addEventListener("click", function () { panel.classList.contains("open") ? close() : open(); });
  $(".x").addEventListener("click", close);

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || busy || !convId) return;
    busy = true; sendBtn.disabled = true; input.value = "";
    var tmp = document.createElement("div");
    tmp.className = "m user"; tmp.style.background = accent; tmp.style.opacity = ".6"; tmp.textContent = text;
    log.appendChild(tmp);
    if (status !== "handover") {
      var t = document.createElement("div"); t.className = "typing"; t.textContent = "Typing…"; log.appendChild(t);
    }
    log.scrollTop = log.scrollHeight;
    api("POST", "/v1/conversations/" + convId + "/messages?after_id=" + lastId, { text: text })
      .then(function (d) { tmp.remove(); setStatus(d.status); render(d.messages); })
      .catch(function (err) {
        if (err && err.status === 429) {
          tmp.remove(); input.value = text;  // keep the text so they can resend it
          note("You're sending messages quickly. Please wait " + (err.retryAfter || 30) + " seconds and send again.");
          return;
        }
        tmp.style.opacity = "1";
        note("Message not sent. Check your connection and try again.");
      })
      .finally(function () { busy = false; sendBtn.disabled = false; input.focus(); });
  });

  if (script.dataset.open === "true") open();
})();
