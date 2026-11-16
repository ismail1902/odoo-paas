/* Real-time branch monitor: polls /api/monitor and updates Chart.js charts. */
(function () {
  "use strict";

  var root = document.querySelector(".monitor[data-branch-id]");
  if (!root) return;
  var branchId = root.dataset.branchId;
  var ChartLib = window.Chart;
  var updatedEl = document.getElementById("monitor-updated");
  if (!ChartLib) {
    if (updatedEl) updatedEl.textContent = "Chart library failed to load";
    return;
  }

  var COLORS = {
    accent: "rgba(160, 110, 146, 1)",
    accentSoft: "rgba(160, 110, 146, 0.16)",
    blue: "rgba(158, 193, 224, 1)",
    blueSoft: "rgba(158, 193, 224, 0.12)",
    green: "rgba(46, 204, 113, 1)",
    greenSoft: "rgba(46, 204, 113, 0.12)",
    red: "rgba(231, 76, 60, 1)",
    redSoft: "rgba(231, 76, 60, 0.12)",
    yellow: "rgba(241, 196, 15, 1)",
    muted: "#7d93a8",
    grid: "rgba(42, 58, 76, 0.55)",
    panel: "#17222e",
    border: "#2a3a4c",
    text: "#dce7f1",
  };

  var MAX_POINTS = 90;
  var POLL_MS = 2000;
  var labels = [];
  var containerCpu = [];
  var hostCpu = [];
  var containerMem = [];
  var hostMem = [];
  var netRx = [];
  var netTx = [];
  var diskRead = [];
  var diskWrite = [];
  var previous = null;
  var paused = false;
  var loading = false;
  var timer = null;

  ChartLib.defaults.color = COLORS.muted;
  ChartLib.defaults.font.family =
    'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  ChartLib.defaults.font.size = 11;
  ChartLib.defaults.borderColor = COLORS.grid;

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function fmtPct(value) {
    return num(value).toFixed(1) + "%";
  }

  function fmtBytes(value) {
    var n = num(value);
    if (n < 1024) return Math.round(n) + " B";
    var units = ["KB", "MB", "GB", "TB", "PB"];
    var index = -1;
    do {
      n /= 1024;
      index += 1;
    } while (n >= 1024 && index < units.length - 1);
    return (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + " " + units[index];
  }

  function fmtRate(value) {
    return fmtBytes(value) + "/s";
  }

  function timeLabel(ms) {
    return new Date(ms).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function dataset(label, data, color, fillColor, extra) {
    var item = {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: fillColor || "transparent",
      borderWidth: 2,
      pointRadius: 0,
      pointHitRadius: 8,
      tension: 0.35,
      fill: Boolean(fillColor),
      spanGaps: true,
    };
    if (extra) Object.assign(item, extra);
    return item;
  }

  function baseOptions(yTitle, y1Title) {
    var scales = {
      x: {
        grid: { display: false },
        ticks: { maxTicksLimit: 6, maxRotation: 0, autoSkip: true },
      },
      y: {
        beginAtZero: true,
        grid: { color: COLORS.grid },
        border: { display: false },
        ticks: { color: COLORS.muted },
        title: yTitle
          ? { display: true, text: yTitle, color: COLORS.muted, font: { size: 10 } }
          : undefined,
      },
    };
    if (y1Title) {
      scales.y1 = {
        position: "right",
        beginAtZero: true,
        suggestedMax: 100,
        grid: { drawOnChartArea: false },
        border: { display: false },
        ticks: { color: COLORS.muted },
        title: { display: true, text: y1Title, color: COLORS.muted, font: { size: 10 } },
      };
    }
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top",
          align: "end",
          labels: {
            color: COLORS.muted,
            boxWidth: 8,
            boxHeight: 8,
            usePointStyle: true,
            pointStyle: "circle",
            padding: 12,
          },
        },
        tooltip: {
          backgroundColor: "rgba(15, 23, 32, 0.96)",
          borderColor: COLORS.border,
          borderWidth: 1,
          titleColor: COLORS.text,
          bodyColor: COLORS.text,
          padding: 10,
          cornerRadius: 6,
          displayColors: true,
        },
      },
      scales: scales,
    };
  }

  function makeLineChart(id, datasets, yTitle, y1Title) {
    var canvas = document.getElementById(id);
    if (!canvas) return null;
    return new ChartLib(canvas.getContext("2d"), {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: baseOptions(yTitle, y1Title),
    });
  }

  var cpuChart = makeLineChart(
    "chart-cpu",
    [
      dataset("Container CPU", containerCpu, COLORS.accent, COLORS.accentSoft),
      dataset("Host CPU", hostCpu, COLORS.blue, null, { borderDash: [5, 4] }),
    ],
    null,
    null
  );
  if (cpuChart) {
    cpuChart.options.scales.y.suggestedMax = 100;
    cpuChart.options.scales.y.ticks.callback = function (value) {
      return value + "%";
    };
    cpuChart.options.plugins.tooltip.callbacks = {
      label: function (ctx) {
        return ctx.dataset.label + ": " + fmtPct(ctx.parsed.y);
      },
    };
  }

  var memoryChart = makeLineChart(
    "chart-memory",
    [
      dataset("Container RAM", containerMem, COLORS.green, COLORS.greenSoft),
      dataset("Host RAM", hostMem, COLORS.yellow, null, {
        borderDash: [5, 4],
        yAxisID: "y1",
      }),
    ],
    "Container MB",
    "Host %"
  );
  if (memoryChart) {
    memoryChart.options.scales.y.ticks.callback = function (value) {
      return Math.round(value) + " MB";
    };
    memoryChart.options.scales.y1.ticks.callback = function (value) {
      return value + "%";
    };
    memoryChart.options.plugins.tooltip.callbacks = {
      label: function (ctx) {
        return (
          ctx.dataset.label +
          ": " +
          (ctx.dataset.yAxisID === "y1"
            ? fmtPct(ctx.parsed.y)
            : Math.round(num(ctx.parsed.y)) + " MB")
        );
      },
    };
  }

  var networkChart = makeLineChart(
    "chart-network",
    [
      dataset("Received", netRx, COLORS.blue, COLORS.blueSoft),
      dataset("Sent", netTx, COLORS.accent, COLORS.accentSoft),
    ],
    "bytes / s",
    null
  );
  if (networkChart) {
    networkChart.options.scales.y.ticks.callback = function (value) {
      return fmtRate(value);
    };
    networkChart.options.plugins.tooltip.callbacks = {
      label: function (ctx) {
        return ctx.dataset.label + ": " + fmtRate(ctx.parsed.y);
      },
    };
  }

  var diskIoChart = makeLineChart(
    "chart-diskio",
    [
      dataset("Read", diskRead, COLORS.green, COLORS.greenSoft),
      dataset("Write", diskWrite, COLORS.red, COLORS.redSoft),
    ],
    "bytes / s",
    null
  );
  if (diskIoChart) {
    diskIoChart.options.scales.y.ticks.callback = function (value) {
      return fmtRate(value);
    };
    diskIoChart.options.plugins.tooltip.callbacks = {
      label: function (ctx) {
        return ctx.dataset.label + ": " + fmtRate(ctx.parsed.y);
      },
    };
  }

  var initialDiskUsed = num(root.dataset.diskUsed);
  var initialDiskTotal = num(root.dataset.diskTotal);
  var initialDiskPercent = num(root.dataset.diskPercent);
  var diskCanvas = document.getElementById("chart-disk");
  var diskChart = diskCanvas
    ? new ChartLib(diskCanvas.getContext("2d"), {
        type: "doughnut",
        data: {
          labels: ["Used", "Free"],
          datasets: [
            {
              data: [initialDiskUsed, Math.max(0, initialDiskTotal - initialDiskUsed)],
              backgroundColor: [
                initialDiskPercent >= 90
                  ? COLORS.red
                  : initialDiskPercent >= 75
                  ? COLORS.yellow
                  : COLORS.green,
                COLORS.border,
              ],
              borderWidth: 0,
              hoverOffset: 0,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "74%",
          animation: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "rgba(15, 23, 32, 0.96)",
              borderColor: COLORS.border,
              borderWidth: 1,
              callbacks: {
                label: function (ctx) {
                  return ctx.label + ": " + fmtBytes(ctx.raw);
                },
              },
            },
          },
        },
      })
    : null;

  function text(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function setStatus(message, state) {
    text("monitor-updated", message);
    if (state) text("monitor-insight-state", state);
  }

  function updateKpis(stats) {
    var host = stats.host || {};
    var container = stats.container;
    var state = stats.state || "unknown";
    text("kpi-state", state);
    var stateEl = document.getElementById("kpi-state");
    if (stateEl) stateEl.className = "kpi-value state-" + state;
    text(
      "kpi-state-sub",
      container ? num(container.pids) + " processes" : "container not running"
    );
    text("kpi-container-cpu", container ? fmtPct(container.cpu_percent) : "—");
    text("kpi-container-cpu-sub", container ? "docker stats" : "no container stats");
    text("kpi-container-ram", container ? fmtBytes(container.mem_used) : "—");
    var share =
      container && num(host.mem_total)
        ? (num(container.mem_used) / num(host.mem_total)) * 100
        : 0;
    text(
      "kpi-container-ram-sub",
      container ? share.toFixed(1) + "% of host RAM" : "—"
    );
    text("kpi-host-cpu", fmtPct(host.cpu_percent));
    text(
      "kpi-host-cpu-sub",
      (num(host.cpu_count) || 1) +
        " vCPU · load " +
        num(host.load_1).toFixed(2)
    );
    text("kpi-host-ram", fmtPct(host.mem_percent));
    text(
      "kpi-host-ram-sub",
      fmtBytes(host.mem_used) + " / " + fmtBytes(host.mem_total)
    );
    text("kpi-disk", fmtPct(host.disk_percent));
    text(
      "kpi-disk-sub",
      fmtBytes(num(host.disk_total) - num(host.disk_used)) + " free"
    );
  }

  function updateDisk(host) {
    var used = num(host.disk_used);
    var total = num(host.disk_total);
    var percent = num(host.disk_percent);
    if (diskChart) {
      diskChart.data.datasets[0].data = [used, Math.max(0, total - used)];
      diskChart.data.datasets[0].backgroundColor = [
        percent >= 90 ? COLORS.red : percent >= 75 ? COLORS.yellow : COLORS.green,
        COLORS.border,
      ];
      diskChart.update("none");
    }
    text("disk-center-value", Math.round(percent) + "%");
  }

  function renderInsights(stats) {
    var host = stats.host || {};
    var container = stats.container;
    var items = [];
    if (!container) {
      items.push({
        tone: "warn",
        text: "Container is not running; only host metrics are shown.",
      });
    } else {
      var cpu = num(container.cpu_percent);
      items.push({
        tone: cpu >= 80 ? "warn" : "good",
        text:
          "Container CPU is " +
          (cpu >= 80 ? "high" : cpu >= 40 ? "moderate" : "low") +
          " at " +
          fmtPct(cpu) +
          ".",
      });
      var share = num(host.mem_total)
        ? (num(container.mem_used) / num(host.mem_total)) * 100
        : 0;
      items.push({
        tone: share >= 80 ? "warn" : "info",
        text:
          "Container uses " +
          fmtBytes(container.mem_used) +
          " of RAM (" +
          share.toFixed(1) +
          "% of host).",
      });
      if (num(container.pids)) {
        items.push({
          tone: "info",
          text: "Container has " + num(container.pids) + " running processes.",
        });
      }
    }
    var load = num(host.load_1);
    var cpus = num(host.cpu_count) || 1;
    items.push({
      tone: load > cpus * 0.9 ? "warn" : "info",
      text:
        "Host load average is " +
        load.toFixed(2) +
        " across " +
        cpus +
        " vCPU.",
    });
    var hostMem = num(host.mem_percent);
    if (hostMem >= 90) {
      items.push({
        tone: "warn",
        text: "Host memory is " + fmtPct(hostMem) + " used.",
      });
    }
    var disk = num(host.disk_percent);
    items.push({
      tone: disk >= 85 ? "warn" : "info",
      text:
        "Data disk is " +
        fmtPct(disk) +
        " used; " +
        fmtBytes(num(host.disk_total) - num(host.disk_used)) +
        " free.",
    });
    if (num(host.swap_total)) {
      items.push({
        tone: num(host.swap_percent) >= 50 ? "warn" : "info",
        text: "Swap is " + fmtPct(host.swap_percent) + " used.",
      });
    }
    var list = document.getElementById("monitor-insights");
    if (!list) return;
    list.innerHTML = "";
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.className = "insight " + item.tone;
      li.textContent = item.text;
      list.appendChild(li);
    });
  }

  function pushPoint(stats, now) {
    var host = stats.host || {};
    var container = stats.container;
    labels.push(timeLabel(now));
    containerCpu.push(num(container && container.cpu_percent));
    hostCpu.push(num(host.cpu_percent));
    containerMem.push(num(container && container.mem_used) / 1048576);
    hostMem.push(num(host.mem_percent));

    var rx = 0;
    var tx = 0;
    var read = 0;
    var write = 0;
    if (container) {
      if (previous) {
        var seconds = Math.max(0.001, (now - previous.time) / 1000);
        rx = Math.max(0, (num(container.net_rx) - previous.rx) / seconds);
        tx = Math.max(0, (num(container.net_tx) - previous.tx) / seconds);
        read = Math.max(0, (num(container.blk_read) - previous.read) / seconds);
        write = Math.max(0, (num(container.blk_write) - previous.write) / seconds);
      }
      previous = {
        time: now,
        rx: num(container.net_rx),
        tx: num(container.net_tx),
        read: num(container.blk_read),
        write: num(container.blk_write),
      };
    } else {
      previous = null;
    }
    netRx.push(rx);
    netTx.push(tx);
    diskRead.push(read);
    diskWrite.push(write);

    while (labels.length > MAX_POINTS) {
      labels.shift();
      containerCpu.shift();
      hostCpu.shift();
      containerMem.shift();
      hostMem.shift();
      netRx.shift();
      netTx.shift();
      diskRead.shift();
      diskWrite.shift();
    }
  }

  function render(stats, now) {
    updateKpis(stats);
    pushPoint(stats, now);
    updateDisk(stats.host || {});
    renderInsights(stats);
    [cpuChart, memoryChart, networkChart, diskIoChart].forEach(function (chart) {
      if (chart) {
        chart.data.labels = labels;
        chart.update("none");
      }
    });
    setStatus("updated " + timeLabel(now) + " · every 2s", "live");
  }

  function schedule() {
    clearTimeout(timer);
    if (!paused && !document.hidden) {
      timer = setTimeout(load, POLL_MS);
    }
  }

  function load() {
    if (paused || loading || document.hidden) return;
    loading = true;
    fetch("/api/monitor/" + encodeURIComponent(branchId), {
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (data.error) throw new Error(data.error);
        render(data, Date.now());
      })
      .catch(function (error) {
        setStatus("connection lost — retrying (" + error.message + ")", "error");
      })
      .finally(function () {
        loading = false;
        schedule();
      });
  }

  var toggle = document.getElementById("monitor-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      paused = !paused;
      root.classList.toggle("paused", paused);
      toggle.textContent = paused ? "Resume" : "Pause";
      if (paused) {
        clearTimeout(timer);
        setStatus("paused", "paused");
      } else {
        previous = null;
        setStatus("resuming…", "live");
        load();
      }
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(timer);
    } else if (!paused) {
      previous = null;
      load();
    }
  });

  load();
})();
