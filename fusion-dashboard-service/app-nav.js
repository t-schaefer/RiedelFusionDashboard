/* Top navigation shared by Dashboard, New device and Expert config. Put <div id="appNav"></div> first in <body>. */
(function () {
  var host = document.getElementById("appNav");
  if (!host) return;
  var path = location.pathname.replace(/\/+$/, "") || "/";
  var here = path === "/config/advanced" ? "/config" : path;
  var items = [["/", "Dashboard"], ["/guide", "New device"], ["/config", "Expert config"]];
  function esc(t) { return String(t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function draw(mode) {
    var h = '<nav class="appnav"><a class="brand" href="/">Fusion Dashboard</a>';
    items.forEach(function (i) { h += '<a class="nl' + (here === i[0] ? " active" : "") + '" href="' + i[0] + '">' + i[1] + "</a>"; });
    (mode.links || []).forEach(function (l) {
      h += '<a class="nl" target="_blank" rel="noopener" href="' + esc(l.url.replace("{host}", location.hostname)) + '">' + esc(l.label) + "</a>";
    });
    h += '<span class="sp"></span>';
    if (mode.demo) h += '<span class="pill" title="Simulated devices - nothing real is touched' + (mode.resetMinutes ? ". The demo starts over about every " + mode.resetMinutes + " minutes." : "") + '">DEMO</span>';
    if (mode.auth && (here === "/config" || here === "/guide")) h += '<a class="out" href="/config/logout">Log out</a>';
    host.innerHTML = h + "</nav>";
  }
  draw({});
  fetch("/api/mode", { cache: "no-store" }).then(function (r) { return r.json(); }).then(draw).catch(function () {});
})();
