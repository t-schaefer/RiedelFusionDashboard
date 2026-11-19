/* In-page messages and confirmations for every page. Browsers may block or hide alert()/confirm() pop-ups
   (embedded browsers, kiosks, "prevent this page from creating more dialogs"); then a click seems to do nothing.
   - UI.toast(text, "ok"|"bad", ms)  : message in the corner (errors stay until closed)
   - UI.confirm(text, opts)         : Promise<boolean>, a dialog on the page. First line = title. Cancel has the focus.
                                      opts: { ok: "button text", danger: true|false }
   - window.alert is replaced by UI.toast, so old code keeps working (it just no longer blocks). */
(function () {
  var css = ".ui-toasts{position:fixed;right:16px;bottom:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:min(460px,calc(100vw - 32px))}" +
    ".ui-toast{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:10px;background:var(--panel,#fff);color:var(--text,#111);border:1px solid var(--line,#ccc);border-left:4px solid var(--ok-strong,#1f9d57);box-shadow:0 6px 24px rgba(0,0,0,.3);white-space:pre-wrap;font-size:14px;line-height:1.4}" +
    ".ui-toast.bad{border-left-color:var(--bad-strong,#dc3545);background:var(--bad-soft,#fde6e6)}" +
    ".ui-toast button{margin-left:auto;background:transparent;border:0;color:inherit;font-size:18px;line-height:1;cursor:pointer;padding:0 4px}" +
    ".ui-ov{position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}" +
    ".ui-box{background:var(--panel,#fff);color:var(--text,#111);border:1px solid var(--line,#ccc);border-radius:14px;padding:20px;max-width:520px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.45)}" +
    ".ui-box h3{margin:0 0 8px;font-size:17px}.ui-box .b{white-space:pre-wrap;font-size:14px;line-height:1.5;color:var(--muted,#555);max-height:50vh;overflow:auto}" +
    ".ui-box .a{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}" +
    ".ui-box button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid var(--line,#ccc);background:var(--panel-2,#f0f2f6);color:inherit;cursor:pointer}" +
    ".ui-box button.go{background:var(--accent,#2563eb);border-color:var(--accent,#2563eb);color:var(--on-accent,#fff);font-weight:600}" +
    ".ui-box button.go.danger{background:var(--bad-strong,#dc3545);border-color:var(--bad-strong,#dc3545);color:#fff}" +
    ".ui-box button:focus-visible{outline:2px solid var(--accent,#2563eb);outline-offset:2px}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  var host = null;
  function toast(text, kind, ms) {
    if (!host) { host = el("div", "ui-toasts"); document.body.appendChild(host); }
    var bad = kind === "bad", t = el("div", "ui-toast" + (bad ? " bad" : ""));
    t.setAttribute("role", bad ? "alert" : "status");
    t.appendChild(el("span", "", String(text)));
    var x = el("button", "", "×"); x.title = "Dismiss"; x.onclick = function () { t.remove(); }; t.appendChild(x);
    host.appendChild(t);
    var life = ms === undefined ? (bad ? 0 : 9000) : ms;
    if (life > 0) setTimeout(function () { t.remove(); }, life);
    return t;
  }

  function confirmBox(message, opts) {
    opts = opts || {};
    var lines = String(message).split("\n"), title = lines.shift(), rest = lines.join("\n").trim();
    var first = (title.match(/^[A-Za-z]+/) || [""])[0].toLowerCase();
    var verb = { reboot: "Reboot", remove: "Remove", reset: "Reset", clear: "Clear", set: "Apply", write: "Write", switch: "Switch on", enable: "Enable", turn: "Turn" }[first] || "Continue";
    var danger = opts.danger !== undefined ? opts.danger : first !== "remove";
    return new Promise(function (resolve) {
      var ov = el("div", "ui-ov"), box = el("div", "ui-box");
      box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true");
      box.appendChild(el("h3", "", title));
      if (rest) box.appendChild(el("div", "b", rest));
      var a = el("div", "a"), no = el("button", "", "Cancel"), yes = el("button", "go" + (danger ? " danger" : ""), opts.ok || verb);
      a.appendChild(no); a.appendChild(yes); box.appendChild(a); ov.appendChild(box); document.body.appendChild(ov);
      var prev = document.activeElement;
      function done(v) { document.removeEventListener("keydown", key, true); ov.remove(); if (prev && prev.focus) try { prev.focus(); } catch (e) {} resolve(v); }
      function key(e) {
        if (e.key === "Escape") { e.preventDefault(); done(false); }
        else if (e.key === "Tab") { e.preventDefault(); (document.activeElement === no ? yes : no).focus(); }
      }
      document.addEventListener("keydown", key, true);
      no.onclick = function () { done(false); }; yes.onclick = function () { done(true); };
      ov.addEventListener("mousedown", function (e) { if (e.target === ov) done(false); });
      no.focus();
    });
  }

  window.UI = { toast: toast, confirm: confirmBox };
  window.alert = function (m) {
    var s = String(m), bad = /^(failed|error|cannot|could not)|failed/i.test(s.slice(0, 80));
    toast(s, bad ? "bad" : "ok");
  };
})();
