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
};

const app = {
  context: null,
  metric: "lodging",
  mode: "national",
  activeState: null,
  selectedZip: null,
  map: null,
  renderer: null,
  statesLayer: null,
  gradientLayer: null,
  zctaLayer: null,
  statesGeojson: null,
  stateGeoCache: new Map(),
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
      fillOpacity: 0.55,
    };
  }
  return {
    color: "#ffffff",
    weight: 1.15,
    fillColor: "#ffffff",
    fillOpacity: feature.properties.hasLayer ? 0.015 : 0.2,
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

  const sampleSize = 7;
  const fieldWidth = Math.max(1, Math.ceil(width / sampleSize));
  const fieldHeight = Math.max(1, Math.ceil(height / sampleSize));
  const fieldCanvas = document.createElement("canvas");
  fieldCanvas.width = fieldWidth;
  fieldCanvas.height = fieldHeight;
  const fieldContext = fieldCanvas.getContext("2d");
  const image = fieldContext.createImageData(fieldWidth, fieldHeight);
  const xScale = width / fieldWidth;
  const yScale = height / fieldHeight;
  const distanceFloor = Math.max(16, width * height * 0.0025);

  for (let y = 0; y < fieldHeight; y += 1) {
    const sampleY = minY + (y + 0.5) * yScale;
    for (let x = 0; x < fieldWidth; x += 1) {
      const sampleX = minX + (x + 0.5) * xScale;
      let weightedValue = 0;
      let totalWeight = 0;
      fieldCells.forEach((cell) => {
        const dx = sampleX - cell.x;
        const dy = sampleY - cell.y;
        const weight = 1 / (dx * dx + dy * dy + distanceFloor);
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
      map.getPane("stateGradientPane").appendChild(this._canvas);
      map.on("moveend zoomend resize viewreset", this.redraw, this);
      this.redraw();
    },
    onRemove(map) {
      map.off("moveend zoomend resize viewreset", this.redraw, this);
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
      if (app.mode !== "national" || !app.nationalData || !app.statesGeojson) {
        canvas.style.display = "none";
        return;
      }
      canvas.style.display = "block";
      const map = this._map;
      const size = map.getSize();
      const ratio = window.devicePixelRatio || 1;
      const topLeft = map.containerPointToLayerPoint([0, 0]);
      L.DomUtil.setPosition(canvas, topLeft);
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
  });
  return new StateGradientLayer();
}

function zctaStyle(feature) {
  const zip = feature.properties.zip;
  const entry = app.stateByZip.get(zip);
  const selected = zip === app.selectedZip;
  let fillColor = COLORS.noRate;
  let fillOpacity = 0.35;
  if (entry && entry.status === "ambiguous") {
    fillColor = COLORS.ambiguous;
    fillOpacity = 0.72;
  } else if (entry) {
    fillColor = heatColor(entry[currentMetric().field], currentRange());
    fillOpacity = 0.78;
  }
  return {
    color: selected ? COLORS.selected : "#ffffff",
    weight: selected ? 2.3 : 0.45,
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

function renderLegend() {
  const legend = document.getElementById("map-legend");
  legend.replaceChildren();
  legend.appendChild(el("b", null, currentMetric().label));
  appendScale(legend, currentRange());
  if (app.mode === "state") addLegendKey(legend, COLORS.ambiguous, "Multiple rate localities");
  addLegendKey(legend, COLORS.noRate, "No single rate");
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
        : "Each state fill smoothly blends coarse regional medians from its underlying ZIP rates."
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
  if (app.mode !== "state" || !app.selectedZip) return;
  const entry = app.stateByZip.get(app.selectedZip);
  target.appendChild(el("h2", null, "Selected ZIP area"));
  const card = el("div", "heat-selection");
  const head = el("div", "heat-selection-head");
  head.appendChild(el("strong", null, app.selectedZip));
  const body = el("div", "heat-selection-body");
  if (!entry) {
    body.appendChild(el("span", "heat-selection-value", "No rate"));
    body.appendChild(el("span", "heat-selection-label", "No published rate for this date"));
  } else if (entry.status === "ambiguous") {
    body.appendChild(el("span", "heat-selection-value", "Multiple rates"));
    body.appendChild(
      el("span", "heat-selection-label", `${entry.candidateCount} official localities intersect this ZIP`)
    );
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
    body.appendChild(link);
  } else {
    body.appendChild(el("span", "heat-selection-value", money(entry[currentMetric().field])));
    body.appendChild(el("span", "heat-selection-label", currentMetric().label));
    body.appendChild(el("span", "heat-selection-locality", entry.locality));
  }
  card.append(head, body);
  target.appendChild(card);
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
        locality: null,
        isStandard: null,
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
      locality: row[2],
      isStandard: Boolean(row[3]),
      lodgingRate: row[4],
      mieRate: row[5],
      firstLastDayMie: row[6],
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
    if (state) setStateData(state);
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
    renderer: app.renderer,
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

async function selectState(code) {
  if (app.activeState === code) return;
  document.getElementById("map-hint").textContent = `Loading ${code} ZIP rates…`;
  const geoPromise = app.stateGeoCache.has(code)
    ? Promise.resolve(app.stateGeoCache.get(code))
    : fetchJSON(geoUrl(`zcta/${code}.geojson`, `zcta/${code}`));
  const [geojson, data] = await Promise.all([
    geoPromise,
    loadStateData(code, app.nationalData),
  ]);
  app.stateGeoCache.set(code, geojson);

  if (app.zctaLayer) app.map.removeLayer(app.zctaLayer);
  app.activeState = code;
  app.mode = "state";
  app.selectedZip = null;
  if (app.statesLayer) {
    app.statesLayer.eachLayer((layer) => layer.closeTooltip());
  }
  setStateData(data);
  app.zctaLayer = L.geoJSON(geojson, {
    renderer: app.renderer,
    style: zctaStyle,
    onEachFeature(feature, layer) {
      layer.bindTooltip(zctaTooltip(feature), { sticky: true, className: "zcta-tip" });
      layer.on("click", (event) => {
        L.DomEvent.stopPropagation(event);
        app.selectedZip = feature.properties.zip;
        document.getElementById("zip-search-input").value = app.selectedZip;
        renderAll();
      });
    },
  }).addTo(app.map);
  app.map.fitBounds(app.zctaLayer.getBounds(), { padding: [18, 18], animate: false });
  document.getElementById("back-to-states").hidden = false;
  document.getElementById("map-hint").textContent = `${code}: select a ZIP area for its rate.`;
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
  let match = null;
  if (!app.zctaLayer) return match;
  app.zctaLayer.eachLayer((layer) => {
    if (layer.feature && layer.feature.properties.zip === zip) match = layer;
  });
  return match;
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
  } else if (entry.center) {
    app.map.setView(entry.center, 11, { animate: false });
  }
  document.getElementById("map-hint").textContent =
    `${entry.state}: ZIP ${zip} selected. Choose another ZIP or click an area.`;
  renderAll();
}

function backToStates() {
  if (app.zctaLayer) app.map.removeLayer(app.zctaLayer);
  app.zctaLayer = null;
  app.activeState = null;
  app.stateData = null;
  app.stateByZip.clear();
  app.selectedZip = null;
  app.mode = "national";
  app.map.setView(NATIONAL_VIEW.center, NATIONAL_VIEW.zoom, { animate: false });
  document.getElementById("back-to-states").hidden = true;
  document.getElementById("map-hint").textContent = "Click a state for ZIP-level rates.";
  renderAll();
}

function setupBasemap() {
  document.getElementById("basemap-toggle").addEventListener("change", (event) => {
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
  app.renderer = L.canvas({ padding: 0.4 });
  app.map = L.map("map", {
    preferCanvas: true,
    renderer: app.renderer,
    minZoom: 3,
    maxZoom: 17,
    worldCopyJump: false,
  }).setView(NATIONAL_VIEW.center, NATIONAL_VIEW.zoom);
  app.map.createPane("stateGradientPane");
  app.map.getPane("stateGradientPane").style.zIndex = "350";
  app.map.getPane("stateGradientPane").style.pointerEvents = "none";
  watchMapSize();
  setupBasemap();

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
