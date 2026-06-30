/**
 * Lightweight multi-series line charts for Tuya appliance power history.
 * Reads chart data from embedded JSON script tags — no external CDN.
 */
(function () {
  "use strict";

  var SERIES_COLORS = [
    "#58a6ff",
    "#3fb950",
    "#d29922",
    "#f85149",
  ];

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

  function renderLineChart(container, series, options) {
    options = options || {};
    var width = options.width || 640;
    var height = options.height || 240;
    var padding = { top: 16, right: 12, bottom: 42, left: 52 };
    var plotW = width - padding.left - padding.right;
    var plotH = height - padding.top - padding.bottom;

    container.innerHTML = "";
    if (!series.length) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = options.emptyText || "No appliance power history yet.";
      container.appendChild(empty);
      return;
    }

    var allPoints = [];
    series.forEach(function (item) {
      (item.points || []).forEach(function (point) {
        allPoints.push(point);
      });
    });
    if (!allPoints.length) {
      var noPoints = document.createElement("div");
      noPoints.className = "chart-empty";
      noPoints.textContent = options.emptyText || "No watt readings in this window.";
      container.appendChild(noPoints);
      return;
    }

    var maxSeriesLen = 0;
    series.forEach(function (item) {
      maxSeriesLen = Math.max(maxSeriesLen, (item.points || []).length);
    });

    var maxWatts = 0;
    allPoints.forEach(function (point) {
      maxWatts = Math.max(maxWatts, Number(point.watts) || 0);
    });
    maxWatts = Math.max(maxWatts, 1);

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.title || "Appliance power chart");

    var yTicks = 4;
    for (var t = 0; t <= yTicks; t += 1) {
      var tickVal = (maxWatts / yTicks) * t;
      var y = padding.top + plotH - (tickVal / maxWatts) * plotH;
      var grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
      grid.setAttribute("x1", String(padding.left));
      grid.setAttribute("x2", String(width - padding.right));
      grid.setAttribute("y1", String(y));
      grid.setAttribute("y2", String(y));
      grid.setAttribute("class", "chart-grid");
      svg.appendChild(grid);

      var yLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      yLabel.setAttribute("x", String(padding.left - 6));
      yLabel.setAttribute("y", String(y + 4));
      yLabel.setAttribute("class", "chart-axis-label");
      yLabel.setAttribute("text-anchor", "end");
      yLabel.textContent = tickVal >= 100 ? String(Math.round(tickVal)) : tickVal.toFixed(0);
      svg.appendChild(yLabel);
    }

    series.forEach(function (item, seriesIndex) {
      var points = item.points || [];
      if (!points.length) {
        return;
      }
      var color = SERIES_COLORS[seriesIndex % SERIES_COLORS.length];
      var pathParts = [];
      points.forEach(function (point, index) {
        var x = padding.left + (index / Math.max(points.length - 1, 1)) * plotW;
        var watts = Number(point.watts) || 0;
        var yPoint = padding.top + plotH - (watts / maxWatts) * plotH;
        pathParts.push((index === 0 ? "M" : "L") + x.toFixed(2) + " " + yPoint.toFixed(2));
      });

      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", pathParts.join(" "));
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", color);
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("stroke-linecap", "round");
      svg.appendChild(path);

      points.forEach(function (point, index) {
        if (maxSeriesLen > 24 && index % Math.ceil(maxSeriesLen / 8) !== 0 && index !== points.length - 1) {
          return;
        }
        var x = padding.left + (index / Math.max(points.length - 1, 1)) * plotW;
        var watts = Number(point.watts) || 0;
        var yPoint = padding.top + plotH - (watts / maxWatts) * plotH;
        var dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", String(x));
        dot.setAttribute("cy", String(yPoint));
        dot.setAttribute("r", "2.5");
        dot.setAttribute("fill", color);
        svg.appendChild(dot);
      });
    });

    var labelSeries = series.reduce(function (best, item) {
      return (item.points || []).length > (best.points || []).length ? item : best;
    }, series[0]);
    (labelSeries.points || []).forEach(function (point, index) {
      if ((labelSeries.points || []).length > 12 && index % Math.ceil(labelSeries.points.length / 6) !== 0 && index !== labelSeries.points.length - 1) {
        return;
      }
      var x = padding.left + (index / Math.max(labelSeries.points.length - 1, 1)) * plotW;
      var xLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      xLabel.setAttribute("x", String(x));
      xLabel.setAttribute("y", String(height - 10));
      xLabel.setAttribute("class", "chart-axis-label");
      xLabel.setAttribute("text-anchor", "middle");
      xLabel.textContent = point.label || "";
      svg.appendChild(xLabel);
    });

    var legendY = padding.top - 4;
    series.forEach(function (item, seriesIndex) {
      var color = SERIES_COLORS[seriesIndex % SERIES_COLORS.length];
      var legendX = padding.left + seriesIndex * 150;
      var swatch = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      swatch.setAttribute("x", String(legendX));
      swatch.setAttribute("y", String(legendY - 8));
      swatch.setAttribute("width", "10");
      swatch.setAttribute("height", "10");
      swatch.setAttribute("fill", color);
      svg.appendChild(swatch);

      var legendLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
      legendLabel.setAttribute("x", String(legendX + 14));
      legendLabel.setAttribute("y", String(legendY));
      legendLabel.setAttribute("class", "chart-axis-label");
      legendLabel.textContent = item.label || item.key || "";
      svg.appendChild(legendLabel);
    });

    container.appendChild(svg);
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderLineChart(
      document.getElementById("chart-tuya-power-1h"),
      parseChartData("chart-data-tuya-power-1h"),
      { title: "Appliance power last 1 hour", emptyText: "No appliance power history in the last hour." }
    );
    renderLineChart(
      document.getElementById("chart-tuya-power-24h"),
      parseChartData("chart-data-tuya-power-24h"),
      { title: "Appliance power last 24 hours", emptyText: "No appliance power history in the last 24 hours." }
    );
  });
})();
