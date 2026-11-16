/* Theme preference: system, light or dark. Persisted in localStorage. */
(function () {
  "use strict";

  var KEY = "paas-theme";
  var root = document.documentElement;
  var media = window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: light)")
    : null;

  function storedMode() {
    try {
      var value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (error) {
      return "system";
    }
  }

  function resolvedTheme(mode) {
    if (mode === "light" || mode === "dark") return mode;
    return media && media.matches ? "light" : "dark";
  }

  function syncButtons(mode) {
    var buttons = document.querySelectorAll("[data-theme-choice]");
    Array.prototype.forEach.call(buttons, function (button) {
      var active = button.getAttribute("data-theme-choice") === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function apply() {
    var mode = storedMode();
    var theme = resolvedTheme(mode);
    root.setAttribute("data-theme", theme);
    root.style.colorScheme = theme;
    syncButtons(mode);
    window.dispatchEvent(
      new CustomEvent("paas-theme-change", {
        detail: { mode: mode, theme: theme },
      })
    );
  }

  function setMode(mode) {
    try {
      localStorage.setItem(KEY, mode);
    } catch (error) {
      /* localStorage can be unavailable in private mode */
    }
    apply();
  }

  apply();

  document.addEventListener("DOMContentLoaded", function () {
    var buttons = document.querySelectorAll("[data-theme-choice]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        setMode(button.getAttribute("data-theme-choice") || "system");
      });
    });
    syncButtons(storedMode());
  });

  if (media) {
    var onChange = function () {
      if (storedMode() === "system") apply();
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else if (media.addListener) media.addListener(onChange);
  }

  window.addEventListener("storage", function (event) {
    if (event.key === KEY) apply();
  });
})();
