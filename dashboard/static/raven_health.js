/**
 * Raven Health details page — lightweight SVG gauges, charts, and live updates.
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var REFRESH_MS = 5000;
  var lastGoodData = null;
  var refreshFailed = false;

  var COLORS = {
    accent: "#58a6ff",
    ok: "#3fb950",
    warn: "#d29922",
    fail: "#f85149",
    purple: "#bc8cff",
    muted: "#8b949e",
    user: "#58a6ff",
    system: "#bc8cff",
    nice: "#3fb950",
    idle: "#2d3a4f",
    used: "#58a6ff",
    cached: "#bc8cff",
    free: "#3fb950"
  };

  var initialNode = document.getElementById("raven-health-initial-data");
  if (initialNode && initialNode.textContent) {
    try {
      lastGoodData = JSON.parse(initialNode.textContent);
      REFRESH_MS = (lastGoodData.refresh_seconds || 5) * 1000;
    } catch (_err) {
      lastGoodData = null;
    }
  }

  function num(value, fallback) {
    var parsed = Number(value);
    return isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) {
      node.textContent = value == null || value === "" ? "—" : String(value);
    }
  }

  function setTexts(ids, value) {
    ids.forEach(function (id) { setText(id, value); });
  }

  function emptyState(container, message) {
    if (!container) return;
    container.innerHTML = "";
    var empty = document.createElement("div");
    empty.className = "viz-empty";
    empty.textContent = message || "No data yet.";
    container.appendChild(empty);
  }

  function getHistory(data) {
    var history = (data && data.history) || {};
    return {
      cpu: history.cpu_history_1h || history.cpu_1h || [],
      load: history.load_history_1h || history.load_1h || [],
      memory: history.memory_history_1h || history.memory_1h || [],
      network: history.network_history_1h || history.network_1h || []
    };
  }

  function renderDonutGauge(container, percent, options) {
    options = options || {};
    if (!container) return;
    container.innerHTML = "";

    var value = clamp(num(percent, 0), 0, 100);
    var size = options.size || 110;
    var stroke = options.stroke || 10;
    var radius = (size - stroke) / 2;
    var center = size / 2;
    var circumference = 2 * Math.PI * radius;
    var dash = (value / 100) * circumference;
    var color = options.color || COLORS.accent;

    if (value >= 90) color = COLORS.fail;
    else if (value >= 75) color = COLORS.warn;
    else if (options.color) color = options.color;

    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + size + " " + size);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", (options.label || "Gauge") + " " + value.toFixed(0) + "%");

    var track = document.createElementNS(SVG_NS, "circle");
    track.setAttribute("cx", String(center));
    track.setAttribute("cy", String(center));
    track.setAttribute("r", String(radius));
    track.setAttribute("fill", "none");
    track.setAttribute("stroke", "#2d3a4f");
    track.setAttribute("stroke-width", String(stroke));
    svg.appendChild(track);

    var arc = document.createElementNS(SVG_NS, "circle");
    arc.setAttribute("cx", String(center));
    arc.setAttribute("cy", String(center));
    arc.setAttribute("r", String(radius));
    arc.setAttribute("fill", "none");
    arc.setAttribute("stroke", color);
    arc.setAttribute("stroke-width", String(stroke));
    arc.setAttribute("stroke-linecap", "round");
    arc.setAttribute("transform", "rotate(-90 " + center + " " + center + ")");
    arc.setAttribute("stroke-dasharray", dash + " " + (circumference - dash));
    svg.appendChild(arc);

    var label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", String(center));
    label.setAttribute("y", String(center - 2));
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "gauge-center");
    label.textContent = options.centerText || (value.toFixed(0) + "%");
    svg.appendChild(label);

    if (options.subText) {
      var sub = document.createElementNS(SVG_NS, "text");
      sub.setAttribute("x", String(center));
      sub.setAttribute("y", String(center + 16));
      sub.setAttribute("text-anchor", "middle");
      sub.setAttribute("class", "gauge-sub");
      sub.textContent = options.subText;
      svg.appendChild(sub);
    }

    container.appendChild(svg);
  }

  function renderLegend(container, items) {
    if (!container) return;
    container.innerHTML = "";
    if (!items || !items.length) {
      emptyState(container, "No breakdown available.");
      return;
    }
    items.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "legend-item";
      row.innerHTML =
        '<span><span class="legend-dot" style="background:' + (item.color || COLORS.accent) + '"></span>' +
        item.label + "</span><strong>" + item.value + "</strong>";
      container.appendChild(row);
    });
  }

  function renderSparkline(container, data, options) {
    options = options || {};
    if (!container) return;
    container.innerHTML = "";
    if (!data || !data.length) {
      emptyState(container, options.emptyText || "No load history yet.");
      return;
    }

    var width = options.width || 280;
    var height = options.height || 72;
    var padding = 6;
    var values = data.map(function (row) { return num(row.value, 0); });
    var maxVal = Math.max.apply(null, values.concat([0.01]));
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.title || "Sparkline");

    var points = [];
    data.forEach(function (row, index) {
      var x = padding + (index / Math.max(data.length - 1, 1)) * (width - padding * 2);
      var y = height - padding - (num(row.value, 0) / maxVal) * (height - padding * 2);
      points.push(x + "," + y);
    });

    var line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", options.color || COLORS.accent);
    line.setAttribute("stroke-width", "2");
    svg.appendChild(line);
    container.appendChild(svg);
  }

  function renderAreaChart(container, data, options) {
    options = options || {};
    if (!container) return;
    container.innerHTML = "";

    if (!data || !data.length) {
      emptyState(container, options.emptyText || "No history data yet.");
      return;
    }

    var width = options.width || 420;
    var height = options.height || 150;
    var padding = { top: 14, right: 10, bottom: 28, left: 36 };
    var plotW = width - padding.left - padding.right;
    var plotH = height - padding.top - padding.bottom;
    var values = data.map(function (row) { return num(row.value, 0); });
    var maxVal = Math.max.apply(null, values.concat([options.max || 0.01]));
    if (options.max) maxVal = Math.max(maxVal, options.max);

    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.title || "History chart");

    var baseline = padding.top + plotH;
    var grid = document.createElementNS(SVG_NS, "line");
    grid.setAttribute("x1", String(padding.left));
    grid.setAttribute("x2", String(width - padding.right));
    grid.setAttribute("y1", String(baseline));
    grid.setAttribute("y2", String(baseline));
    grid.setAttribute("class", "chart-grid");
    svg.appendChild(grid);

    var points = [];
    var areaPoints = [padding.left + "," + baseline];
    data.forEach(function (row, index) {
      var x = padding.left + (index / Math.max(data.length - 1, 1)) * plotW;
      var y = padding.top + plotH - (num(row.value, 0) / maxVal) * plotH;
      points.push(x + "," + y);
      areaPoints.push(x + "," + y);
    });
    areaPoints.push((padding.left + plotW) + "," + baseline);

    var area = document.createElementNS(SVG_NS, "polygon");
    area.setAttribute("points", areaPoints.join(" "));
    area.setAttribute("fill", options.color || COLORS.accent);
    area.setAttribute("class", "chart-area");
    svg.appendChild(area);

    var line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", points.join(" "));
    line.setAttribute("class", "chart-line");
    line.setAttribute("stroke", options.color || COLORS.accent);
    svg.appendChild(line);

    var first = data[0];
    var last = data[data.length - 1];
    var startLabel = document.createElementNS(SVG_NS, "text");
    startLabel.setAttribute("x", String(padding.left));
    startLabel.setAttribute("y", String(height - 8));
    startLabel.setAttribute("class", "chart-axis-label");
    startLabel.textContent = (first && first.label) || "";
    svg.appendChild(startLabel);

    var endLabel = document.createElementNS(SVG_NS, "text");
    endLabel.setAttribute("x", String(width - padding.right));
    endLabel.setAttribute("y", String(height - 8));
    endLabel.setAttribute("class", "chart-axis-label");
    endLabel.setAttribute("text-anchor", "end");
    endLabel.textContent = (last && last.label) || "";
    svg.appendChild(endLabel);

    container.appendChild(svg);
  }

  function progressClass(percent) {
    if (percent >= 90) return "fail";
    if (percent >= 75) return "warn";
    if (percent >= 50) return "";
    return "ok";
  }

  function renderProgressList(container, rows, emptyText) {
    if (!container) return;
    container.innerHTML = "";
    if (!rows || !rows.length) {
      emptyState(container, emptyText || "No data available.");
      return;
    }
    rows.forEach(function (row) {
      var wrap = document.createElement("div");
      wrap.className = "progress-row" + (row.emphasis ? " emphasis" : "");
      var percent = clamp(num(row.percent, 0), 0, 100);
      wrap.innerHTML =
        '<div class="progress-head"><span>' + row.label + '</span><strong>' +
        (row.display || percent.toFixed(0) + "%") + '</strong></div>' +
        '<div class="progress-bar"><div class="progress-fill ' + (row.tone || progressClass(percent)) +
        '" style="width:' + percent + '%"></div></div>';
      container.appendChild(wrap);
    });
  }

  function renderCoreBars(container, cores) {
    if (!container) return;
    container.innerHTML = "";
    if (!cores || !cores.length) {
      emptyState(container, "No per-core data available.");
      return;
    }
    cores.forEach(function (core) {
      var percent = clamp(num(core.cpu_percent, 0), 0, 100);
      var row = document.createElement("div");
      row.className = "core-bar-row";
      row.innerHTML =
        "<span>Core " + core.core + '</span>' +
        '<div class="progress-bar"><div class="progress-fill" style="width:' + percent + '%"></div></div>' +
        "<strong>" + (core.cpu_percent_display || percent.toFixed(0) + "%") + "</strong>";
      container.appendChild(row);
    });
  }

  function renderProcessRows(container, processes) {
    if (!container) return;
    container.innerHTML = "";
    if (!processes || !processes.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.innerHTML = '<td colspan="3">No process data available.</td>';
      container.appendChild(emptyRow);
      return;
    }
    processes.forEach(function (proc) {
      var row = document.createElement("tr");
      row.innerHTML =
        '<td class="process-name" title="' + (proc.name || "") + '">' + (proc.name || "unknown") + "</td>" +
        '<td><span class="metric-chip">' + (proc.cpu_percent_display || "—") + "</span></td>" +
        "<td>" + (proc.memory_percent_display || "—") + "</td>";
      container.appendChild(row);
    });
  }

  function renderNetworkTable(container, interfaces) {
    if (!container) return;
    container.innerHTML = "";
    if (!interfaces || !interfaces.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.innerHTML = '<td colspan="4">No network data available.</td>';
      container.appendChild(emptyRow);
      return;
    }
    interfaces.forEach(function (iface) {
      var row = document.createElement("tr");
      row.innerHTML =
        "<td>" + iface.name + "</td>" +
        "<td>" + (iface.bytes_recv_display || "—") + "</td>" +
        "<td>" + (iface.bytes_sent_display || "—") + "</td>" +
        "<td>" + (iface.speed_mbps ? iface.speed_mbps + " Mbps" : "—") + "</td>";
      container.appendChild(row);
    });
  }

  function renderContainerList(container, containers) {
    if (!container) return;
    if (!containers || !containers.length) {
      emptyState(container, "No container details available.");
      return;
    }
    renderProgressList(
      container,
      containers.slice(0, 8).map(function (item) {
        var running = String(item.status || "").toLowerCase().indexOf("run") >= 0;
        return {
          label: item.name,
          display: item.status || "unknown",
          percent: running ? 100 : 35,
          tone: running ? "ok" : "warn"
        };
      })
    );
  }

  function tempTone(celsius) {
    if (celsius >= 90) return "fail";
    if (celsius >= 75) return "warn";
    return "ok";
  }

  function sensorRows(sensors) {
    if (!sensors || !sensors.length) return [];
    var maxTemp = Math.max.apply(null, sensors.map(function (s) { return num(s.value_celsius, 0); }).concat([1]));
    return sensors.map(function (sensor) {
      var value = num(sensor.value_celsius, 0);
      return {
        label: sensor.label || "sensor",
        display: sensor.value_display || value.toFixed(0) + "°C",
        percent: clamp((value / Math.max(maxTemp, 90)) * 100, 0, 100),
        tone: tempTone(value),
        emphasis: !!sensor.is_highest
      };
    });
  }

  function diskRows(disks) {
    return (disks || []).map(function (disk) {
      return {
        label: disk.mount + " · " + disk.device,
        display: (disk.percent_display || "—") + (disk.used_display ? " · " + disk.used_display : ""),
        percent: num(disk.percent, 0),
        tone: progressClass(num(disk.percent, 0))
      };
    });
  }

  function updateBanner(data, stale) {
    var banner = document.getElementById("glances-banner");
    var badge = document.getElementById("glances-status-badge");
    if (badge) {
      if (stale) {
        badge.textContent = "Stale";
        badge.className = "status-badge stale";
      } else {
        badge.textContent = data.glances_available ? "Live" : "Unavailable";
        badge.className = "status-badge " + (data.status || "unavailable");
      }
    }
    if (banner) {
      if (data.glances_available && !stale) banner.classList.add("hidden");
      else banner.classList.remove("hidden");
    }
  }

  function applyDetails(data, stale) {
    if (!data) return;

    updateBanner(data, stale);
    setText("updated-at", data.updated_at);

    var overview = data.overview || {};
    var cpu = overview.cpu || data.cpu || {};
    var load = overview.load || {};
    var memory = overview.memory || data.memory || {};
    var swap = overview.swap || data.swap || {};
    var system = overview.system || data.system || {};
    var containers = overview.containers || data.containers || {};
    var history = getHistory(data);
    var breakdown = cpu.breakdown || {};

    var cpuPercent = num(cpu.total_percent, 0);
    renderDonutGauge(document.getElementById("gauge-cpu"), cpuPercent, {
      label: "CPU Usage",
      subText: (cpu.core_count || cpu.cpu_threads || "—") + " cores"
    });
    renderDonutGauge(document.getElementById("gauge-cpu-detail"), cpuPercent, {
      label: "CPU Usage",
      subText: (cpu.cpu_threads || "—") + " threads"
    });

    renderLegend(document.getElementById("cpu-legend"), [
      { label: "User", value: breakdown.user_display || "—", color: COLORS.user },
      { label: "System", value: breakdown.system_display || "—", color: COLORS.system },
      { label: "Nice", value: breakdown.nice_display || "—", color: COLORS.nice },
      { label: "Idle", value: breakdown.idle_display || "—", color: COLORS.idle }
    ].filter(function (item) { return item.value !== "—"; }));
    renderLegend(document.getElementById("cpu-legend-detail"), [
      { label: "Total", value: cpu.total_display || "—", color: COLORS.accent },
      { label: "User", value: breakdown.user_display || "—", color: COLORS.user },
      { label: "System", value: breakdown.system_display || "—", color: COLORS.system },
      { label: "Idle", value: breakdown.idle_display || "—", color: COLORS.idle }
    ].filter(function (item) { return item.value !== "—"; }));

    renderSparkline(document.getElementById("sparkline-load"), history.load, {
      title: "Load sparkline",
      emptyText: "No load history for the last hour.",
      color: COLORS.purple
    });

    setText("load-1", load.load_1 != null ? load.load_1.toFixed(2) : "—");
    setText("load-5", load.load_5 != null ? load.load_5.toFixed(2) : "—");
    setText("load-15", load.load_15 != null ? load.load_15.toFixed(2) : "—");

    var memPercent = num(memory.percent, 0);
    renderDonutGauge(document.getElementById("gauge-memory"), memPercent, { label: "Memory Usage", color: COLORS.accent });
    renderDonutGauge(document.getElementById("gauge-memory-detail"), memPercent, { label: "Memory Usage", color: COLORS.accent });
    renderLegend(document.getElementById("memory-legend"), [
      { label: "Used", value: memory.percent_display || "—", color: COLORS.used },
      { label: "Cached", value: memory.cached_display || "—", color: COLORS.cached },
      { label: "Free", value: memory.free_display || "—", color: COLORS.free },
      { label: "Total", value: memory.total_display || "—", color: COLORS.muted }
    ]);
    renderLegend(document.getElementById("memory-legend-detail"), [
      { label: "Used", value: memory.percent_display || "—", color: COLORS.used },
      { label: "Summary", value: memory.summary || "—", color: COLORS.accent },
      { label: "Free", value: memory.free_display || "—", color: COLORS.free },
      { label: "Cached", value: memory.cached_display || "—", color: COLORS.cached }
    ]);

    var swapPercent = num(swap.percent, 0);
    renderDonutGauge(document.getElementById("gauge-swap"), swapPercent, { label: "Swap Usage", color: COLORS.purple });
    renderDonutGauge(document.getElementById("gauge-swap-detail"), swapPercent, { label: "Swap Usage", color: COLORS.purple });
    renderLegend(document.getElementById("swap-legend"), [
      { label: "Used", value: swap.percent_display || "—", color: COLORS.purple },
      { label: "Used / Total", value: [swap.used_display, swap.total_display].filter(Boolean).join(" / ") || "—", color: COLORS.accent },
      { label: "Free", value: swap.free_display || "—", color: COLORS.free }
    ]);
    renderLegend(document.getElementById("swap-legend-detail"), [
      { label: "Used", value: swap.percent_display || "—", color: COLORS.purple },
      { label: "Summary", value: swap.summary || "—", color: COLORS.accent },
      { label: "Free", value: swap.free_display || "—", color: COLORS.free }
    ]);

    var containerPercent = num(containers.percent, containers.total ? (100 * containers.running / containers.total) : 0);
    renderDonutGauge(document.getElementById("gauge-containers"), containerPercent, {
      label: "Containers running",
      color: COLORS.ok,
      centerText: String(containers.running == null ? "—" : containers.running),
      subText: "running"
    });
    renderLegend(document.getElementById("containers-legend"), [
      { label: "Running", value: containers.running == null ? "—" : String(containers.running), color: COLORS.ok },
      { label: "Total", value: containers.total == null ? "—" : String(containers.total), color: COLORS.accent },
      { label: "Share", value: containers.percent_display || "—", color: COLORS.purple }
    ]);

    renderProgressList(
      document.getElementById("overview-temps"),
      sensorRows(overview.temperatures || data.sensors),
      "No temperature sensors available."
    );
    renderProgressList(
      document.getElementById("sensors-list"),
      sensorRows(data.sensors),
      "No temperature sensors available."
    );

    renderProgressList(
      document.getElementById("overview-disks"),
      diskRows(overview.disks || data.disks),
      "No filesystem data available."
    );
    renderProgressList(
      document.getElementById("disks-list"),
      diskRows(data.disks),
      "No filesystem data available."
    );

    renderProcessRows(
      document.getElementById("overview-processes-body"),
      overview.top_processes || (data.processes || []).slice(0, 5)
    );
    renderProcessRows(document.getElementById("processes-body"), data.processes);
    renderCoreBars(document.getElementById("cpu-per-core"), cpu.per_core || (data.cpu && data.cpu.per_core) || []);
    renderNetworkTable(document.getElementById("network-body"), data.network);
    renderContainerList(document.getElementById("system-containers-list"), data.docker);

    setTexts(["system-host", "system-host-detail"], system.hostname);
    setTexts(["system-uptime", "system-uptime-detail"], system.uptime);
    setTexts(["system-threads", "system-threads-detail"], system.cpu_threads);
    setTexts(["system-os", "system-os-detail"], system.os);
    setTexts(["system-kernel-detail"], system.kernel);
    setTexts(["system-containers-detail"], containers.running == null ? "—" : containers.running + " running");

    renderAreaChart(document.getElementById("chart-cpu-1h"), history.cpu, {
      title: "CPU usage 1h",
      emptyText: "No CPU history for the last hour.",
      color: COLORS.accent,
      max: 100
    });
    renderAreaChart(document.getElementById("chart-load-1h"), history.load, {
      title: "Load average 1h",
      emptyText: "No load history for the last hour.",
      color: COLORS.purple
    });
    renderAreaChart(document.getElementById("chart-memory-1h"), history.memory, {
      title: "Memory usage 1h",
      emptyText: "No memory history for the last hour.",
      color: COLORS.ok,
      max: 100
    });
    renderAreaChart(document.getElementById("chart-network-1h"), history.network, {
      title: "Network I/O 1h",
      emptyText: "Network history not available yet.",
      color: COLORS.purple
    });
    renderAreaChart(document.getElementById("chart-network-detail"), history.network, {
      title: "Network I/O 1h",
      emptyText: "Network history not available yet.",
      color: COLORS.purple
    });
  }

  function refreshDetails() {
    fetch("/api/raven/health/glances", { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("refresh failed");
        return response.json();
      })
      .then(function (data) {
        refreshFailed = false;
        lastGoodData = data;
        applyDetails(data, false);
      })
      .catch(function () {
        refreshFailed = true;
        if (lastGoodData) applyDetails(lastGoodData, true);
        else updateBanner({ glances_available: false, status: "unavailable" }, true);
      });
  }

  if (lastGoodData) applyDetails(lastGoodData, false);
  window.setInterval(refreshDetails, REFRESH_MS);
})();
