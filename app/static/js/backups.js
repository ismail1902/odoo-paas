/* Database import dialog on the branch Backups tab. */
(function () {
  "use strict";

  var dialog = document.getElementById("import-backup-dialog");
  if (!dialog) return;

  var openers = document.querySelectorAll("[data-open-import-backup]");
  var closers = document.querySelectorAll("[data-close-import-backup]");
  var form = dialog.querySelector("form");

  function openDialog() {
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

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
  if (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-import-confirm");
      if (message && !window.confirm(message)) event.preventDefault();
    });
  }
})();
