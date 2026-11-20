/* Backup dialogs: create options and import upload progress. */
(function () {
  "use strict";

  function showDialog(dialog) {
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function hideDialog(dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function bindDialog(dialog, openerSelector, closerSelector, onOpen) {
    if (!dialog) return;
    var openers = document.querySelectorAll(openerSelector);
    var closers = dialog.querySelectorAll(closerSelector);

    function open(event) {
      if (event) event.preventDefault();
      if (onOpen) onOpen();
      showDialog(dialog);
    }

    function close() {
      hideDialog(dialog);
    }

    Array.prototype.forEach.call(openers, function (button) {
      button.addEventListener("click", open);
    });
    Array.prototype.forEach.call(closers, function (button) {
      button.addEventListener("click", close);
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) close();
    });
  }

  var createDialog = document.getElementById("create-backup-dialog");
  var createForm = createDialog ? createDialog.querySelector("form") : null;
  bindDialog(
    createDialog,
    "[data-open-create-backup]",
    "[data-close-create-backup]",
    function () {
      if (createForm) createForm.reset();
    }
  );
  if (createForm) {
    createForm.addEventListener("submit", function () {
      var submit = createForm.querySelector("[data-create-submit]");
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Creating backup…";
      }
    });
  }

  var dialog = document.getElementById("import-backup-dialog");
  if (!dialog) return;

  var form = dialog.querySelector("form");
  var fields = dialog.querySelector("[data-import-fields]");
  var progress = dialog.querySelector("[data-import-progress]");
  var phase = dialog.querySelector("[data-import-phase]");
  var percent = dialog.querySelector("[data-import-percent]");
  var bar = dialog.querySelector("[data-import-bar]");
  var bytes = dialog.querySelector("[data-import-bytes]");
  var speed = dialog.querySelector("[data-import-speed]");
  var error = dialog.querySelector("[data-import-error]");
  var submit = dialog.querySelector("[data-import-submit]");
  var cancel = dialog.querySelector("[data-import-cancel]");
  var openers = document.querySelectorAll("[data-open-import-backup]");
  var closers = document.querySelectorAll("[data-close-import-backup]");
  var uploading = false;
  var xhr = null;

  function formatBytes(value) {
    if (!value || value < 1) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    var amount = value / Math.pow(1024, index);
    return amount.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  function resetUpload() {
    if (xhr) {
      xhr = null;
    }
    uploading = false;
    if (fields) fields.hidden = false;
    if (progress) progress.hidden = true;
    if (error) error.hidden = true;
    if (submit) submit.disabled = false;
    if (cancel) {
      cancel.textContent = "Cancel";
      cancel.disabled = false;
    }
    if (bar) bar.style.width = "0%";
    if (percent) percent.textContent = "0%";
    if (bytes) bytes.textContent = "0 B / 0 B";
    if (speed) speed.textContent = "—";
    if (phase) phase.textContent = "Uploading backup…";
  }

  function openDialog() {
    if (form) form.reset();
    resetUpload();
    showDialog(dialog);
  }

  function closeDialog() {
    hideDialog(dialog);
  }

  function abortUpload() {
    if (xhr) xhr.abort();
    resetUpload();
    closeDialog();
  }

  function failUpload(message) {
    uploading = false;
    if (fields) fields.hidden = false;
    if (progress) progress.hidden = true;
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
    if (submit) submit.disabled = false;
    if (cancel) {
      cancel.textContent = "Cancel";
      cancel.disabled = false;
    }
  }

  function startUpload() {
    var input = form.querySelector('input[type="file"]');
    var file = input && input.files && input.files[0];
    if (file && file.size > 2 * 1024 * 1024 * 1024) {
      failUpload("The selected file is larger than the 2 GB limit.");
      return;
    }
    var message = form.getAttribute("data-import-confirm");
    if (message && !window.confirm(message)) return;
    if (!form.reportValidity()) return;

    uploading = true;
    if (fields) fields.hidden = true;
    if (progress) progress.hidden = false;
    if (error) error.hidden = true;
    if (submit) submit.disabled = true;
    if (cancel) {
      cancel.textContent = "Cancel upload";
      cancel.disabled = false;
    }
    if (bar) bar.style.width = "0%";
    if (percent) percent.textContent = "0%";
    if (bytes) bytes.textContent = "0 B / 0 B";
    if (speed) speed.textContent = "—";
    if (phase) phase.textContent = "Uploading backup…";

    var started = Date.now();
    xhr = new XMLHttpRequest();
    xhr.open("POST", form.action, true);
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) {
        if (percent) percent.textContent = "…";
        return;
      }
      var ratio = event.total ? event.loaded / event.total : 0;
      var elapsed = Math.max((Date.now() - started) / 1000, 0.1);
      var rate = event.loaded / elapsed;
      var remaining = rate > 0 ? Math.max(0, (event.total - event.loaded) / rate) : 0;
      if (bar) bar.style.width = (ratio * 100).toFixed(1) + "%";
      if (percent) percent.textContent = Math.floor(ratio * 100) + "%";
      if (bytes) bytes.textContent = formatBytes(event.loaded) + " / " + formatBytes(event.total);
      if (speed) {
        speed.textContent = formatBytes(rate) + "/s · ~" + Math.ceil(remaining) + "s left";
      }
    };
    xhr.upload.onload = function () {
      if (percent) percent.textContent = "100%";
      if (bar) bar.style.width = "100%";
      if (speed) speed.textContent = "Upload complete";
      if (phase) phase.textContent = "Starting restore…";
      if (cancel) cancel.disabled = true;
    };
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 400) {
        window.location.href = xhr.responseURL || window.location.href;
      } else {
        failUpload("Upload failed (HTTP " + xhr.status + ").");
      }
    };
    xhr.onerror = function () {
      failUpload("Upload failed. Check your connection and try again.");
    };
    xhr.onabort = function () {
      if (uploading) {
        resetUpload();
        closeDialog();
      }
    };
    xhr.send(new FormData(form));
  }

  Array.prototype.forEach.call(openers, function (button) {
    button.addEventListener("click", openDialog);
  });
  Array.prototype.forEach.call(closers, function (button) {
    button.addEventListener("click", function () {
      if (uploading) {
        if (cancel && !cancel.disabled) abortUpload();
        return;
      }
      closeDialog();
    });
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog && !uploading) closeDialog();
  });
  dialog.addEventListener("cancel", function (event) {
    if (uploading) event.preventDefault();
  });
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!uploading) startUpload();
    });
  }
})();
