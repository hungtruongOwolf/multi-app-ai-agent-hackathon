// Incident Judge console: live refresh without a framework.
(function () {
  function relTime(iso) {
    var secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return secs + "s ago";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }

  function tick() {
    document.querySelectorAll("time[datetime]:not(.tl-time)").forEach(function (el) {
      el.textContent = relTime(el.getAttribute("datetime"));
    });
  }

  function wireRows(root) {
    (root || document).querySelectorAll("tr.rowlink").forEach(function (row) {
      row.addEventListener("click", function (ev) {
        if (ev.target.closest("a")) return;
        window.location.href = row.getAttribute("data-href");
      });
    });
  }

  var main = document.getElementById("main");
  var content = document.getElementById("content");
  var url = main && main.getAttribute("data-refresh");
  var last = content ? content.innerHTML : "";

  function refresh() {
    if (!url || document.hidden) return;
    var open = Array.prototype.map.call(document.querySelectorAll("details[open]"), function (d, i) { return i; });
    fetch(url, { headers: { "Accept": "text/html" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html && html !== last) {
          last = html;
          content.innerHTML = html;
          var details = document.querySelectorAll("details");
          open.forEach(function (i) { if (details[i]) details[i].open = true; });
          wireRows(content);
          tick();
        }
      })
      .catch(function () { /* agent restarting: keep the last good view */ });
  }

  wireRows();
  tick();
  setInterval(tick, 15000);
  if (url) setInterval(refresh, 3000);
})();
