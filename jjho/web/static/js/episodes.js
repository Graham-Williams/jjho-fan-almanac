/* Instant client-side filter for the episode browser.
 * Progressive enhancement: the page renders every episode server-side and
 * works with JS off (the form does a server-side ?q= filter). With JS on we
 * hijack the box for an instant, no-round-trip filter over title + blurb.
 * Loaded as an external file because the page CSP forbids inline scripts. */
(function () {
  "use strict";
  var input = document.getElementById("filter");
  var list = document.getElementById("episode-list");
  if (!input || !list) return;

  var form = input.closest("form");
  if (form) form.addEventListener("submit", function (e) { e.preventDefault(); });

  var rows = Array.prototype.slice.call(
    list.querySelectorAll("[data-search]"));
  var countEl = document.getElementById("result-count");
  var emptyEl = document.getElementById("no-results");

  function apply() {
    var term = input.value.trim().toLowerCase();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var hay = rows[i].getAttribute("data-search");
      var match = term === "" || hay.indexOf(term) !== -1;
      rows[i].hidden = !match;
      if (match) shown++;
    }
    if (countEl) {
      countEl.textContent = shown + (shown === 1 ? " case" : " cases");
    }
    if (emptyEl) emptyEl.hidden = shown !== 0;
  }

  var t;
  input.addEventListener("input", function () {
    clearTimeout(t);
    t = setTimeout(apply, 60);
  });
  // If arriving with a server-side ?q= already applied, sync the counter.
  apply();
})();
