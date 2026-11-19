/* New-project modal on the dashboard. */
(function () {
  "use strict";

  var dialog = document.getElementById("new-project-dialog");
  if (!dialog) return;
  var openers = document.querySelectorAll("[data-open-new-project]");
  var closers = document.querySelectorAll("[data-close-new-project]");
  var slugInput = dialog.querySelector('input[name="slug"]');
  var odooSelect = dialog.querySelector('select[name="odoo_version"]');
  var postgresSelect = dialog.querySelector('select[name="postgres_version"]');

  function syncPostgresOptions() {
    if (!odooSelect || !postgresSelect) return;
    var odooVersion = odooSelect.value;
    var first = null;
    Array.prototype.forEach.call(postgresSelect.options, function (option) {
      var versions = (option.getAttribute("data-odoo") || "").split(/\s+/);
      var allowed = versions.indexOf(odooVersion) !== -1;
      option.hidden = !allowed;
      option.style.display = allowed ? "" : "none";
      option.disabled = !allowed;
      if (allowed && !first) first = option;
    });
    var selected = postgresSelect.options[postgresSelect.selectedIndex];
    if ((!selected || selected.disabled) && first) first.selected = true;
  }

  function openDialog() {
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    syncPostgresOptions();
    if (slugInput) slugInput.focus();
  }

  if (odooSelect) odooSelect.addEventListener("change", syncPostgresOptions);
  syncPostgresOptions();

  function closeDialog() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  Array.prototype.forEach.call(openers, function (button) {
    button.addEventListener("click", openDialog);
  });
  Array.prototype.forEach.call(closers, function (button) {
    button.addEventListener("click", closeDialog);
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) closeDialog();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && dialog.hasAttribute("open")) closeDialog();
  });
})();

/* Project search and grid/list view. */
(function () {
  "use strict";

  var input = document.getElementById("project-search");
  var grid = document.querySelector("[data-project-grid]");
  if (!input || !grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll("[data-project-card]"));
  var count = document.getElementById("project-count");
  var empty = document.getElementById("project-search-empty");
  var viewButtons = document.querySelectorAll("[data-project-view]");
  var storageKey = "odoo-paas-project-view";

  function applyFilter() {
    var query = input.value.trim().toLowerCase();
    var visible = 0;
    cards.forEach(function (card) {
      var haystack = (card.getAttribute("data-search") || "").toLowerCase();
      var match = !query || haystack.indexOf(query) !== -1;
      card.hidden = !match;
      if (match) visible += 1;
    });
    if (count) {
      count.textContent = query ? visible + " of " + cards.length : cards.length + " total";
    }
    if (empty) empty.hidden = !query || visible !== 0;
  }

  function setView(view, persist) {
    var list = view === "list";
    grid.classList.toggle("project-list", list);
    Array.prototype.forEach.call(viewButtons, function (button) {
      var active = button.getAttribute("data-project-view") === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (persist) {
      try {
        localStorage.setItem(storageKey, view);
      } catch (error) {
        /* Storage can be unavailable in private browsing. */
      }
    }
  }

  input.addEventListener("input", applyFilter);
  input.addEventListener("search", applyFilter);
  input.addEventListener("change", applyFilter);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && input.value) {
      input.value = "";
      applyFilter();
    }
  });
  Array.prototype.forEach.call(viewButtons, function (button) {
    button.addEventListener("click", function () {
      setView(button.getAttribute("data-project-view"), true);
    });
  });

  var savedView = "grid";
  try {
    savedView = localStorage.getItem(storageKey) || "grid";
  } catch (error) {
    /* Storage can be unavailable in private browsing. */
  }
  setView(savedView === "list" ? "list" : "grid", false);
  applyFilter();
})();
