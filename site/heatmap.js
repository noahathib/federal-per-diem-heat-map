/* Read-only per diem heat map. */
"use strict";

const NATIONAL_VIEW = {
  center: [39.5, -98.35],
  zoom: window.matchMedia("(max-width: 560px)").matches ? 3 : 4,
};
const STATIC_MODE = document.documentElement.dataset.staticMap === "true";
const GSA_RATE_LOOKUP = "https://www.gsa.gov/travel/plan-a-trip/per-diem-rates";

const METRICS = {
  lodging: { field: "lodgingRate", label: "Lodging / night" },
  mie: { field: "mieRate", label: "M&IE / day" },
  firstLast: { field: "firstLastDayMie", label: "First & last day M&IE" },
};

const COLORS = {
  low: "#2b6fb0",
  middle: "#f5c242",
  high: "#b03137",
  noRate: "#c5ced7",
  ambiguous: "#73558f",
  stateContext: "#d3dbe2",
  selected: "#151a20",
  locality: "#6f2a8e",
  county: "#24384a",
  municipal: "#0a6b62",
  zcta: "#ffffff",
};

const LAYER_ZOOM = {
  localities: 6,
  counties: 7,
  municipal: 9,
  zcta: 9,
};

const app = {
  context: null,
  metric: "lodging",
  mode: "national",
  activeState: null,
  selectedZip: null,
  selection: null,
  highlighted: "zip",
  map: null,
  renderer: null,
  renderers: {},
  statesLayer: null,
  gradientLayer: null,
  zctaLayer: null,
  countyLayer: null,
  municipalLayer: null,
  localityLayer: null,
  selectionLayer: null,
  statesGeojson: null,
  geoCache: {
    zcta: new Map(),
    counties: new Map(),
    municipal: new Map(),
    localities: new Map(),
  },
  spatialIndexes: {},
  featureLayers: {
    zcta: new Map(),
    counties: new Map(),
    municipal: new Map(),
    localities: new Map(),
  },
  layerState: {
    rate: true,
    localities: true,
    municipal: true,
    zcta: false,
    counties: false,
    basemap: false,
  },
  nationalData: null,
  stateData: null,
  nationalByState: new Map(),
  stateByZip: new Map(),
  nationalSnapshots: null,
  stateRateCache: new Map(),
  zipIndex: null,
  tiles: null,
  sizeObserver: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function money(value) {
  if (value === undefined || value === null || value === "") return "No single rate";
  const number = Number(value);
  if (!Number.isFinite(number)) return "No single rate";
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

async function fetchJSON(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function hexToRgb(hex) {
  return [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
}

function blend(start, end, amount) {
  const a = hexToRgb(start);
  const b = hexToRgb(end);
  const channels = a.map((value, index) => Math.round(value + (b[index] - value) * amount));
  return `rgb(${channels.join(", ")})`;
}

function heatRgb(value, range) {
  if (value === undefined || value === null || value === "") return hexToRgb(COLORS.noRate);
  const number = Number(value);
  if (!range || !Number.isFinite(number) || range.min === null || range.max === null) {
    return hexToRgb(COLORS.noRate);
  }
  const spread = Number(range.max) - Number(range.min);
  const position = spread === 0 ? 0.5 : Math.max(0, Math.min(1, (number - range.min) / spread));
  const start = position <= 0.5 ? hexToRgb(COLORS.low) : hexToRgb(COLORS.middle);
  const end = position <= 0.5 ? hexToRgb(COLORS.middle) : hexToRgb(COLORS.high);
  const amount = position <= 0.5 ? position * 2 : (position - 0.5) * 2;
  return start.map((channel, index) => Math.round(channel + (end[index] - channel) * amount));
}

function heatColor(value, range) {
  if (value === undefined || value === null || value === "") return COLORS.noRate;
  const number = Number(value);
  if (!range || !Number.isFinite(number) || range.min === null || range.max === null) {
    return COLORS.noRate;
  }
  const spread = Number(range.max) - Number(range.min);
  const position = spread === 0 ? 0.5 : Math.max(0, Math.min(1, (number - range.min) / spread));
  return position <= 0.5
    ? blend(COLORS.low, COLORS.middle, position * 2)
    : blend(COLORS.middle, COLORS.high, (position - 0.5) * 2);
}

function currentMetric() {
  return METRICS[app.metric];
}

function currentData() {
  return app.mode === "state" ? app.stateData : app.nationalData;
}

function currentRange() {
  const data = currentData();
  return data && data.ranges ? data.ranges[currentMetric().field] : null;
}

function tooltipNode(title, lines) {
  const node = el("div", "heat-tooltip");
  node.appendChild(el("strong", null, title));
  lines.forEach((line) => node.appendChild(el("span", null, line)));
  return node;
}

function stateStyle(feature) {
  if (app.mode === "state") {
    return {
      color: "#ffffff",
      weight: 0.9,
      fillColor: COLORS.stateContext,
      fillOpacity: 0,
    };
  }
  return {
    color: "#ffffff",
    weight: 1.15,
    fillColor: "#ffffff",
    fillOpacity: app.layerState.rate
      ? (feature.properties.hasLayer ? 0.015 : 0.2)
      : 0.06,
  };
}

function stateTooltip(feature) {
  const summary = app.nationalByState.get(feature.properties.state);
  if (!summary) return tooltipNode(feature.properties.name, ["No rate data for this date"]);
  const value = summary[currentMetric().field];
  return tooltipNode(feature.properties.name, [
    `${currentMetric().label} median: ${money(value)}`,
    `${summary.ratedZipCount.toLocaleString()} ZIPs in gradient`,
    `${summary.ambiguousZipCount.toLocaleString()} multi-locality ZIPs`,
  ]);
}

function projectedStatePath(feature, map, topLeft) {
  const path = new Path2D();
  const bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  const polygons = feature.geometry.type === "Polygon"
    ? [feature.geometry.coordinates]
    : feature.geometry.coordinates;
  polygons.forEach((polygon) => {
    polygon.forEach((ring) => {
      ring.forEach(([longitude, latitude], index) => {
        const point = map.latLngToLayerPoint([latitude, longitude]).subtract(topLeft);
        if (index === 0) path.moveTo(point.x, point.y);
        else path.lineTo(point.x, point.y);
        bounds.minX = Math.min(bounds.minX, point.x);
        bounds.minY = Math.min(bounds.minY, point.y);
        bounds.maxX = Math.max(bounds.maxX, point.x);
        bounds.maxY = Math.max(bounds.maxY, point.y);
      });
      path.closePath();
    });
  });
  return { path, bounds };
}

function drawStateGradient(context, feature, cells, range, map, topLeft, size) {
  const projected = projectedStatePath(feature, map, topLeft);
  const minX = Math.max(0, Math.floor(projected.bounds.minX));
  const minY = Math.max(0, Math.floor(projected.bounds.minY));
  const maxX = Math.min(size.x, Math.ceil(projected.bounds.maxX));
  const maxY = Math.min(size.y, Math.ceil(projected.bounds.maxY));
  const width = maxX - minX;
  const height = maxY - minY;
  if (width <= 0 || height <= 0) return;

  const fieldCells = cells
    .map((cell) => {
      const value = Number(cell[currentMetric().field]);
      if (!Number.isFinite(value) || !cell.ratedZipCount) return null;
      const point = map
        .latLngToLayerPoint([cell.latitude, cell.longitude])
        .subtract(topLeft);
      return { x: point.x, y: point.y, value };
    })
    .filter(Boolean);

  context.save();
  context.clip(projected.path, "evenodd");
  if (!fieldCells.length) {
    context.fillStyle = COLORS.noRate;
    context.fill(projected.path, "evenodd");
    context.restore();
    return;
  }

  const sampleSize = 5;
  const fieldWidth = Math.max(1, Math.ceil(width / sampleSize));
  const fieldHeight = Math.max(1, Math.ceil(height / sampleSize));
  const fieldCanvas = document.createElement("canvas");
  fieldCanvas.width = fieldWidth;
  fieldCanvas.height = fieldHeight;
  const fieldContext = fieldCanvas.getContext("2d");
  const image = fieldContext.createImageData(fieldWidth, fieldHeight);
  const xScale = width / fieldWidth;
  const yScale = height / fieldHeight;
  const distanceFloor = Math.max(4, width * height * 0.00045);

  for (let y = 0; y < fieldHeight; y += 1) {
    const sampleY = minY + (y + 0.5) * yScale;
    for (let x = 0; x < fieldWidth; x += 1) {
      const sampleX = minX + (x + 0.5) * xScale;
      let weightedValue = 0;
      let totalWeight = 0;
      fieldCells.forEach((cell) => {
        const dx = sampleX - cell.x;
        const dy = sampleY - cell.y;
        const weight = 1 / Math.pow(dx * dx + dy * dy + distanceFloor, 1.35);
        weightedValue += cell.value * weight;
        totalWeight += weight;
      });
      const [red, green, blue] = heatRgb(weightedValue / totalWeight, range);
      const offset = (y * fieldWidth + x) * 4;
      image.data[offset] = red;
      image.data[offset + 1] = green;
      image.data[offset + 2] = blue;
      image.data[offset + 3] = 255;
    }
  }
  fieldContext.putImageData(image, 0, 0);
  context.imageSmoothingEnabled = true;
  context.globalAlpha = 0.88;
  context.drawImage(fieldCanvas, minX, minY, width, height);
  context.restore();
}

function createStateGradientLayer() {
  const StateGradientLayer = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      this._canvas = L.DomUtil.create("canvas", "leaflet-layer state-gradient-layer");
      this._zoomAnimated = map.options.zoomAnimation && L.Browser.any3d;
      L.DomUtil.addClass(
        this._canvas,
        this._zoomAnimated ? "leaflet-zoom-animated" : "leaflet-zoom-hide"
      );
      map.getPane("stateGradientPane").appendChild(this._canvas);
      map.on("moveend zoomend resize viewreset", this.redraw, this);
      if (this._zoomAnimated) map.on("zoomanim", this._animateZoom, this);
      this.redraw();
    },
    onRemove(map) {
      map.off("moveend zoomend resize viewreset", this.redraw, this);
      if (this._zoomAnimated) map.off("zoomanim", this._animateZoom, this);
      if (this._frame) L.Util.cancelAnimFrame(this._frame);
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },
    redraw() {
      if (!this._canvas || this._frame) return;
      this._frame = L.Util.requestAnimFrame(this._draw, this);
    },
    _draw() {
      this._frame = null;
      const canvas = this._canvas;
      if (!canvas) return;
      if (
        !app.layerState.rate ||
        app.mode !== "national" ||
        !app.nationalData ||
        !app.statesGeojson
      ) {
        canvas.style.display = "none";
        return;
      }
      canvas.style.display = "block";
      const map = this._map;
      const size = map.getSize();
      const ratio = window.devicePixelRatio || 1;
      const topLeft = map.containerPointToLayerPoint([0, 0]);
      this._bounds = map.getBounds();
      if (this._zoomAnimated) L.DomUtil.setTransform(canvas, topLeft, 1);
      else L.DomUtil.setPosition(canvas, topLeft);
      canvas.style.width = `${size.x}px`;
      canvas.style.height = `${size.y}px`;
      canvas.width = Math.round(size.x * ratio);
      canvas.height = Math.round(size.y * ratio);
      const context = canvas.getContext("2d");
      context.setTransform(ratio, 0, 0, ratio, 0, 0);

      const byState = new Map();
      (app.nationalData.cells || []).forEach((cell) => {
        if (!byState.has(cell.state)) byState.set(cell.state, []);
        byState.get(cell.state).push(cell);
      });
      const range = currentRange();
      app.statesGeojson.features.forEach((feature) => {
        drawStateGradient(
          context,
          feature,
          byState.get(feature.properties.state) || [],
          range,
          map,
          topLeft,
          size
        );
      });
    },
    _animateZoom(event) {
      if (!this._canvas || !this._bounds) return;
      const scale = this._map.getZoomScale(event.zoom);
      const offset = this._map
        ._latLngBoundsToNewLayerBounds(this._bounds, event.zoom, event.center)
        .min;
      L.DomUtil.setTransform(this._canvas, offset, scale);
    },
  });
  return new StateGradientLayer();
}

function zctaStyle(feature) {
  const zip = feature.properties.zip;
  const entry = app.stateByZip.get(zip);
  let fillColor = COLORS.noRate;
  let fillOpacity = app.layerState.rate ? 0.35 : 0;
  if (entry && entry.status === "ambiguous") {
    fillColor = COLORS.ambiguous;
    fillOpacity = app.layerState.rate ? 0.72 : 0;
  } else if (entry) {
    fillColor = heatColor(entry[currentMetric().field], currentRange());
    fillOpacity = app.layerState.rate ? 0.78 : 0;
  }
  const showBoundary = app.layerState.zcta && app.map.getZoom() >= LAYER_ZOOM.zcta;
  return {
    color: COLORS.zcta,
    opacity: showBoundary ? 0.7 : 0,
    weight: showBoundary ? 0.65 : 0,
    fillColor,
    fillOpacity,
  };
}

function zctaTooltip(feature) {
  const zip = feature.properties.zip;
  const entry = app.stateByZip.get(zip);
  if (!entry) return tooltipNode(zip, ["No published rate for this date"]);
  if (entry.status === "ambiguous") {
    return tooltipNode(zip, [`${entry.candidateCount} published rate localities`, "No single ZIP rate"]);
  }
  return tooltipNode(zip, [
    `${currentMetric().label}: ${money(entry[currentMetric().field])}`,
    entry.locality,
  ]);
}

function boundaryStyle(kind) {
  if (kind === "localities") {
    return { color: COLORS.locality, opacity: 0.92, weight: 2.3, fillOpacity: 0 };
  }
  if (kind === "counties") {
    return { color: COLORS.county, opacity: 0.72, weight: 1.65, fillOpacity: 0 };
  }
  return { color: COLORS.municipal, opacity: 0.68, weight: 1, fillOpacity: 0 };
}

function geometryBounds(geometry) {
  const bounds = [Infinity, Infinity, -Infinity, -Infinity];
  const visit = (coordinates) => {
    if (typeof coordinates[0] === "number") {
      bounds[0] = Math.min(bounds[0], coordinates[0]);
      bounds[1] = Math.min(bounds[1], coordinates[1]);
      bounds[2] = Math.max(bounds[2], coordinates[0]);
      bounds[3] = Math.max(bounds[3], coordinates[1]);
      return;
    }
    coordinates.forEach(visit);
  };
  visit(geometry.coordinates);
  return bounds;
}

function pointOnSegment(x, y, start, end) {
  const cross = (x - start[0]) * (end[1] - start[1]) -
    (y - start[1]) * (end[0] - start[0]);
  if (Math.abs(cross) > 1e-10) return false;
  return x >= Math.min(start[0], end[0]) - 1e-10 &&
    x <= Math.max(start[0], end[0]) + 1e-10 &&
    y >= Math.min(start[1], end[1]) - 1e-10 &&
    y <= Math.max(start[1], end[1]) + 1e-10;
}

function pointInRing(x, y, ring) {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const start = ring[previous];
    const end = ring[index];
    if (pointOnSegment(x, y, start, end)) return true;
    const crosses = (end[1] > y) !== (start[1] > y) &&
      x < ((start[0] - end[0]) * (y - end[1])) / (start[1] - end[1]) + end[0];
    if (crosses) inside = !inside;
  }
  return inside;
}

function pointInPolygon(x, y, polygon) {
  if (!polygon.length || !pointInRing(x, y, polygon[0])) return false;
  return !polygon.slice(1).some((ring) => pointInRing(x, y, ring));
}

function pointInGeometry(longitude, latitude, geometry) {
  const polygons = geometry.type === "Polygon"
    ? [geometry.coordinates]
    : geometry.coordinates;
  return polygons.some((polygon) => pointInPolygon(longitude, latitude, polygon));
}

function createSpatialIndex(geojson, cellSize = 0.5) {
  const buckets = new Map();
  const all = geojson.features || [];
  all.forEach((feature) => {
    const bounds = geometryBounds(feature.geometry);
    feature._mapBounds = bounds;
    const minX = Math.floor(bounds[0] / cellSize);
    const maxX = Math.floor(bounds[2] / cellSize);
    const minY = Math.floor(bounds[1] / cellSize);
    const maxY = Math.floor(bounds[3] / cellSize);
    for (let x = minX; x <= maxX; x += 1) {
      for (let y = minY; y <= maxY; y += 1) {
        const key = `${x}:${y}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(feature);
      }
    }
  });
  return {
    containing(latitude, longitude) {
      const key = `${Math.floor(longitude / cellSize)}:${Math.floor(latitude / cellSize)}`;
      return (buckets.get(key) || []).filter((feature) => {
        const bounds = feature._mapBounds;
        return longitude >= bounds[0] && longitude <= bounds[2] &&
          latitude >= bounds[1] && latitude <= bounds[3] &&
          pointInGeometry(longitude, latitude, feature.geometry);
      });
    },
  };
}

function featureTooltip(feature, kind) {
  const properties = feature.properties;
  if (kind === "localities") {
    return tooltipNode(properties.locality, [
      properties.definition,
      "GSA county-defined rate area",
    ]);
  }
  if (kind === "counties") {
    return tooltipNode(properties.displayName, [properties.stateName]);
  }
  return tooltipNode(properties.displayName, [
    properties.county || properties.stateName,
    `Census ${properties.type}`,
  ]);
}

function appendScale(target, range) {
  const scale = el("div", "heat-scale");
  const labels = el("div", "heat-scale-labels");
  labels.append(el("span", null, money(range && range.min)), el("span", null, money(range && range.max)));
  target.append(scale, labels);
}

function addLegendKey(target, color, label) {
  const row = el("div", "legend-key");
  const swatch = el("span", "swatch");
  swatch.style.background = color;
  row.append(swatch, document.createTextNode(label));
  target.appendChild(row);
}

function addBoundaryKey(target, color, label) {
  const row = el("div", "legend-key");
  const line = el("span", "boundary-line");
  line.style.color = color;
  row.append(line, document.createTextNode(label));
  target.appendChild(row);
}

function renderLegend() {
  const legend = document.getElementById("map-legend");
  legend.replaceChildren();
  legend.appendChild(el("b", null, currentMetric().label));
  appendScale(legend, currentRange());
  if (app.mode === "state") addLegendKey(legend, COLORS.ambiguous, "Multiple rate localities");
  addLegendKey(legend, COLORS.noRate, "No single rate");
  if (app.mode === "state") {
    const zoom = app.map.getZoom();
    if (app.layerState.localities && zoom >= LAYER_ZOOM.localities) {
      addBoundaryKey(legend, COLORS.locality, "County-defined GSA area");
    }
    if (app.layerState.counties && zoom >= LAYER_ZOOM.counties) {
      addBoundaryKey(legend, COLORS.county, "County boundary");
    }
    if (app.layerState.municipal && zoom >= LAYER_ZOOM.municipal) {
      addBoundaryKey(legend, COLORS.municipal, "Municipal / subdivision");
    }
  }
}

function renderScalePanel() {
  const target = document.getElementById("scale-panel");
  target.replaceChildren();
  const scope = app.mode === "state" ? app.activeState : "United States";
  target.appendChild(el("h2", null, `${currentMetric().label} scale · ${scope}`));
  appendScale(target, currentRange());
  target.appendChild(
    el(
      "p",
      null,
      app.mode === "state"
        ? "ZIP colors are scaled from the lowest to highest unambiguous rate in this state."
        : "Each state fill blends local high-rate samples so small expensive areas remain visible."
    )
  );
}

function stat(value, label) {
  const node = el("div", "heat-stat");
  node.append(el("strong", null, Number(value || 0).toLocaleString()), el("span", null, label));
  return node;
}

function renderSummary() {
  const target = document.getElementById("summary-panel");
  target.replaceChildren();
  const data = currentData();
  if (!data) return;
  target.appendChild(el("h2", null, app.mode === "state" ? `${app.activeState} coverage` : "National coverage"));
  const grid = el("div", "heat-stats");
  grid.append(
    stat(data.ratedZipCount, "ZIPs in gradient"),
    stat(data.ambiguousZipCount, "Multiple rates")
  );
  target.appendChild(grid);
  target.appendChild(el("p", null, `${data.travelDate} · fiscal year ${data.fiscalYear}`));
  if (data.rateStatus === "planning-estimate") {
    target.appendChild(
      el(
        "p",
        "planning-note",
        `Planning estimate: showing the seasonal rates from ${data.rateDate} ` +
          `(FY${data.rateFiscalYear}). Official rates are loaded through ${data.officialCoverageEnd}.`
      )
    );
  }
}

function renderSelection() {
  const target = document.getElementById("selection-panel");
  target.replaceChildren();
  if (app.mode !== "state" || !app.selection) return;
  const selection = app.selection;
  const entry = selection.entry;
  target.appendChild(el("h2", null, "Selected location"));
  const card = el("div", "heat-selection");
  const head = el("div", "heat-selection-head");
  const title = selection.municipal
    ? selection.municipal.properties.displayName
    : selection.zip
      ? `ZIP / ZCTA ${selection.zip.properties.zip}`
      : selection.county
        ? selection.county.properties.displayName
        : "Map location";
  head.appendChild(el("strong", null, title));
  const locationLine = [
    selection.county && selection.county.properties.displayName,
    selection.stateName,
  ].filter(Boolean).join(", ");
  if (locationLine) head.appendChild(el("span", null, locationLine));
  const body = el("div", "heat-selection-body");

  const perDiem = el("section", "selection-group");
  perDiem.appendChild(el("h3", null, "Per diem"));
  if (!entry) {
    perDiem.appendChild(el("p", "selection-message", "No published rate for this date."));
  } else if (entry.status === "ambiguous") {
    const warning = el("p", "selection-message");
    warning.append(
      el("strong", null, "Rate requires more precise location. "),
      document.createTextNode(
        `This ZIP intersects ${entry.candidateCount} published GSA rate localities. ` +
        "The map does not choose one from ZIP geography alone."
      )
    );
    perDiem.appendChild(warning);
    if (entry.candidates && entry.candidates.length) {
      perDiem.appendChild(
        el("p", "selection-message", `Published candidates: ${entry.candidates.join("; ")}`)
      );
    }
    const link = el(
      "a",
      "ghost heat-selection-link",
      STATIC_MODE ? "Resolve with GSA" : "Resolve in rate dashboard"
    );
    link.href = STATIC_MODE ? GSA_RATE_LOOKUP : "/";
    if (STATIC_MODE) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    perDiem.appendChild(link);
  } else {
    const rates = el("dl", "selection-grid");
    [
      ["Lodging / night", entry.lodgingRate],
      ["M&IE / day", entry.mieRate],
      ["First & last day", entry.firstLastDayMie],
    ].forEach(([label, value]) => {
      rates.append(el("dt", null, label), el("dd", null, money(value)));
    });
    perDiem.appendChild(rates);
  }
  body.appendChild(perDiem);

  const rateArea = el("section", "selection-group");
  rateArea.appendChild(el("h3", null, "Rate area"));
  if (entry && entry.status !== "ambiguous") {
    rateArea.appendChild(el("p", "selection-message", entry.locality));
    rateArea.appendChild(
      el(
        "p",
        "selection-message",
        selection.locality
          ? "Boundary available: GSA definition matches complete counties."
          : "No authoritative polygon is shown for this published definition."
      )
    );
  } else {
    rateArea.appendChild(el("p", "selection-message", "No single rate area can be assigned."));
  }
  body.appendChild(rateArea);

  const geography = el("section", "selection-group");
  geography.appendChild(el("h3", null, "Geography"));
  const geographicRows = el("dl", "selection-grid");
  const municipalLabel = selection.municipal
    ? selection.municipal.properties.displayName
    : selection.mode === "zip"
      ? "Select a point to identify"
      : "Not identified";
  const countyLabel = selection.county
    ? selection.county.properties.displayName
    : (selection.countyIntersections || []).map((item) => item.properties.displayName).join("; ") ||
      "Not identified";
  geographicRows.append(
    el("dt", null, "Municipality"),
    el("dd", null, municipalLabel),
    el("dt", null, "County"),
    el("dd", null, countyLabel),
    el("dt", null, "ZIP / ZCTA"),
    el("dd", null, selection.zip ? selection.zip.properties.zip : "Not identified")
  );
  geography.appendChild(geographicRows);
  if (selection.mode === "zip" && selection.municipalIntersections.length > 1) {
    geography.appendChild(
      el(
        "p",
        "selection-message",
        `This ZCTA intersects ${selection.municipalIntersections.length} loaded municipal ` +
        "geographies. Click a point to identify the applicable one."
      )
    );
  }
  body.appendChild(geography);

  const actions = el("div", "selection-actions");
  [
    ["locality", "Highlight rate area", Boolean(selection.locality)],
    ["municipal", "Highlight municipality", Boolean(selection.municipal)],
    ["zip", "Highlight ZIP", Boolean(selection.zip)],
    ["county", "Highlight county", Boolean(selection.county)],
  ].forEach(([kind, label, available]) => {
    if (!available) return;
    const button = el("button", "ghost", label);
    button.type = "button";
    button.dataset.highlight = kind;
    button.setAttribute("aria-pressed", String(app.highlighted === kind));
    actions.appendChild(button);
  });
  if (actions.childElementCount) body.appendChild(actions);
  card.append(head, body);
  target.appendChild(card);

  target.querySelectorAll("[data-highlight]").forEach((button) => {
    button.addEventListener("click", () => highlightSelection(button.dataset.highlight));
  });
}

function renderAll() {
  renderLegend();
  renderScalePanel();
  renderSummary();
  renderSelection();
  if (app.gradientLayer) app.gradientLayer.redraw();
  if (app.statesLayer) {
    app.statesLayer.eachLayer((layer) => {
      layer.setStyle(stateStyle(layer.feature));
      layer.setTooltipContent(stateTooltip(layer.feature));
    });
  }
  if (app.zctaLayer) {
    app.zctaLayer.eachLayer((layer) => {
      layer.setStyle(zctaStyle(layer.feature));
      layer.setTooltipContent(zctaTooltip(layer.feature));
    });
  }
  updateLayerVisibility();
}

function setNationalData(data) {
  app.nationalData = data;
  app.nationalByState = new Map((data.states || []).map((entry) => [entry.state, entry]));
}

function setStateData(data) {
  app.stateData = data;
  app.stateByZip = new Map((data.rates || []).map((entry) => [entry.zip, entry]));
}

function dateQuery(state) {
  const date = encodeURIComponent(document.getElementById("date-input").value);
  return `/api/heatmap?date=${date}${state ? `&state=${encodeURIComponent(state)}` : ""}`;
}

function geoUrl(staticPath, apiPath) {
  return STATIC_MODE ? `./data/geo/${staticPath}` : `/api/geo/${apiPath}`;
}

async function loadNationalData() {
  if (!STATIC_MODE) return fetchJSON(dateQuery());
  if (!app.nationalSnapshots) {
    app.nationalSnapshots = await fetchJSON("./data/national.json");
  }
  const travelDate = document.getElementById("date-input").value;
  const snapshot = app.nationalSnapshots.dates[travelDate];
  if (!snapshot) throw new Error(`No published planning data for ${travelDate}`);
  if (snapshot.cells) return snapshot;
  const layout = app.nationalSnapshots.cellLayout || [];
  const values = snapshot.cellValues || [];
  if (layout.length !== values.length) throw new Error("Published map gradient data is incomplete");
  return {
    ...snapshot,
    cells: layout.map((cell, index) => ({
      id: cell[0],
      state: cell[1],
      latitude: cell[2],
      longitude: cell[3],
      ratedZipCount: values[index][0],
      ambiguousZipCount: values[index][1],
      lodgingRate: values[index][2],
      mieRate: values[index][3],
      firstLastDayMie: values[index][4],
    })),
  };
}

function rangesFor(items) {
  const ranges = {};
  Object.values(METRICS).forEach(({ field }) => {
    const values = items
      .map((item) => Number(item[field]))
      .filter((value) => Number.isFinite(value));
    ranges[field] = {
      min: values.length ? Math.min(...values) : null,
      max: values.length ? Math.max(...values) : null,
    };
  });
  return ranges;
}

function materializeStaticState(source, national) {
  const rates = [];
  Object.entries(source.zips || {}).forEach(([zip, intervals]) => {
    const matches = intervals.filter(
      (row) => row[0] <= national.rateDate && national.rateDate <= row[1]
    );
    if (!matches.length) return;
    if (matches.length > 1) {
      rates.push({
        zip,
        status: "ambiguous",
        candidateCount: matches.length,
        candidates: [...new Set(matches.map((row) => row[2]))].sort(),
        locality: null,
        isStandard: null,
        destinationId: null,
        county: null,
        lodgingRate: null,
        mieRate: null,
        firstLastDayMie: null,
      });
      return;
    }
    const row = matches[0];
    rates.push({
      zip,
      status: "rated",
      candidateCount: 1,
      candidates: [],
      locality: row[2],
      isStandard: Boolean(row[3]),
      lodgingRate: row[4],
      mieRate: row[5],
      firstLastDayMie: row[6],
      destinationId: row[7] || null,
      county: row[8] || null,
    });
  });
  const rated = rates.filter((item) => item.status === "rated");
  return {
    travelDate: national.travelDate,
    fiscalYear: national.fiscalYear,
    rateDate: national.rateDate,
    rateFiscalYear: national.rateFiscalYear,
    rateStatus: national.rateStatus,
    officialCoverageEnd: national.officialCoverageEnd,
    scope: source.state,
    state: source.state,
    rates,
    ranges: rangesFor(rated),
    ratedZipCount: rated.length,
    ambiguousZipCount: rates.length - rated.length,
  };
}

async function loadStateData(code, national) {
  if (!STATIC_MODE) return fetchJSON(dateQuery(code));
  if (!app.stateRateCache.has(code)) {
    app.stateRateCache.set(code, await fetchJSON(`./data/rates/${code}.json`));
  }
  return materializeStaticState(app.stateRateCache.get(code), national);
}

async function loadDate() {
  const button = document.querySelector("#heat-form button");
  button.disabled = true;
  try {
    const national = await loadNationalData();
    const state = app.activeState
      ? await loadStateData(app.activeState, national)
      : null;
    setNationalData(national);
    if (state) {
      setStateData(state);
      if (app.selection && app.selection.zip) {
        app.selection.entry = app.stateByZip.get(app.selection.zip.properties.zip) || null;
        app.selection.locality = localityForEntry(app.selection.entry, app.selection.point);
      }
    }
    renderAll();
  } finally {
    button.disabled = false;
  }
}

function watchMapSize() {
  const sync = () => app.map.invalidateSize({ animate: false, pan: false });
  if (typeof ResizeObserver === "function") {
    app.sizeObserver = new ResizeObserver(sync);
    app.sizeObserver.observe(app.map.getContainer());
  }
  window.addEventListener("resize", sync);
  window.addEventListener("pageshow", sync);
  requestAnimationFrame(sync);
}

function buildStatesLayer(geojson) {
  app.statesGeojson = geojson;
  if (!app.gradientLayer) {
    app.gradientLayer = createStateGradientLayer().addTo(app.map);
  }
  app.statesLayer = L.geoJSON(geojson, {
    renderer: app.renderers.stateBoundary,
    pane: "stateBoundaryPane",
    style: stateStyle,
    onEachFeature(feature, layer) {
      layer.bindTooltip(stateTooltip(feature), { sticky: true, className: "zcta-tip" });
      layer.on("click", (event) => {
        L.DomEvent.stopPropagation(event);
        if (!feature.properties.hasLayer) {
          toast(`${feature.properties.name} has no generated ZIP layer.`);
          return;
        }
        selectState(feature.properties.state).catch((error) => toast(error.message));
      });
      layer.on("mouseover", () => {
        if (app.mode === "national") layer.setStyle({ color: COLORS.selected, weight: 2 });
      });
      layer.on("mouseout", () => layer.setStyle(stateStyle(feature)));
    },
  }).addTo(app.map);
}

async function loadStateGeometry(kind, code) {
  if (!app.geoCache[kind].has(code)) {
    app.geoCache[kind].set(
      code,
      await fetchJSON(geoUrl(`${kind}/${code}.geojson`, `${kind}/${code}`))
    );
  }
  return app.geoCache[kind].get(code);
}

function registerFeatureLayers(kind, layerGroup) {
  const index = new Map();
  layerGroup.eachLayer((layer) => {
    const properties = layer.feature.properties;
    const key = kind === "zcta" ? properties.zip : properties.geoid || layer.feature.id;
    index.set(String(key), layer);
  });
  app.featureLayers[kind] = index;
}

function buildReferenceLayer(geojson, kind, pane) {
  const rendererName = {
    localities: "rateArea",
    counties: "county",
    municipal: "municipality",
  }[kind];
  const layer = L.geoJSON(geojson, {
    renderer: app.renderers[rendererName],
    pane,
    style: () => boundaryStyle(kind),
    onEachFeature(feature, featureLayer) {
      featureLayer.bindTooltip(featureTooltip(feature, kind), {
        sticky: true,
        className: "zcta-tip",
      });
    },
  });
  registerFeatureLayers(kind, layer);
  return layer;
}

function setLayerOnMap(layer, visible) {
  if (!layer) return;
  const present = app.map.hasLayer(layer);
  if (visible && !present) layer.addTo(app.map);
  if (!visible && present) app.map.removeLayer(layer);
}

function updateLayerVisibility() {
  if (!app.map) return;
  const zoom = app.map.getZoom();
  const inState = app.mode === "state";
  const zctaVisible = inState && (app.layerState.rate ||
    (app.layerState.zcta && zoom >= LAYER_ZOOM.zcta));
  setLayerOnMap(app.zctaLayer, zctaVisible);
  setLayerOnMap(
    app.localityLayer,
    inState && app.layerState.localities && zoom >= LAYER_ZOOM.localities
  );
  setLayerOnMap(
    app.countyLayer,
    inState && app.layerState.counties && zoom >= LAYER_ZOOM.counties
  );
  setLayerOnMap(
    app.municipalLayer,
    inState && app.layerState.municipal && zoom >= LAYER_ZOOM.municipal
  );
  if (app.zctaLayer && app.map.hasLayer(app.zctaLayer)) {
    app.zctaLayer.eachLayer((layer) => layer.setStyle(zctaStyle(layer.feature)));
  }
  const status = document.getElementById("layer-status");
  if (!inState) {
    status.textContent = "Detailed boundaries appear after selecting a state.";
    return;
  }
  const visible = [];
  if (app.layerState.localities && zoom >= LAYER_ZOOM.localities) visible.push("rate areas");
  if (app.layerState.counties && zoom >= LAYER_ZOOM.counties) visible.push("counties");
  if (app.layerState.municipal && zoom >= LAYER_ZOOM.municipal) visible.push("municipal");
  if (app.layerState.zcta && zoom >= LAYER_ZOOM.zcta) visible.push("ZCTA borders");
  status.textContent = visible.length
    ? `Zoom ${zoom}: showing ${visible.join(", ")}.`
    : `Zoom ${zoom}: zoom in for the enabled detailed boundaries.`;
}

function containingFeatures(kind, point) {
  const index = app.spatialIndexes[kind];
  return index ? index.containing(point.lat, point.lng) : [];
}

function primaryMunicipal(features) {
  return [...features].sort((left, right) => {
    const priority = Number(right.properties.priority || 0) - Number(left.properties.priority || 0);
    if (priority) return priority;
    return Number(left.properties.areaLand || Infinity) - Number(right.properties.areaLand || Infinity);
  })[0] || null;
}

function localityForEntry(entry, point = null) {
  if (!entry || entry.status === "ambiguous" || !entry.destinationId) return null;
  const candidates = (app.geoCache.localities.get(app.activeState)?.features || [])
    .filter((feature) => String(feature.properties.destinationId) === String(entry.destinationId));
  if (!candidates.length) return null;
  if (point) {
    return candidates.find((feature) => pointInGeometry(point.lng, point.lat, feature.geometry)) ||
      candidates[0];
  }
  return candidates[0];
}

function stateNameForSelection(...features) {
  const feature = features.find(Boolean);
  if (feature && feature.properties.stateName) return feature.properties.stateName;
  const state = (app.statesGeojson.features || [])
    .find((item) => item.properties.state === app.activeState);
  return state ? state.properties.name : app.activeState;
}

function buildPointSelection(point) {
  const zip = containingFeatures("zcta", point)[0] || null;
  const counties = containingFeatures("counties", point);
  const municipalMatches = containingFeatures("municipal", point);
  const municipal = primaryMunicipal(municipalMatches);
  const county = counties[0] || null;
  const entry = zip ? app.stateByZip.get(zip.properties.zip) || null : null;
  return {
    mode: "point",
    point,
    zip,
    entry,
    county,
    countyIntersections: counties,
    municipal,
    municipalIntersections: municipalMatches,
    locality: localityForEntry(entry, point),
    stateName: stateNameForSelection(municipal, county),
  };
}

function featureByGeoid(kind, geoid) {
  const layer = app.featureLayers[kind].get(String(geoid));
  return layer ? layer.feature : null;
}

function buildZipSelection(feature, center) {
  const properties = feature.properties;
  const countyIntersections = (properties.countyGeoids || [])
    .map((geoid) => featureByGeoid("counties", geoid))
    .filter(Boolean);
  const municipalGeoids = [
    ...(properties.cousubGeoids || []),
    ...(properties.placeGeoids || []),
  ];
  const municipalIntersections = municipalGeoids
    .map((geoid) => featureByGeoid("municipal", geoid))
    .filter(Boolean);
  const entry = app.stateByZip.get(properties.zip) || null;
  return {
    mode: "zip",
    point: center,
    zip: feature,
    entry,
    county: countyIntersections.length === 1 ? countyIntersections[0] : null,
    countyIntersections,
    municipal: municipalIntersections.length === 1 ? municipalIntersections[0] : null,
    municipalIntersections,
    locality: localityForEntry(entry, center),
    stateName: stateNameForSelection(countyIntersections[0], municipalIntersections[0]),
  };
}

function selectionFeatures(kind) {
  if (!app.selection) return [];
  if (kind === "locality" && app.selection.locality) {
    const destinationId = app.selection.locality.properties.destinationId;
    return (app.geoCache.localities.get(app.activeState)?.features || [])
      .filter((feature) => String(feature.properties.destinationId) === String(destinationId));
  }
  const feature = app.selection[kind];
  return feature ? [feature] : [];
}

function highlightSelection(kind) {
  const features = selectionFeatures(kind);
  if (!features.length) return;
  app.highlighted = kind;
  app.selectionLayer.clearLayers();
  L.geoJSON({ type: "FeatureCollection", features }, {
    pane: "selectionPane",
    renderer: app.renderers.selection,
    interactive: false,
    style: {
      color: COLORS.selected,
      opacity: 1,
      weight: 3.5,
      fillColor: "#ffffff",
      fillOpacity: 0.08,
    },
  }).addTo(app.selectionLayer);
  renderSelection();
}

function selectPoint(point) {
  app.selection = buildPointSelection(point);
  app.selectedZip = app.selection.zip ? app.selection.zip.properties.zip : null;
  if (app.selectedZip) document.getElementById("zip-search-input").value = app.selectedZip;
  app.highlighted = app.selection.municipal ? "municipal" : app.selection.zip ? "zip" : "county";
  renderAll();
  const features = selectionFeatures(app.highlighted);
  if (features.length) highlightSelection(app.highlighted);
}

async function selectState(code) {
  if (app.activeState === code) return;
  document.getElementById("map-hint").textContent = `Loading ${code} geography and rates…`;
  const [zcta, counties, municipal, localities, data] = await Promise.all([
    loadStateGeometry("zcta", code),
    loadStateGeometry("counties", code),
    loadStateGeometry("municipal", code),
    loadStateGeometry("localities", code),
    loadStateData(code, app.nationalData),
  ]);

  [app.zctaLayer, app.countyLayer, app.municipalLayer, app.localityLayer]
    .filter(Boolean)
    .forEach((layer) => setLayerOnMap(layer, false));
  app.activeState = code;
  app.mode = "state";
  app.selectedZip = null;
  app.selection = null;
  app.selectionLayer.clearLayers();
  if (app.statesLayer) app.statesLayer.eachLayer((layer) => layer.closeTooltip());
  setStateData(data);

  app.spatialIndexes = {
    zcta: createSpatialIndex(zcta),
    counties: createSpatialIndex(counties),
    municipal: createSpatialIndex(municipal, 0.25),
    localities: createSpatialIndex(localities),
  };
  app.zctaLayer = L.geoJSON(zcta, {
    renderer: app.renderers.rateHeat,
    pane: "rateHeatPane",
    style: zctaStyle,
    onEachFeature(feature, layer) {
      layer.bindTooltip(zctaTooltip(feature), { sticky: true, className: "zcta-tip" });
    },
  });
  registerFeatureLayers("zcta", app.zctaLayer);
  app.countyLayer = buildReferenceLayer(counties, "counties", "countyPane");
  app.municipalLayer = buildReferenceLayer(municipal, "municipal", "municipalityPane");
  app.localityLayer = buildReferenceLayer(localities, "localities", "rateAreaPane");

  const bounds = app.zctaLayer.getBounds();
  if (bounds.isValid()) app.map.fitBounds(bounds, { padding: [18, 18], animate: false });
  document.getElementById("back-to-states").hidden = false;
  document.getElementById("map-hint").textContent =
    `${code}: click anywhere to identify rate and jurisdiction.`;
  renderAll();
}

function normalizeZipSearch(raw) {
  const match = String(raw || "").trim().match(/^(\d{5})(?:-\d{4})?$/);
  if (!match) throw new Error("Enter a five-digit ZIP code.");
  return match[1];
}

async function loadZipEntry(zip) {
  if (!STATIC_MODE) return fetchJSON(`/api/zip/${encodeURIComponent(zip)}`);
  if (!app.zipIndex) app.zipIndex = await fetchJSON("./data/zip-index.json");
  const row = app.zipIndex.zips && app.zipIndex.zips[zip];
  if (!row) {
    return {
      zip,
      found: false,
      message: `ZIP ${zip} has no drawable Census ZIP area.`,
    };
  }
  return { zip, found: true, state: row[0], center: [row[1], row[2]] };
}

function layerForZip(zip) {
  return app.featureLayers.zcta.get(zip) || null;
}

async function locateZip(raw) {
  const zip = normalizeZipSearch(raw);
  const entry = await loadZipEntry(zip);
  if (!entry.found || !entry.state) {
    toast(entry.message || `ZIP ${zip} cannot be drawn on this map.`);
    return;
  }
  await selectState(entry.state);
  app.selectedZip = zip;
  document.getElementById("zip-search-input").value = zip;
  const layer = layerForZip(zip);
  if (layer) {
    app.map.fitBounds(layer.getBounds(), {
      padding: [32, 32],
      maxZoom: 12,
      animate: false,
    });
    layer.openTooltip();
    app.selection = buildZipSelection(layer.feature, layer.getBounds().getCenter());
  } else if (entry.center) {
    app.map.setView(entry.center, 11, { animate: false });
  }
  app.highlighted = "zip";
  document.getElementById("map-hint").textContent =
    `${entry.state}: ZIP ${zip} selected. Choose another ZIP or click an area.`;
  renderAll();
  if (app.selection) highlightSelection("zip");
}

function backToStates() {
  [app.zctaLayer, app.countyLayer, app.municipalLayer, app.localityLayer]
    .filter(Boolean)
    .forEach((layer) => setLayerOnMap(layer, false));
  app.zctaLayer = null;
  app.countyLayer = null;
  app.municipalLayer = null;
  app.localityLayer = null;
  app.selectionLayer.clearLayers();
  app.spatialIndexes = {};
  Object.keys(app.featureLayers).forEach((kind) => { app.featureLayers[kind] = new Map(); });
  app.activeState = null;
  app.stateData = null;
  app.stateByZip.clear();
  app.selectedZip = null;
  app.selection = null;
  app.mode = "national";
  app.map.setView(NATIONAL_VIEW.center, NATIONAL_VIEW.zoom, { animate: false });
  document.getElementById("back-to-states").hidden = true;
  document.getElementById("map-hint").textContent = "Click a state for local rates and geography.";
  renderAll();
}

function setupBasemap() {
  document.getElementById("basemap-toggle").addEventListener("change", (event) => {
    app.layerState.basemap = event.target.checked;
    if (event.target.checked) {
      if (!app.tiles) {
        app.tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
          attribution: "© OpenStreetMap contributors",
        });
      }
      app.tiles.addTo(app.map);
      app.tiles.bringToBack();
    } else if (app.tiles) {
      app.map.removeLayer(app.tiles);
    }
  });
}

function setupLayerControls() {
  const bindings = {
    "layer-rate": "rate",
    "layer-localities": "localities",
    "layer-municipal": "municipal",
    "layer-zcta": "zcta",
    "layer-counties": "counties",
  };
  Object.entries(bindings).forEach(([id, key]) => {
    document.getElementById(id).addEventListener("change", (event) => {
      app.layerState[key] = event.target.checked;
      renderAll();
    });
  });
  if (window.matchMedia("(max-width: 560px)").matches) {
    document.getElementById("layer-panel").removeAttribute("open");
  }
}

function renderContext(context) {
  const database = context.database || {};
  if (context.hosting === "github-pages") {
    document.getElementById("context-line").textContent =
      `Mobile static edition · official through ${database.coverageEnd} · ` +
      `updated ${context.generatedAt.slice(0, 10)}`;
  } else {
    document.getElementById("context-line").textContent = database.exists
      ? `${database.path} · FY ${(database.fiscalYears || []).join(", ") || "none"}`
      : `No database at ${database.path} — run a refresh first.`;
  }
  const stats = document.getElementById("masthead-stats");
  stats.replaceChildren();
  [
    [(database.zipCount || 0).toLocaleString(), "ZIP codes"],
    [(database.rateCount || 0).toLocaleString(), "Rate records"],
  ].forEach(([value, label]) => {
    const node = el("div", "stat");
    node.append(el("span", "stat-value", value), el("span", "stat-label", label));
    stats.appendChild(node);
  });
}

async function init() {
  app.map = L.map("map", {
    preferCanvas: true,
    minZoom: 3,
    maxZoom: 17,
    worldCopyJump: false,
  }).setView(NATIONAL_VIEW.center, NATIONAL_VIEW.zoom);
  [
    ["stateGradientPane", 350],
    ["rateHeatPane", 360],
    ["stateBoundaryPane", 400],
    ["countyPane", 420],
    ["municipalityPane", 430],
    ["rateAreaPane", 450],
    ["selectionPane", 470],
  ].forEach(([name, zIndex]) => {
    app.map.createPane(name);
    app.map.getPane(name).style.zIndex = String(zIndex);
  });
  app.map.getPane("stateGradientPane").style.pointerEvents = "none";
  app.map.getPane("selectionPane").style.pointerEvents = "none";
  app.renderers = {
    rateHeat: L.canvas({ padding: 0.4, pane: "rateHeatPane" }),
    stateBoundary: L.canvas({ padding: 0.4, pane: "stateBoundaryPane" }),
    county: L.canvas({ padding: 0.4, pane: "countyPane" }),
    municipality: L.canvas({ padding: 0.4, pane: "municipalityPane" }),
    rateArea: L.canvas({ padding: 0.4, pane: "rateAreaPane" }),
    selection: L.canvas({ padding: 0.4, pane: "selectionPane" }),
  };
  app.selectionLayer = L.layerGroup().addTo(app.map);
  watchMapSize();
  setupBasemap();
  setupLayerControls();
  document.getElementById("geography-provenance").href = STATIC_MODE
    ? "./data/geo/manifest.json"
    : "/api/context";

  app.map.on("zoomend", () => {
    updateLayerVisibility();
    renderLegend();
  });
  app.map.on("click", (event) => {
    if (app.mode === "state") selectPoint(event.latlng);
  });

  document.getElementById("metric-input").addEventListener("change", (event) => {
    app.metric = event.target.value;
    renderAll();
  });
  document.getElementById("heat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadDate().catch((error) => toast(error.message));
  });
  document.getElementById("zip-search").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      await locateZip(document.getElementById("zip-search-input").value);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById("back-to-states").addEventListener("click", backToStates);

  app.context = await fetchJSON(STATIC_MODE ? "./data/context.json" : "/api/context");
  renderContext(app.context);
  const dateInput = document.getElementById("date-input");
  const travelWindow = app.context.travelWindow || {};
  dateInput.min = travelWindow.start || app.context.today;
  dateInput.max = travelWindow.end || app.context.today;
  dateInput.value = app.context.today;
  const dateHelp = document.getElementById("date-help");
  if (dateHelp && travelWindow.end) {
    dateHelp.textContent =
      `Choose any date through ${travelWindow.end}. Dates beyond loaded official rates ` +
      "use the latest equivalent seasonal rate as a clearly labeled planning estimate.";
  }
  const [national, states] = await Promise.all([
    loadNationalData(),
    fetchJSON(geoUrl("states.geojson", "states")),
  ]);
  setNationalData(national);
  buildStatesLayer(states);
  renderAll();
}

init().catch((error) => {
  console.error(error);
  document.getElementById("map-hint").textContent = "The heat map could not be loaded.";
  toast(`Startup failed: ${error.message}`);
});
