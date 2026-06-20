/**
 * Raven Health details page — lightweight live updates via fetch().
 */
(function () {
  "use strict";

  var REFRESH_MS = 5000;
  var initialNode = document.getElementById("raven-health-initial-data");
  var initialData = {};
  if (initialNode && initialNode.textContent) {
    try {
      initialData = JSON.parse(initialNode.textContent);
      REFRESH_MS = (initialData.refresh_seconds || 5) * 1000;
    } catch (_err) {
      initialData = {};
    }
  }

  function setText(field, value) {
    document.querySelectorAll('[data-field="' + field + '"]').forEach(function (node) {
      node.textContent = value == null || value === "" ? "—" : String(value);
    });
  }

  function renderProcessRows(container, processes) {
    if (!container) {
      return;
    }
    container.innerHTML = "";
    if (!processes || !processes.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.innerHTML = '<td colspan="3">No process data available.</td>';
      container.appendChild(emptyRow);
      return;
    }
    processes.forEach(function (proc) {
      var row = document.createElement("tr");
      var nameCell = document.createElement("td");
      nameCell.className = "process-name";
      nameCell.title = proc.name || "";
      nameCell.textContent = proc.name || "unknown";
      var cpuCell = document.createElement("td");
      cpuCell.textContent = proc.cpu_percent_display || "—";
      var memCell = document.createElement("td");
      memCell.textContent = proc.memory_percent_display || "—";
      row.appendChild(nameCell);
      row.appendChild(cpuCell);
      row.appendChild(memCell);
      container.appendChild(row);
    });
  }

  function renderTemps(container, sensors) {
    if (!container) {
      return;
    }
    container.innerHTML = "";
    if (!sensors || !sensors.length) {
      container.innerHTML = '<div class="detail-row"><span>CPU</span><strong>—</strong></div>';
      return;
    }
    sensors.slice(0, 6).forEach(function (sensor) {
      var row = document.createElement("div");
      row.className = "detail-row" + (sensor.is_highest ? " emphasis" : "");
      row.innerHTML =
        "<span>" + (sensor.label || "sensor") + "</span><strong>" +
        (sensor.value_display || "—") + "</strong>";
      container.appendChild(row);
    });
  }

  function renderLineChart(container, data, options) {
    options = options || {};
    var width = options.width || 320;
    var height = options.height || 140;
    var padding = { top: 12, right: 8, bottom: 28, left: 36 };
    var plotW = width - padding.left - padding.right;
    var plotH = height - padding.top - padding.bottom;

    container.innerHTML = "";
    if (!data || !data.length) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = options.emptyText || "No history data yet.";
      container.appendChild(empty);
      return;
    }

    var values = data.map(function (row) { return Number(row.value) || 0; });
    var maxVal = Math.max.apply(null, values.concat([0.01]));
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.title || "History chart");

    var points = [];
    data.forEach(function (row, index) {
      var x = padding.left + (index / Math.max(data.length - 1, 1)) * plotW;
      var y = padding.top + plotH - ((Number(row.value) || 0) / maxVal) * plotH;
      points.push(x + "," + y);
    });

    var grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
    grid.setAttribute("x1", String(padding.left));
    grid.setAttribute("x2", String(width - padding.right));
    grid.setAttribute("y1", String(padding.top + plotH));
    grid.setAttribute("y2", String(padding.top + plotH));
    grid.setAttribute("class", "chart-grid");
    svg.appendChild(grid);

    var polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", points.join(" "));
    polyline.setAttribute("class", "chart-line");
    svg.appendChild(polyline);

    var first = data[0];
    var last = data[data.length - 1];
    var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", String(padding.left));
    label.setAttribute("y", String(height - 8));
    label.setAttribute("class", "chart-axis-label");
    label.textContent = (first && first.label) || "";
    svg.appendChild(label);

    var labelEnd = document.createElementNS("http://www.w3.org/2000/svg", "text");
    labelEnd.setAttribute("x", String(width - padding.right));
    labelEnd.setAttribute("y", String(height - 8));
    labelEnd.setAttribute("class", "chart-axis-label");
    labelEnd.setAttribute("text-anchor", "end");
    labelEnd.textContent = (last && last.label) || "";
    svg.appendChild(labelEnd);

    container.appendChild(svg);
  }

  function renderCharts(history) {
    history = history || {};
    renderLineChart(
      document.getElementById("chart-cpu-1h"),
      history.cpu_1h || [],
      { title: "CPU history 1h", emptyText: "No CPU history for the last hour." }
    );
    renderLineChart(
      document.getElementById("chart-load-1h"),
      history.load_1h || [],
      { title: "Load average 1h", emptyText: "No load history for the last hour." }
    );
    renderLineChart(
      document.getElementById("chart-memory-1h"),
      history.memory_1h || [],
      { title: "Memory usage 1h", emptyText: "No memory history for the last hour." }
    );
  }

  function updateBanner(data) {
    var banner = document.getElementById("glances-banner");
    var badge = document.getElementById("glances-status-badge");
    if (badge) {
      badge.textContent = data.glances_available ? "Live" : "Unavailable";
      badge.className = "status-badge " + (data.status || "unavailable");
    }
    if (banner) {
      if (data.glances_available) {
        banner.classList.add("hidden");
      } else {
        banner.classList.remove("hidden");
      }
    }
  }

  function applyDetails(data) {
    if (!data) {
      return;
    }
    updateBanner(data);
    var updatedAt = document.getElementById("updated-at");
    if (updatedAt) {
      updatedAt.textContent = data.updated_at || "—";
    }

    var overview = data.overview || {};
    var cpu = overview.cpu || data.cpu || {};
    var load = overview.load || {};
    var memory = overview.memory || data.memory || {};
    var swap = overview.swap || data.swap || {};
    var system = overview.system || data.system || {};

    setText("cpu-total", cpu.total_display);
    setText("cpu-total-detail", cpu.total_display);
    setText("cpu-cores", cpu.core_count || cpu.cpu_threads);
    setText("cpu-threads", load.cpu_threads || cpu.cpu_threads);
    setText("cpu-threads-detail", cpu.cpu_threads);
    setText("load-average", load.average_display);
    setText("memory-percent", memory.percent_display);
    setText("memory-percent-detail", memory.percent_display);
    setText(
      "memory-used-total",
      [memory.used_display, memory.total_display].filter(Boolean).join(" / ") || "—"
    );
    setText("memory-free", memory.free_display);
    setText("memory-cached", memory.cached_display);
    setText("memory-summary", memory.summary);
    setText("swap-percent", swap.percent_display);
    setText("swap-percent-detail", swap.percent_display);
    setText(
      "swap-used-total",
      [swap.used_display, swap.total_display].filter(Boolean).join(" / ") || "—"
    );
    setText("swap-free", swap.free_display);
    setText("swap-summary", swap.summary);
    setText("system-host", system.hostname);
    setText("system-uptime", system.uptime);
    setText("system-threads", system.cpu_threads);
    setText(
      "system-containers",
      system.containers_running == null ? "—" : system.containers_running + " running"
    );
    setText("system-os", system.os);
    setText("system-kernel", system.kernel);

    renderTemps(document.getElementById("overview-temps"), overview.temperatures || data.sensors);
    renderTemps(document.getElementById("sensors-list"), data.sensors);
    renderProcessRows(
      document.getElementById("overview-processes-body"),
      overview.top_processes || (data.processes || []).slice(0, 5)
    );
    renderProcessRows(document.getElementById("processes-body"), data.processes);
    renderCharts(data.history || overview.history);
  }

  function refreshDetails() {
    fetch("/api/raven/health/glances", { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("refresh failed");
        }
        return response.json();
      })
      .then(applyDetails)
      .catch(function () {
        var badge = document.getElementById("glances-status-badge");
        if (badge) {
          badge.textContent = "Unavailable";
          badge.className = "status-badge unavailable";
        }
      });
  }

  applyDetails(initialData);
  window.setInterval(refreshDetails, REFRESH_MS);
})();
