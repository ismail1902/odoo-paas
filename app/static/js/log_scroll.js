/* Preserve scroll position when HTMX refreshes a live log panel. */
(function () {
  "use strict";

  var states = {};

  function logTarget(event) {
    var target = (event.detail && event.detail.target) || event.target;
    if (!target || !target.querySelector) return null;
    var log = target.querySelector(".log, .restore-log");
    return log ? { target: target, log: log } : null;
  }

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var found = logTarget(event);
    if (!found) return;
    var log = found.log;
    states[found.target.id] = {
      top: log.scrollTop,
      atBottom:
        log.scrollHeight - log.scrollTop - log.clientHeight < 32,
    };
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = (event.detail && event.detail.target) || event.target;
    if (!target || !target.id) return;
    var state = states[target.id];
    if (!state) return;
    delete states[target.id];
    var current = document.getElementById(target.id) || target;
    var log = current.querySelector(".log, .restore-log");
    if (!log) return;
    log.scrollTop = state.atBottom
      ? log.scrollHeight
      : Math.min(state.top, log.scrollHeight);
  });
})();
