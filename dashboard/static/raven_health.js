/**
 * Raven Health details page — lightweight SVG gauges, charts, and live updates.
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var REFRESH_MS = 5000;
  var lastGoodData = null;
  var refreshFailed = false;
  var chartRange = "1h";
  var PROCESS_NAME_MAX = 28;

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

  function getHistory(data, range) {
    range = range || chartRange || "1h";
    var history = (data && data.history) || {};
    function series(metric) {
      return history[metric + "_history_" + range]
        || history[metric + "_" + range]
        || (range === "1h" ? (history[metric + "_history_1h"] || history[metric + "_1h"]) : [])
        || [];
    }
    return {
      cpu: series("cpu"),
      load: series("load"),
      memory: series("memory"),
      network: series("network"),
      range: range,
      collecting: !!history.collecting,
      sampleCount: history.sample_count_1h || 0,
      collectingLabel: history.collecting_label || "Collecting history — check back in a few minutes"
    };
  }

  function truncateName(name) {
    name = name || "unknown";
    if (name.length <= PROCESS_NAME_MAX) return name;
    return name.slice(0, PROCESS_NAME_MAX - 1) + "…";
  }

  function diskTone(percent) {
    if (percent > 85) return "fail";
    if (percent >= 70) return "warn";
    return "ok";
  }

  function historyEmptyText(history, fallback) {
    if (history && history.collecting) {
      return history.collectingLabel || "Collecting history — check back in a few minutes";
    }
    return fallback || "No history data yet.";
  }

  function emptyStateWithLive(container, message, liveLine) {
    if (!container) return;
    container.innerHTML = "";
    var empty = document.createElement("div");
    empty.className = "viz-empty";
    empty.textContent = message || "No data yet.";
    container.appendChild(empty);
    if (liveLine) {
      var live = document.createElement("div");
      live.className = "viz-live-now";
      live.textContent = "Current: " + liveLine;
      container.appendChild(live);
    }
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
    svg.style.width = "100%";
    svg.style.height = "100%";

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
      emptyStateWithLive(
        container,
        options.emptyText || "No load history yet.",
        options.liveText
      );
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
    svg.style.width = "100%";
    svg.style.height = "auto";

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
      emptyStateWithLive(
        container,
        options.emptyText || "No history data yet.",
        options.liveText
      );
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
    svg.style.width = "100%";
    svg.style.height = "auto";

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

  function renderDiskList(container, disks, emptyText) {
    if (!container) return;
    container.innerHTML = "";
    var rows = diskRows(disks);
    if (!rows.length) {
      emptyState(container, emptyText || "No filesystem data available.");
      return;
    }
    var currentGroup = null;
    rows.forEach(function (row) {
      if (row.group !== currentGroup) {
        currentGroup = row.group;
        var title = document.createElement("div");
        title.className = "disk-group-title" + (currentGroup === "storage" ? " storage" : "");
        title.textContent = currentGroup === "storage" ? "Storage volumes" : "System volumes";
        container.appendChild(title);
      }
      var wrap = document.createElement("div");
      wrap.className = "progress-row" + (row.isStorage ? " storage-volume" : "");
      var percent = clamp(num(row.percent, 0), 0, 100);
      wrap.innerHTML =
        '<div class="progress-head"><span>' + row.label + '</span><strong>' +
        row.display + '</strong></div>' +
        '<div class="progress-bar"><div class="progress-fill ' + (row.tone || diskTone(percent)) +
        '" style="width:' + percent + '%"></div></div>';
      container.appendChild(wrap);
    });
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

  function renderProcessTable(container, processes, options) {
    options = options || {};
    if (!container) return;
    container.innerHTML = "";
    if (!processes || !processes.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.innerHTML = '<td colspan="3">No process data available.</td>';
      container.appendChild(emptyRow);
      return;
    }
    var maxCpu = Math.max.apply(null, processes.map(function (p) { return num(p.cpu_percent, 0); }).concat([1]));
    var maxMem = Math.max.apply(null, processes.map(function (p) { return num(p.memory_percent, 0); }).concat([1]));
    processes.forEach(function (proc, index) {
      var cpu = num(proc.cpu_percent, 0);
      var mem = num(proc.memory_percent, 0);
      var row = document.createElement("tr");
      if (index === 0) row.className = "process-row-top";
      var cpuWidth = clamp((cpu / maxCpu) * 100, 0, 100);
      var memWidth = clamp((mem / maxMem) * 100, 0, 100);
      row.innerHTML =
        '<td class="process-name" title="' + (proc.name || "") + '">' + truncateName(proc.name) + "</td>" +
        '<td class="inline-bar-cell"><div class="inline-bar"><div class="inline-bar-track"><div class="inline-bar-fill" style="width:' +
        cpuWidth + '%"></div></div><span>' + (proc.cpu_percent_display || "—") + "</span></div></td>" +
        '<td class="inline-bar-cell"><div class="inline-bar"><div class="inline-bar-track"><div class="inline-bar-fill mem" style="width:' +
        memWidth + '%"></div></div><span>' + (proc.memory_percent_display || "—") + "</span></div></td>";
      container.appendChild(row);
    });
    var summaryEl = document.getElementById(options.summaryId);
    if (summaryEl) {
      var shown = processes.length;
      var total = options.totalCount != null ? options.totalCount : shown;
      summaryEl.textContent = "Showing " + shown + " of " + total + " processes · sorted by CPU";
    }
  }

  function renderProcessRows(container, processes) {
    renderProcessTable(container, processes, {});
  }

  function renderNetworkTable(container, interfaces) {
    if (!container) return;
    container.innerHTML = "";
    if (!interfaces || !interfaces.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.innerHTML = '<td colspan="5">No network data available.</td>';
      container.appendChild(emptyRow);
      return;
    }
    interfaces.forEach(function (iface) {
      var row = document.createElement("tr");
      row.innerHTML =
        "<td>" + iface.name + "</td>" +
        "<td>" + (iface.bytes_recv_display || "—") + "</td>" +
        "<td>" + (iface.bytes_sent_display || "—") + "</td>" +
        "<td>" + (iface.rx_rate_display || "—") + "</td>" +
        "<td>" + (iface.tx_rate_display || "—") + "</td>" +
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

  function sensorSummaryRows(sensors) {
    if (!sensors || !sensors.length) return [];
    var maxTemp = Math.max.apply(null, sensors.map(function (s) { return num(s.value_celsius, 0); }).concat([1]));
    return sensors.map(function (sensor) {
      var value = num(sensor.value_celsius, 0);
      return {
        label: sensor.label || "sensor",
        display: sensor.value_display || value.toFixed(0) + "°C",
        percent: clamp((value / Math.max(maxTemp, 90)) * 100, 0, 100),
        tone: tempTone(value),
        emphasis: sensor.label === "Highest System Temp"
      };
    });
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

  function sensorRawRows(sensors) {
    return sensorRows(sensors || []);
  }

  function renderSensorPanels(summaryContainer, rawContainer, summarySensors, rawSensors) {
    renderProgressList(
      summaryContainer,
      sensorSummaryRows(summarySensors),
      "No temperature sensors available."
    );
    if (rawContainer) {
      renderProgressList(
        rawContainer,
        sensorRawRows(rawSensors),
        "No additional sensors."
      );
    }
  }

  function diskRows(disks) {
    return (disks || []).map(function (disk) {
      var percent = num(disk.percent, 0);
      var usedTotal = [disk.used_display, disk.total_display].filter(Boolean).join(" / ");
      var display = (disk.percent_display || "—") + (usedTotal ? " · " + usedTotal : "");
      return {
        label: disk.mount || "—",
        display: display,
        percent: percent,
        tone: diskTone(percent),
        group: disk.is_storage || disk.category === "storage" ? "storage" : "system",
        isStorage: !!(disk.is_storage || disk.category === "storage")
      };
    });
  }

  function renderSummaryBanner(data) {
    var banner = document.getElementById("health-summary");
    if (!banner) return;
    var summary = (data && data.summary) || {};
    var items = [
      { label: "CPU", value: summary.cpu_display || "—" },
      { label: "RAM", value: summary.memory_display || "—" },
      { label: "Temp", value: summary.temp_display || "—" },
      { label: "Load", value: summary.load_display || "—" },
      { label: "Containers", value: summary.containers_display || "—" }
    ];
    banner.innerHTML = items.map(function (item) {
      return '<span class="health-summary-item"><span>' + item.label + '</span><strong>' + item.value + "</strong></span>";
    }).join("");
  }

  function renderSystemLines(container, system) {
    if (!container || !system) return;
    var lines = system.display_lines || [];
    if (!lines.length) {
      lines = [
        system.hostname ? "Host: " + system.hostname : null,
        system.os ? "OS: " + system.os : null,
        system.kernel ? "Kernel: " + system.kernel : null
      ].filter(Boolean);
    }
    container.innerHTML = lines.map(function (line) {
      var parts = line.split(": ");
      if (parts.length >= 2) {
        return "<div><span>" + parts[0] + ":</span> " + parts.slice(1).join(": ") + "</div>";
      }
      return "<div>" + line + "</div>";
    }).join("");
  }

  function renderFooterMeta(data) {
    var footer = document.getElementById("footer-meta");
    if (!footer) return;
    var meta = (data && data.meta) || {};
    var parts = [
      meta.glances_version ? "Glances " + meta.glances_version : null,
      meta.build_git_commit ? "Dashboard " + meta.build_git_commit : null,
      meta.last_updated ? "Updated " + meta.last_updated : (data.updated_at ? "Updated " + data.updated_at : null)
    ].filter(Boolean);
    footer.textContent = parts.join(" · ");
  }

  function syncChartRangeButtons() {
    document.querySelectorAll(".chart-range-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-range") === chartRange);
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
    renderSummaryBanner(data);
    renderFooterMeta(data);
    setText("updated-at", data.updated_at);

    var overview = data.overview || {};
    var cpu = overview.cpu || data.cpu || {};
    var load = overview.load || {};
    var memory = overview.memory || data.memory || {};
    var swap = overview.swap || data.swap || {};
    var system = overview.system || data.system || {};
    var containers = overview.containers || data.containers || {};
    var history = getHistory(data, chartRange);
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
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: load.load_1 != null ? load.load_1.toFixed(2) + " (1 min)" : null,
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

    renderSensorPanels(
      document.getElementById("overview-temps"),
      document.getElementById("overview-temps-raw"),
      overview.temperatures || data.sensors,
      overview.temperatures_raw || data.sensors_raw
    );
    renderSensorPanels(
      document.getElementById("sensors-list"),
      document.getElementById("sensors-raw-list"),
      data.sensors,
      data.sensors_raw
    );

    renderDiskList(
      document.getElementById("overview-disks"),
      overview.disks || data.disks,
      "No filesystem data available."
    );
    renderDiskList(
      document.getElementById("disks-list"),
      data.disks,
      "No filesystem data available."
    );

    renderProcessTable(
      document.getElementById("overview-processes-body"),
      overview.top_processes || (data.processes || []).slice(0, 5),
      { summaryId: "overview-process-summary", totalCount: data.process_count }
    );
    renderProcessTable(
      document.getElementById("processes-body"),
      data.processes,
      { summaryId: "process-summary", totalCount: data.process_count }
    );
    renderCoreBars(document.getElementById("cpu-per-core"), cpu.per_core || (data.cpu && data.cpu.per_core) || []);
    renderNetworkTable(document.getElementById("network-body"), data.network);
    renderContainerList(document.getElementById("system-containers-list"), data.docker);

    renderSystemLines(document.getElementById("system-lines"), system);
    renderSystemLines(document.getElementById("system-lines-detail"), system);
    setTexts(["system-uptime", "system-uptime-detail"], system.uptime);
    setTexts(["system-threads", "system-threads-detail"], system.cpu_threads);
    setTexts(["system-containers-detail"], containers.running == null ? "—" : containers.running + " running");

    var rangeLabel = chartRange.toUpperCase();
    renderAreaChart(document.getElementById("chart-cpu-1h"), history.cpu, {
      title: "CPU usage " + rangeLabel,
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: cpu.total_display || null,
      color: COLORS.accent,
      max: 100
    });
    renderAreaChart(document.getElementById("chart-load-1h"), history.load, {
      title: "Load average " + rangeLabel,
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: load.load_1 != null ? load.load_1.toFixed(2) : null,
      color: COLORS.purple
    });
    renderAreaChart(document.getElementById("chart-memory-1h"), history.memory, {
      title: "Memory usage " + rangeLabel,
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: memory.percent_display || null,
      color: COLORS.ok,
      max: 100
    });

    var networkLiveParts = (data.network || []).slice(0, 3).map(function (iface) {
      var rx = iface.rx_rate_display;
      var tx = iface.tx_rate_display;
      if (rx || tx) {
        return iface.name + " ↓" + (rx || "—") + " ↑" + (tx || "—");
      }
      return null;
    }).filter(Boolean);
    var networkLiveText = networkLiveParts.length
      ? networkLiveParts.join(" · ")
      : (history.collecting ? "Collecting network rates…" : null);

    renderAreaChart(document.getElementById("chart-network-1h"), history.network, {
      title: "Network I/O " + rangeLabel,
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: networkLiveText,
      color: COLORS.purple
    });
    renderAreaChart(document.getElementById("chart-network-detail"), history.network, {
      title: "Network I/O " + rangeLabel,
      emptyText: historyEmptyText(history, "Collecting history — check back in a few minutes"),
      liveText: networkLiveText,
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

  document.querySelectorAll(".chart-range-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      chartRange = btn.getAttribute("data-range") || "1h";
      syncChartRangeButtons();
      if (lastGoodData) applyDetails(lastGoodData, refreshFailed);
    });
  });
  syncChartRangeButtons();

  (function initGlancesUiLink() {
    var link = document.getElementById("glances-ui-link");
    if (!link) return;
    var port = 61208;
    link.href = window.location.protocol + "//" + window.location.hostname + ":" + port + "/";
  })();

  window.setInterval(refreshDetails, REFRESH_MS);
})();
