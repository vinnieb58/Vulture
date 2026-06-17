/**
 * Lightweight bar charts for the Kestrel detail page.
 * Reads chart data from embedded JSON script tags — no external CDN.
 */
(function () {
  "use strict";

  function parseChartData(elementId) {
    var node = document.getElementById(elementId);
    if (!node || !node.textContent) {
      return [];
    }
    try {
      var parsed = JSON.parse(node.textContent);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }

  function renderBarChart(container, data, options) {
    options = options || {};
    var width = options.width || 640;
    var height = options.height || 220;
    var padding = { top: 16, right: 12, bottom: 42, left: 44 };
    var plotW = width - padding.left - padding.right;
    var plotH = height - padding.top - padding.bottom;

    container.innerHTML = "";
    if (!data.length) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = options.emptyText || "No data for this chart yet.";
      container.appendChild(empty);
      return;
    }

    var values = data.map(function (row) { return Number(row.kwh) || 0; });
    var maxVal = Math.max.apply(null, values.concat([0.01]));
    var barGap = 2;
    var barWidth = Math.max(2, (plotW / data.length) - barGap);

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.title || "Energy chart");

    var yTicks = 4;
    for (var t = 0; t <= yTicks; t += 1) {
      var tickVal = (maxVal / yTicks) * t;
      var y = padding.top + plotH - (tickVal / maxVal) * plotH;
      var grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
      grid.setAttribute("x1", String(padding.left));
      grid.setAttribute("x2", String(width - padding.right));
      grid.setAttribute("y1", String(y));
      grid.setAttribute("y2", String(y));
      grid.setAttribute("class", "chart-grid");
      svg.appendChild(grid);

      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String(padding.left - 6));
      label.setAttribute("y", String(y + 4));
      label.setAttribute("class", "chart-axis-label");
      label.setAttribute("text-anchor", "end");
      label.textContent = tickVal.toFixed(1);
      svg.appendChild(label);
    }

    data.forEach(function (row, index) {
      var value = Number(row.kwh) || 0;
      var barH = (value / maxVal) * plotH;
      var x = padding.left + index * (barWidth + barGap);
      var y = padding.top + plotH - barH;

      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(barWidth));
      rect.setAttribute("height", String(Math.max(barH, value > 0 ? 1 : 0)));
      rect.setAttribute("class", "chart-bar");
      rect.setAttribute("rx", "2");
      svg.appendChild(rect);

      if (data.length <= 24 || index % Math.ceil(data.length / 12) === 0) {
        var xLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
        xLabel.setAttribute("x", String(x + barWidth / 2));
        xLabel.setAttribute("y", String(height - 10));
        xLabel.setAttribute("class", "chart-axis-label");
        xLabel.setAttribute("text-anchor", "middle");
        xLabel.textContent = row.label || "";
        svg.appendChild(xLabel);
      }
    });

    container.appendChild(svg);
  }

  function renderPeakList(container, data) {
    container.innerHTML = "";
    if (!data.length) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "No peak intervals in the last 30 days.";
      container.appendChild(empty);
      return;
    }

    var list = document.createElement("ol");
    list.className = "peak-list";
    data.forEach(function (row) {
      var item = document.createElement("li");
      item.textContent = row.display || "";
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderBarChart(
      document.getElementById("chart-daily-30"),
      parseChartData("chart-data-daily-30"),
      { title: "Daily usage last 30 days", emptyText: "No daily usage in the last 30 days." }
    );
    renderBarChart(
      document.getElementById("chart-daily-full"),
      parseChartData("chart-data-daily-full"),
      { title: "Daily usage full range", emptyText: "No imported interval data yet." }
    );
    renderBarChart(
      document.getElementById("chart-hourly-30"),
      parseChartData("chart-data-hourly-30"),
      { title: "Average usage by hour", emptyText: "No hourly averages for the last 30 days." }
    );
    renderBarChart(
      document.getElementById("chart-monthly"),
      parseChartData("chart-data-monthly"),
      { title: "Monthly totals", emptyText: "Not enough history for monthly totals." }
    );
    renderPeakList(
      document.getElementById("chart-peaks-30"),
      parseChartData("chart-data-peaks-30")
    );
  });
})();
