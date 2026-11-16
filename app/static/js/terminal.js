/* Terminal helper: attaches an xterm.js instance to a websocket.
 *
 * Usage:
 *   <div id="terminal" data-path="/ws/shell/3" data-mode="shell"></div>
 *   (include xterm.js + addon-fit before loading this script)
 *
 * Modes:
 *   shell - interactive: keystrokes are sent as binary frames; resize events
 *           are sent as JSON text frames {"type":"resize","rows":R,"cols":C}
 *   logs  - read-only stream
 */
(function () {
  function wsUrl(path) {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host + path;
  }

  function initTerminal(el) {
    var mode = el.dataset.mode || "logs";
    var term = new window.Terminal({
      fontSize: 12.5,
      fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", monospace',
      cursorBlink: mode === "shell",
      convertEol: true,
      scrollback: 5000,
      theme: {
        background: "#000000",
        foreground: "#dce7f1",
        cursor: "#875a7b",
        selectionBackground: "#33475c",
      },
    });
    var fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(el);
    fit.fit();

    var socket = new WebSocket(wsUrl(el.dataset.path));
    socket.binaryType = "arraybuffer";

    socket.onopen = function () {
      term.writeln("\x1b[90m[connected]\x1b[0m");
      if (mode === "shell") {
        term.focus();
        sendResize();
      }
    };
    socket.onmessage = function (event) {
      term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
    };
    socket.onclose = function (event) {
      var reason = event.reason ? ": " + event.reason : "";
      term.writeln("\x1b[31m[disconnected" + reason + "]\x1b[0m");
    };

    function sendResize() {
      if (socket.readyState !== 1) return;
      socket.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    }

    if (mode === "shell") {
      term.onData(function (data) {
        if (socket.readyState !== 1) return;
        socket.send(new TextEncoder().encode(data));
      });
      term.onResize(sendResize);
      window.addEventListener("resize", function () {
        fit.fit();
        sendResize();
      });
    }
  }

  document.querySelectorAll(".terminal[data-path]").forEach(initTerminal);
})();
