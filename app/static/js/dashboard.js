/* New-project modal on the dashboard. */
(function () {
  "use strict";

  var dialog = document.getElementById("new-project-dialog");
  if (!dialog) return;
  var openers = document.querySelectorAll("[data-open-new-project]");
  var closers = document.querySelectorAll("[data-close-new-project]");
  var slugInput = dialog.querySelector('input[name="slug"]');

  function openDialog() {
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    if (slugInput) slugInput.focus();
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
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && dialog.hasAttribute("open")) closeDialog();
  });
})();
