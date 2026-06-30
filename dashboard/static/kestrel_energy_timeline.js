/**
 * Combined Energy + HVAC Timeline for the Kestrel detail page.
 *
 * Renders one synchronized SVG chart showing:
 *   - SMT 15-min average-kW bars (utility billing reference)
 *   - Tuya measured load line (sum of 4 appliance CTs)
 *   - Tuya compressor line
 *   - Nest cooling period shading
 *
 * Data is read from an embedded JSON script tag.
 * No external dependencies.
 */
(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------------------

  function parseData(elementId) {
    var node = document.getElementById(elementId);
    if (!node || !node.textContent) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (_) {
      return null;
    }
  }

  function parseTs(iso) {
    return new Date(iso);
  }

  function formatTime(date, tz) {
    // Use Intl if available for local timezone formatting
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: tz || "America/Chicago",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      }).format(date);
    } catch (_) {
      var h = date.getHours();
      var m = date.getMinutes();
      var period = h < 12 ? "AM" : "PM";
      h = h % 12 || 12;
      return h + ":" + String(m).padStart(2, "0") + " " + period;
    }
  }

  function formatDate(date, tz) {
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: tz || "America/Chicago",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
      }).format(date);
    } catch (_) {
      return date.toLocaleString();
    }
  }

  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        el.setAttribute(key, attrs[key]);
      }
    }
    return el;
  }

  function lerp(min, max, t) {
    return min + (max - min) * t;
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  // -------------------------------------------------------------------------
  // Layout constants
  // -------------------------------------------------------------------------

  var PAD = { top: 20, right: 16, bottom: 52, left: 48 };
  var CHART_HEIGHT = 260;
  var COOLING_BAND_OPACITY = 0.12;
  var TZ = "America/Chicago";

  // -------------------------------------------------------------------------
  // Data preparation
  // -------------------------------------------------------------------------

  function buildTimeRange(data) {
    var start = parseTs(data.window_start);
    var end = parseTs(data.window_end);
    return { start: start, end: end, span: end - start };
  }

  function timeToX(ts, range, plotW) {
    var frac = (ts - range.start) / range.span;
    return PAD.left + frac * plotW;
  }

  function maxKw(data) {
    var max = 0.1;
    (data.smt_bars || []).forEach(function (b) {
      if (b.avg_kw > max) max = b.avg_kw;
    });
    (data.tuya_measured || []).forEach(function (p) {
      if (p.kw > max) max = p.kw;
    });
    (data.tuya_compressor || []).forEach(function (p) {
      if (p.kw > max) max = p.kw;
    });
    return max;
  }

  function kwToY(kw, maxKwVal, plotH) {
    return PAD.top + plotH - (kw / maxKwVal) * plotH;
  }

  // -------------------------------------------------------------------------
  // Renderers
  // -------------------------------------------------------------------------

  function renderCoolingBands(svg, bands, range, plotW, plotH) {
    (bands || []).forEach(function (band) {
      var x1 = timeToX(parseTs(band.start), range, plotW);
      var x2 = timeToX(parseTs(band.end), range, plotW);
      var w = Math.max(x2 - x1, 2);
      var rect = svgEl("rect", {
        x: String(x1),
        y: String(PAD.top),
        width: String(w),
        height: String(plotH),
        fill: "#58a6ff",
        opacity: String(COOLING_BAND_OPACITY),
        "pointer-events": "none",
      });
      svg.appendChild(rect);
    });
  }

  function renderSmtBars(svg, bars, range, plotW, plotH, maxKwVal) {
    if (!bars || !bars.length) return;
    var intervalSpan = (parseTs(bars[0].end_ts) - parseTs(bars[0].start_ts));
    var barWidthPx = Math.max(2, (intervalSpan / range.span) * plotW - 1);

    bars.forEach(function (bar) {
      var x = timeToX(parseTs(bar.start_ts), range, plotW);
      var y = kwToY(bar.avg_kw, maxKwVal, plotH);
      var h = PAD.top + plotH - y;
      var rect = svgEl("rect", {
        x: String(x),
        y: String(y),
        width: String(barWidthPx),
        height: String(Math.max(h, bar.avg_kw > 0 ? 1 : 0)),
        fill: "#58a6ff",
        opacity: "0.55",
        rx: "1",
        "data-start": bar.start_ts,
        "data-end": bar.end_ts,
        "data-kwh": String(bar.kwh),
        "data-avg-kw": String(bar.avg_kw),
        "data-series": "smt",
      });
      rect.style.cursor = "crosshair";
      svg.appendChild(rect);
    });
  }

  function renderLine(svg, points, plotW, plotH, maxKwVal, range, color, dasharray) {
    if (!points || points.length < 2) return;
    var coords = points.map(function (p) {
      var x = timeToX(parseTs(p.timestamp), range, plotW);
      var y = kwToY(p.kw, maxKwVal, plotH);
      return [x, y, p];
    });

    // Draw polyline segments, breaking on large time gaps (> 5 minutes)
    var MAX_GAP_MS = 5 * 60 * 1000;
    var pathData = "";
    var inPath = false;

    for (var i = 0; i < coords.length; i++) {
      var x = coords[i][0];
      var y = coords[i][1];
      if (i > 0) {
        var gap = parseTs(coords[i][2].timestamp) - parseTs(coords[i - 1][2].timestamp);
        if (gap > MAX_GAP_MS) {
          inPath = false;
        }
      }
      if (!inPath) {
        pathData += "M " + x.toFixed(1) + " " + y.toFixed(1) + " ";
        inPath = true;
      } else {
        pathData += "L " + x.toFixed(1) + " " + y.toFixed(1) + " ";
      }
    }

    var attrs = {
      d: pathData,
      stroke: color,
      "stroke-width": "1.5",
      fill: "none",
      "pointer-events": "none",
    };
    if (dasharray) attrs["stroke-dasharray"] = dasharray;
    svg.appendChild(svgEl("path", attrs));

    // Draw invisible hover-hit circles
    coords.forEach(function (c) {
      var circle = svgEl("circle", {
        cx: String(c[0].toFixed(1)),
        cy: String(c[1].toFixed(1)),
        r: "4",
        fill: "transparent",
        stroke: "none",
        "data-timestamp": c[2].timestamp,
        "data-kw": String(c[2].kw),
        "data-series": "tuya",
        style: "cursor:crosshair",
      });
      svg.appendChild(circle);
    });
  }

  function renderGridAndAxes(svg, width, plotW, plotH, maxKwVal, range) {
    var yTicks = 4;
    for (var t = 0; t <= yTicks; t++) {
      var kw = (maxKwVal / yTicks) * t;
      var y = kwToY(kw, maxKwVal, plotH);
      svg.appendChild(svgEl("line", {
        x1: String(PAD.left),
        x2: String(PAD.left + plotW),
        y1: String(y.toFixed(1)),
        y2: String(y.toFixed(1)),
        stroke: "#2d3a4f",
        "stroke-width": "1",
        opacity: "0.6",
      }));
      svg.appendChild(Object.assign(svgEl("text", {
        x: String(PAD.left - 6),
        y: String((y + 4).toFixed(1)),
        "text-anchor": "end",
        fill: "#8b949e",
        "font-size": "10",
      }), { textContent: kw.toFixed(1) }));
    }

    // X-axis time labels
    var windowMs = range.span;
    var tickCount = Math.min(8, Math.floor(plotW / 60));
    for (var i = 0; i <= tickCount; i++) {
      var frac = i / tickCount;
      var ts = new Date(range.start.getTime() + frac * windowMs);
      var xPos = PAD.left + frac * plotW;
      var label = formatTime(ts, TZ);
      svg.appendChild(Object.assign(svgEl("text", {
        x: String(xPos.toFixed(1)),
        y: String(CHART_HEIGHT - 6),
        "text-anchor": "middle",
        fill: "#8b949e",
        "font-size": "10",
      }), { textContent: label }));
      svg.appendChild(svgEl("line", {
        x1: String(xPos.toFixed(1)),
        x2: String(xPos.toFixed(1)),
        y1: String(PAD.top + plotH),
        y2: String(PAD.top + plotH + 4),
        stroke: "#2d3a4f",
        "stroke-width": "1",
      }));
    }

    // Y axis label
    var yLabel = svgEl("text", {
      x: "10",
      y: String(PAD.top + plotH / 2),
      "text-anchor": "middle",
      fill: "#8b949e",
      "font-size": "10",
      transform: "rotate(-90,10," + (PAD.top + plotH / 2) + ")",
    });
    yLabel.textContent = "kW";
    svg.appendChild(yLabel);
  }

  function renderLegend(container, data) {
    var items = [];
    if (data.has_smt) items.push({ color: "#58a6ff", opacity: "0.55", label: "SMT (utility, bars)", dashed: false });
    if (data.has_tuya) {
      items.push({ color: "#f0883e", opacity: "1", label: "Tuya measured load", dashed: false });
      items.push({ color: "#da3633", opacity: "1", label: "Compressor", dashed: false });
    }
    if (data.has_nest) items.push({ color: "#58a6ff", opacity: String(COOLING_BAND_OPACITY * 6), label: "Cooling period", band: true });
    if (!items.length) return;

    var legend = document.createElement("div");
    legend.style.cssText = "display:flex;flex-wrap:wrap;gap:0.75rem;margin-top:0.5rem;font-size:0.78rem;color:#8b949e;";
    items.forEach(function (item) {
      var el = document.createElement("span");
      el.style.cssText = "display:flex;align-items:center;gap:0.3rem;";
      var swatch = document.createElement("span");
      if (item.band) {
        swatch.style.cssText = "display:inline-block;width:14px;height:10px;background:" + item.color + ";opacity:0.5;border-radius:2px;";
      } else {
        swatch.style.cssText = "display:inline-block;width:14px;height:3px;background:" + item.color + ";opacity:" + (item.opacity || "1") + ";border-radius:1px;";
      }
      el.appendChild(swatch);
      el.appendChild(document.createTextNode(item.label));
      legend.appendChild(el);
    });
    container.appendChild(legend);
  }

  // -------------------------------------------------------------------------
  // Tooltip
  // -------------------------------------------------------------------------

  function buildTooltip(parent) {
    var tip = document.createElement("div");
    tip.style.cssText = [
      "position:absolute;pointer-events:none;display:none;",
      "background:#1a2332;border:1px solid #2d3a4f;border-radius:8px;",
      "padding:0.5rem 0.75rem;font-size:0.78rem;color:#e6edf3;",
      "max-width:220px;z-index:10;",
    ].join("");
    parent.style.position = "relative";
    parent.appendChild(tip);
    return tip;
  }

  function showTooltip(tip, svgEl_, event, lines) {
    tip.innerHTML = lines.map(function (l) { return "<div>" + l + "</div>"; }).join("");
    var rect = svgEl_.getBoundingClientRect();
    var parentRect = tip.parentElement.getBoundingClientRect();
    var x = event.clientX - parentRect.left + 12;
    var y = event.clientY - parentRect.top - 8;
    // Keep tooltip inside
    var tipW = 220;
    if (x + tipW > parentRect.width) x = event.clientX - parentRect.left - tipW - 8;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
    tip.style.display = "block";
  }

  function hideTooltip(tip) {
    tip.style.display = "none";
  }

  function buildTooltipLines(el, data, range) {
    var series = el.getAttribute("data-series");
    var lines = [];

    if (series === "smt") {
      var startTs = el.getAttribute("data-start");
      var kwh = parseFloat(el.getAttribute("data-kwh") || "0");
      var avgKw = parseFloat(el.getAttribute("data-avg-kw") || "0");
      lines.push("<strong>SMT interval</strong>");
      lines.push(formatTime(parseTs(startTs), TZ));
      lines.push("Energy: " + kwh.toFixed(3) + " kWh");
      lines.push("Avg: " + avgKw.toFixed(2) + " kW");

      // Find nearest Tuya and Nest data
      var ts = parseTs(startTs);
      var nearestTuya = findNearest(data.tuya_measured || [], ts);
      if (nearestTuya) {
        lines.push("Tuya measured: " + nearestTuya.kw.toFixed(2) + " kW");
      }
      var nearestComp = findNearest(data.tuya_compressor || [], ts);
      if (nearestComp) {
        lines.push("Compressor: " + nearestComp.kw.toFixed(2) + " kW");
      }
      var nestAction = findNestAction(data.nest_samples || [], ts);
      if (nestAction) {
        lines.push("Nest: " + nestAction);
      }
    } else if (series === "tuya") {
      var ts2 = parseTs(el.getAttribute("data-timestamp"));
      var kw = parseFloat(el.getAttribute("data-kw") || "0");
      lines.push("<strong>" + formatTime(ts2, TZ) + "</strong>");
      lines.push("Measured load: " + kw.toFixed(2) + " kW");
      var nestAction2 = findNestAction(data.nest_samples || [], ts2);
      if (nestAction2) lines.push("Nest: " + nestAction2);
    }

    return lines;
  }

  function findNearest(points, ts) {
    if (!points || !points.length) return null;
    var best = null;
    var bestDiff = Infinity;
    points.forEach(function (p) {
      var diff = Math.abs(parseTs(p.timestamp) - ts);
      if (diff < bestDiff) {
        bestDiff = diff;
        best = p;
      }
    });
    // Only return if within 2 minutes
    return bestDiff < 120000 ? best : null;
  }

  function findNestAction(samples, ts) {
    if (!samples || !samples.length) return null;
    var best = null;
    var bestDiff = Infinity;
    samples.forEach(function (s) {
      var diff = Math.abs(parseTs(s.timestamp) - ts);
      if (diff < bestDiff) {
        bestDiff = diff;
        best = s;
      }
    });
    if (!best || bestDiff > 600000) return null;  // > 10 min away
    var actions = [];
    Object.keys(best).forEach(function (k) {
      if (k.endsWith("_action") && best[k]) {
        var zone = k.replace("_action", "");
        actions.push(zone + ": " + best[k]);
      }
    });
    return actions.length ? actions.join(", ") : null;
  }

  // -------------------------------------------------------------------------
  // Main render
  // -------------------------------------------------------------------------

  function renderTimeline(container, data) {
    container.innerHTML = "";

    if (!data || (!data.has_smt && !data.has_tuya && !data.has_nest)) {
      var empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "No data available for the selected analysis window.";
      container.appendChild(empty);
      return;
    }

    var containerW = container.offsetWidth || 640;
    var width = containerW;
    var plotW = width - PAD.left - PAD.right;
    var plotH = CHART_HEIGHT - PAD.top - PAD.bottom;

    var range = buildTimeRange(data);
    var maxKwVal = Math.max(maxKw(data), 0.5) * 1.1;

    var svg = svgEl("svg", {
      viewBox: "0 0 " + width + " " + CHART_HEIGHT,
      role: "img",
      "aria-label": "Combined energy and HVAC timeline",
      style: "width:100%;height:auto;display:block;overflow:visible;",
    });

    // 1. Cooling bands (background)
    renderCoolingBands(svg, data.cooling_bands, range, plotW, plotH);

    // 2. Grid and axes
    renderGridAndAxes(svg, width, plotW, plotH, maxKwVal, range);

    // 3. SMT bars
    renderSmtBars(svg, data.smt_bars, range, plotW, plotH, maxKwVal);

    // 4. Tuya lines
    renderLine(svg, data.tuya_measured, plotW, plotH, maxKwVal, range, "#f0883e", null);
    renderLine(svg, data.tuya_compressor, plotW, plotH, maxKwVal, range, "#da3633", null);

    container.appendChild(svg);

    // Tooltip
    var tip = buildTooltip(container);
    svg.addEventListener("mousemove", function (event) {
      var target = event.target;
      var series = target.getAttribute("data-series");
      if (!series) {
        hideTooltip(tip);
        return;
      }
      var lines = buildTooltipLines(target, data, range);
      if (lines.length) {
        showTooltip(tip, svg, event, lines);
      }
    });
    svg.addEventListener("mouseleave", function () {
      hideTooltip(tip);
    });

    // Legend
    renderLegend(container, data);
  }

  // -------------------------------------------------------------------------
  // Daily trends sparklines
  // -------------------------------------------------------------------------

  function renderTrendsChart(container, trends) {
    if (!trends || !trends.length) {
      container.innerHTML = '<div class="chart-empty">No trend data available.</div>';
      return;
    }

    var hasSmt = trends.some(function (d) { return d.smt_kwh !== null; });
    var hasHvac = trends.some(function (d) { return d.hvac_kwh !== null; });

    var maxKwh = 0.1;
    trends.forEach(function (d) {
      if (d.smt_kwh && d.smt_kwh > maxKwh) maxKwh = d.smt_kwh;
    });

    var width = container.offsetWidth || 640;
    var height = 120;
    var pad = { top: 8, right: 8, bottom: 32, left: 36 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    var barW = Math.max(2, plotW / trends.length - 2);

    var svg = svgEl("svg", {
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label": "7-day energy trend",
      style: "width:100%;height:auto;display:block;",
    });

    trends.forEach(function (day, i) {
      var x = pad.left + i * (barW + 2);

      if (day.smt_kwh !== null) {
        var smtH = (day.smt_kwh / maxKwh) * plotH;
        svg.appendChild(svgEl("rect", {
          x: String(x),
          y: String(pad.top + plotH - smtH),
          width: String(barW),
          height: String(Math.max(smtH, 1)),
          fill: "#58a6ff",
          opacity: "0.6",
          rx: "1",
        }));
      }

      if (hasHvac && day.hvac_kwh !== null) {
        var hvacH = (day.hvac_kwh / maxKwh) * plotH;
        svg.appendChild(svgEl("rect", {
          x: String(x),
          y: String(pad.top + plotH - hvacH),
          width: String(barW),
          height: String(Math.max(hvacH, 1)),
          fill: "#f0883e",
          opacity: "0.8",
          rx: "1",
        }));
      }

      if (!day.adequate_coverage) {
        svg.appendChild(Object.assign(svgEl("text", {
          x: String(x + barW / 2),
          y: String(pad.top + plotH / 2),
          "text-anchor": "middle",
          fill: "#8b949e",
          "font-size": "9",
        }), { textContent: "?" }));
      }

      // Date label
      svg.appendChild(Object.assign(svgEl("text", {
        x: String(x + barW / 2),
        y: String(height - 4),
        "text-anchor": "middle",
        fill: day.is_today ? "#e6edf3" : "#8b949e",
        "font-size": "9",
        "font-weight": day.is_today ? "700" : "400",
      }), { textContent: day.date_label }));
    });

    // Y axis ticks
    for (var t = 0; t <= 2; t++) {
      var kwh = (maxKwh / 2) * t;
      var y = pad.top + plotH - (kwh / maxKwh) * plotH;
      svg.appendChild(Object.assign(svgEl("text", {
        x: String(pad.left - 4),
        y: String(y + 3),
        "text-anchor": "end",
        fill: "#8b949e",
        "font-size": "9",
      }), { textContent: kwh.toFixed(0) }));
    }

    container.innerHTML = "";
    container.appendChild(svg);
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var timelineContainer = document.getElementById("chart-energy-timeline");
    if (timelineContainer) {
      var timelineData = parseData("chart-data-energy-timeline");
      if (timelineData) {
        renderTimeline(timelineContainer, timelineData);
      } else {
        timelineContainer.innerHTML = '<div class="chart-empty">Timeline data unavailable.</div>';
      }
    }

    var trendsContainer = document.getElementById("chart-daily-trends");
    if (trendsContainer) {
      var trendsData = parseData("chart-data-daily-trends");
      if (trendsData) {
        renderTrendsChart(trendsContainer, trendsData);
      }
    }
  });
})();
