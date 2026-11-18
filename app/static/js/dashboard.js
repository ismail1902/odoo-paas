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
