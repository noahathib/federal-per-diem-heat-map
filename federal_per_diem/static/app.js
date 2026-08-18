/* Federal Per Diem dashboard.
 *
 * The map selects a ZIP; every number shown is produced by running one of the
 * project's own scripts as a child process. Command output is rendered with
 * textContent so a transcript can never inject markup.
 */
"use strict";

const NATIONAL_VIEW = { center: [39.5, -98.35], zoom: 4 };

const COLORS = {
  rated: "#2f7fb8",
  unrated: "#9aa7b4",
  selected: "#c25a12",
  stateWith: "#4d8fbd",
  stateWithout: "#b3bec9",
  stateContext: "#cfd8e0",
};

const app = {
  context: null,
  mode: "national",
  activeState: null,
  selectedZip: null,
  map: null,
  renderer: null,
  statesLayer: null,
  zctaLayer: null,
  zctaByZip: new Map(),
  layerCache: new Map(),
  tiles: null,
  busy: false,
  pendingView: null,
  sizeObserver: null,
};

/* ------------------------------------------------------------------ utils */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function money(value) {
  if (value === undefined || value === null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

function setBusy(busy) {
  app.busy = busy;
  document.querySelectorAll("button.primary, button.secondary").forEach((button) => {
    button.disabled = busy;
  });
}

/** Pull the useful sentence out of an argparse failure. */
function cleanStderr(text) {
  if (!text) return "";
  const lines = text.trimEnd().split("\n").filter((line) => !line.startsWith("usage:"));
  const last = lines[lines.length - 1] || "";
  const marker = last.indexOf(": error: ");
  return marker >= 0 ? last.slice(marker + 9) : lines.join("\n");
}

/* -------------------------------------------------------------- transcript */

function createEntry(job) {
  const terminal = document.getElementById("terminal");
  const entry = el("div", "entry");
  entry.appendChild(el("div", "entry-cmd", job.command));
  const stdout = el("pre", "entry-out");
  const stderr = el("pre", "entry-err");
  const status = el("div", "entry-status");
  entry.append(stdout, stderr, status);
  terminal.appendChild(entry);
  terminal.scrollTop = terminal.scrollHeight;
  return { entry, stdout, stderr, status };
}

function updateEntry(view, job) {
  const pinned =
    document.getElementById("terminal").scrollHeight -
      document.getElementById("terminal").scrollTop -
      document.getElementById("terminal").clientHeight < 40;
  view.stdout.textContent = job.stdout || "";
  view.stderr.textContent = job.stderr || "";
  view.status.textContent = "";
  if (job.status === "running") {
    view.status.appendChild(el("span", "running", "running…"));
  } else {
    const ok = job.returncode === 0;
    view.status.appendChild(
      el("span", ok ? "ok" : "fail", ok ? "exit 0" : `exit ${job.returncode}`)
    );
    if (job.durationMs !== null && job.durationMs !== undefined) {
      view.status.appendChild(document.createTextNode(`  ·  ${job.durationMs} ms`));
    }
    if (job.error) view.status.appendChild(document.createTextNode(`  ·  ${job.error}`));
  }
  if (pinned) {
    const terminal = document.getElementById("terminal");
    terminal.scrollTop = terminal.scrollHeight;
  }
}

async function runCommand(action, payload) {
  const response = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ action }, payload)),
  });
  const started = await response.json();
  if (!response.ok) throw new Error(started.error || "The command was rejected");

  const view = createEntry(started);
  updateEntry(view, started);
  let job = started;
  while (job.status === "running") {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const poll = await fetch(`/api/job/${job.id}`);
    if (!poll.ok) throw new Error("Lost track of the running command");
    job = await poll.json();
    updateEntry(view, job);
  }
  return job;
}

/* ------------------------------------------------------------------- cards */

function detailRow(list, label, value) {
  list.appendChild(el("dt", null, label));
  list.appendChild(el("dd", null, value));
}

function renderRate(target, rate) {
  target.replaceChildren();
  const card = el("div", "card");

  const head = el("div", "card-head");
  head.appendChild(el("div", "card-zip", rate.zip_code));
  const where = el("div", "card-where");
  where.appendChild(el("div", null, rate.locality));
  const place = [rate.city, rate.county].filter(Boolean).join(" · ");
  where.appendChild(el("div", null, place ? `${place}, ${rate.state}` : rate.state));
  head.appendChild(where);
  card.appendChild(head);

  const grid = el("div", "money-grid");
  [
    [rate.lodging_rate, "Lodging / night"],
    [rate.mie_rate, "M&IE / day"],
    [rate.first_last_day_mie, "First & last day"],
  ].forEach(([value, label]) => {
    const cell = el("div", "money");
    cell.appendChild(el("span", "money-value", money(value)));
    cell.appendChild(el("span", "money-label", label));
    grid.appendChild(cell);
  });
  card.appendChild(grid);

  const list = el("dl", "detail-list");
  detailRow(list, "Travel date", rate.travel_date);
  detailRow(list, "Fiscal year", rate.fiscal_year);
  detailRow(list, "Rate type", rate.is_standard ? "Standard / catch-all" : "Named locality");
  detailRow(list, "Destination ID", rate.destination_id);
  detailRow(list, "Source", rate.source_agency);
  detailRow(list, "Source file", rate.source_file);
  detailRow(list, "Retrieved", String(rate.source_retrieved_at).slice(0, 10));
  card.appendChild(list);

  if (rate.explanation) card.appendChild(el("p", "explain", rate.explanation));
  target.appendChild(card);
}

function renderTrip(target, trip) {
  target.replaceChildren();
  const card = el("div", "card");

  const head = el("div", "card-head");
  head.appendChild(el("div", "card-zip", trip.zip_code));
  const where = el("div", "card-where");
  where.appendChild(el("div", null, `${trip.start_date} → ${trip.end_date}`));
  where.appendChild(
    el("div", null, `${trip.travel_days} travel days · ${trip.lodging_nights} nights`)
  );
  head.appendChild(where);
  card.appendChild(head);

  const grid = el("div", "money-grid");
  [
    [trip.lodging_allowance, "Lodging"],
    [trip.total_mie, "Total M&IE"],
    [trip.per_person_total, "Per person"],
  ].forEach(([value, label]) => {
    const cell = el("div", "money");
    cell.appendChild(el("span", "money-value", money(value)));
    cell.appendChild(el("span", "money-label", label));
    grid.appendChild(cell);
  });
  card.appendChild(grid);

  const list = el("dl", "detail-list");
  detailRow(list, "Travelers", trip.travelers);
  detailRow(list, "First day M&IE", money(trip.first_day_mie));
  detailRow(list, "Last day M&IE", money(trip.last_day_mie));
  detailRow(list, `Full days (${trip.full_mie_days})`, money(trip.full_day_mie));
  detailRow(list, "Mileage", money(trip.mileage_allowance));
  detailRow(list, "Group total", money(trip.group_total));
  card.appendChild(list);
  target.appendChild(card);
}

function renderNotice(target, kind, title, message, raw) {
  target.replaceChildren();
  const notice = el("div", `notice is-${kind}`);
  notice.appendChild(el("h3", null, title));
  if (message) notice.appendChild(el("p", null, message));
  if (raw) notice.appendChild(el("pre", null, raw));
  target.appendChild(notice);
}

/* ---------------------------------------------------------------- map sizing */

/* Leaflet measures the map container once at construction and afterwards only
 * on a window resize. If the page is laid out while the map is collapsed or
 * hidden — a background tab, a window still settling, a pane opened later — the
 * cached size stays wrong, the canvas renderer is built at that wrong size, and
 * every layer draws into nothing: data loads, legend fills, map stays blank.
 * Watch the element itself so the map is re-measured whenever it really changes.
 */
function watchMapSize() {
  const container = app.map.getContainer();
  const sync = () => {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return;
    const size = app.map.getSize();
    if (size.x === width && size.y === height) {
      flushPendingView();
      return;
    }
    app.map.invalidateSize({ animate: false, pan: false });
    flushPendingView();
  };
  if (typeof ResizeObserver === "function") {
    app.sizeObserver = new ResizeObserver(sync);
    app.sizeObserver.observe(container);
  }
  window.addEventListener("resize", sync);
  window.addEventListener("load", sync);
  window.addEventListener("pageshow", sync);
  document.addEventListener("visibilitychange", sync);
  // `--open` launches the browser as the page loads, so the first layout can
  // happen in a window that is still being sized. Re-measure once painting
  // starts rather than trusting the size the map was constructed with.
  requestAnimationFrame(sync);
  sync();
}

/* Any view change resolved against a zero-size container comes out wrong: a fit
 * cannot satisfy its bounds so Leaflet returns maxZoom, and a centre/zoom lands
 * off-target. Hold the intent instead and apply it once the container measures.
 */
function applyView(intent) {
  if (intent.bounds) {
    app.map.fitBounds(intent.bounds, intent.options);
  } else {
    app.map.setView(intent.center, intent.zoom);
  }
}

function viewWhenSized(intent) {
  if (intent.bounds && !intent.bounds.isValid()) return;
  // Tag the intent with the view that asked for it. By the time the container has
  // a size the user may have gone back or picked another state, and replaying it
  // then would drag them back to where they no longer are.
  const tagged = Object.assign({}, intent, {
    mode: app.mode,
    state: app.activeState,
    zip: app.selectedZip,
  });
  const size = app.map.getSize();
  if (size.x > 0 && size.y > 0) {
    app.pendingView = null;
    applyView(tagged);
    return;
  }
  app.pendingView = tagged;
}

function flushPendingView() {
  const pending = app.pendingView;
  if (!pending) return;
  app.pendingView = null;
  const stale =
    pending.mode !== app.mode ||
    pending.state !== app.activeState ||
    pending.zip !== app.selectedZip;
  if (stale) return;
  applyView(pending);
}

function fitWhenSized(bounds, options) {
  viewWhenSized({ bounds, options });
}

/* --------------------------------------------------------------- map layers */

function styleState(feature) {
  const has = feature.properties.hasLayer;
  // With a state selected its ZCTAs are the subject, so the country recedes to a
  // flat backdrop. It must stay drawn: no basemap sits behind it, so hiding the
  // fills leaves the rest of the map looking empty.
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
    weight: 1,
    fillColor: has ? COLORS.stateWith : COLORS.stateWithout,
    fillOpacity: has ? 0.72 : 0.4,
  };
}

function styleZcta(feature) {
  const selected = feature.properties.zip === app.selectedZip;
  if (selected) {
    return { color: COLORS.selected, weight: 2.4, fillColor: COLORS.selected, fillOpacity: 0.5 };
  }
  const rated = feature.properties.inDatabase;
  return {
    color: "#ffffff",
    weight: 0.45,
    fillColor: rated ? COLORS.rated : COLORS.unrated,
    fillOpacity: rated ? 0.5 : 0.24,
  };
}

function setLegend(items) {
  const legend = document.getElementById("map-legend");
  legend.replaceChildren();
  legend.appendChild(el("b", null, items.title));
  items.rows.forEach(([color, label]) => {
    const row = el("div");
    const swatch = el("span", "swatch");
    swatch.style.background = color;
    row.appendChild(swatch);
    row.appendChild(document.createTextNode(label));
    legend.appendChild(row);
  });
}

async function loadStates() {
  const response = await fetch("/api/geo/states");
  if (!response.ok) throw new Error("State boundaries have not been generated");
  const geojson = await response.json();
  app.statesLayer = L.geoJSON(geojson, {
    renderer: app.renderer,
    style: styleState,
    onEachFeature(feature, layer) {
      const p = feature.properties;
      layer.bindTooltip(
        `${p.name} — ${p.zctaCount.toLocaleString()} ZCTAs`,
        { sticky: true, className: "zcta-tip" }
      );
      layer.on("click", (event) => {
        L.DomEvent.stopPropagation(event);
        if (!p.hasLayer) {
          toast(`${p.name} has no generated ZIP layer.`);
          return;
        }
        selectState(p.state);
      });
      layer.on("mouseover", () => {
        if (app.mode === "national") layer.setStyle({ weight: 2, color: "#12181f" });
      });
      layer.on("mouseout", () => app.statesLayer.resetStyle(layer));
    },
  }).addTo(app.map);
  setLegend({
    title: "States",
    rows: [
      [COLORS.stateWith, "ZIP layer available"],
      [COLORS.stateWithout, "No ZIP layer"],
    ],
  });
}

async function selectState(code, options) {
  if (app.activeState === code) return;
  const fitToState = !options || options.fit !== false;
  document.getElementById("map-hint").textContent = `Loading ${code} ZIP areas…`;
  let geojson = app.layerCache.get(code);
  if (!geojson) {
    const response = await fetch(`/api/geo/zcta/${code}`);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      toast(detail.error || `Could not load the ${code} layer`);
      document.getElementById("map-hint").textContent = "Click a state to load its ZIP areas.";
      return;
    }
    geojson = await response.json();
    app.layerCache.set(code, geojson);
  }

  if (app.zctaLayer) app.map.removeLayer(app.zctaLayer);
  app.zctaByZip.clear();
  app.activeState = code;
  app.mode = "state";

  app.zctaLayer = L.geoJSON(geojson, {
    renderer: app.renderer,
    style: styleZcta,
    onEachFeature(feature, layer) {
      app.zctaByZip.set(feature.properties.zip, layer);
      layer.bindTooltip(feature.properties.zip, { sticky: true, className: "zcta-tip" });
    },
  }).addTo(app.map);

  if (app.statesLayer) app.statesLayer.setStyle(styleState);
  // Fit without animation: an animated fit here would still be running when a
  // caller that already knows its target ZIP zooms in, and would override it.
  if (fitToState) {
    fitWhenSized(app.zctaLayer.getBounds(), { padding: [18, 18], animate: false });
  }

  document.getElementById("back-to-states").hidden = false;
  document.getElementById("map-hint").textContent =
    `${code}: click anywhere to resolve the ZIP under the pointer.`;
  setLegend({
    title: `${code} ZIP areas`,
    rows: [
      [COLORS.rated, "Has a published rate"],
      [COLORS.unrated, "No rate in this database"],
      [COLORS.selected, "Selected"],
    ],
  });
  if (app.selectedZip) highlightZip(app.selectedZip, false);
}

function backToStates() {
  if (app.zctaLayer) {
    app.map.removeLayer(app.zctaLayer);
    app.zctaLayer = null;
  }
  app.zctaByZip.clear();
  app.activeState = null;
  app.mode = "national";
  app.pendingView = null;
  if (app.statesLayer) app.statesLayer.setStyle(styleState).eachLayer((layer) => {
    app.statesLayer.resetStyle(layer);
    // A sticky tooltip open when the pointer left the map for the toolbar button
    // never receives the mouseout that would close it.
    layer.closeTooltip();
  });
  viewWhenSized({ center: NATIONAL_VIEW.center, zoom: NATIONAL_VIEW.zoom });
  document.getElementById("back-to-states").hidden = true;
  document.getElementById("map-hint").textContent =
    "Click a state to load its ZIP Code Tabulation Areas.";
  setLegend({
    title: "States",
    rows: [
      [COLORS.stateWith, "ZIP layer available"],
      [COLORS.stateWithout, "No ZIP layer"],
    ],
  });
}

function highlightZip(zip, zoom) {
  if (!app.zctaLayer) return;
  app.zctaLayer.setStyle(styleZcta);
  const layer = app.zctaByZip.get(zip);
  if (!layer) return;
  layer.setStyle(styleZcta(layer.feature));
  layer.bringToFront();
  if (zoom) fitWhenSized(layer.getBounds(), { padding: [60, 60], maxZoom: 13 });
}

/* ------------------------------------------------------------- ZIP selection */

/** Adopt a ZIP as the current selection and run the rate query for it. */
async function selectZip(zip, options) {
  const settings = options || {};
  app.selectedZip = zip;
  document.getElementById("zip-input").value = zip;
  document.getElementById("trip-zip").value = zip;

  if (settings.locateOnMap !== false) {
    const zoomToZip = settings.zoom !== false;
    const response = await fetch(`/api/zip/${zip}`);
    const entry = await response.json();
    if (entry.found) {
      if (entry.state && entry.state !== app.activeState) {
        await selectState(entry.state, { fit: !zoomToZip });
      }
      highlightZip(zip, zoomToZip);
    } else if (entry.message) {
      toast(entry.message);
    }
  } else {
    highlightZip(zip, false);
  }
  await lookupRate();
}

async function locatePoint(latlng) {
  const response = await fetch("/api/locate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ latitude: latlng.lat, longitude: latlng.lng }),
  });
  const result = await response.json();
  if (!response.ok) { toast(result.error || "Could not resolve that point"); return; }
  if (!result.found) { toast(result.message || "No ZIP area near that point"); return; }
  if (!result.exact) {
    toast(
      `No ZIP area covers that point. Nearest is ${result.zip}, ` +
      `about ${result.distance_km.toFixed(1)} km away.`
    );
  }
  await selectZip(result.zip, { zoom: false });
}

/* -------------------------------------------------------------- commands */

async function lookupRate() {
  const zip = document.getElementById("zip-input").value.trim();
  const date = document.getElementById("date-input").value;
  const target = document.getElementById("rate-result");
  if (!zip || !date) { toast("A ZIP code and travel date are required."); return; }

  setBusy(true);
  try {
    const job = await runCommand("query", {
      zip,
      date,
      explain: document.getElementById("explain-input").checked,
    });
    if (job.returncode === 0 && job.parsed) {
      renderRate(target, job.parsed);
    } else {
      const detail = cleanStderr(job.stderr) || job.error || "The command failed.";
      const ambiguous = detail.includes("multiple published rate localities");
      renderNotice(
        target,
        ambiguous ? "warn" : "error",
        ambiguous ? "This ZIP spans more than one locality" : "No rate returned",
        detail
      );
    }
  } catch (error) {
    renderNotice(target, "error", "Could not run the command", error.message);
  } finally {
    setBusy(false);
  }
}

async function estimateTrip(event) {
  event.preventDefault();
  const target = document.getElementById("trip-result");
  const payload = {
    zip: document.getElementById("trip-zip").value.trim(),
    startDate: document.getElementById("trip-start").value,
    endDate: document.getElementById("trip-end").value,
    travelers: Number(document.getElementById("trip-travelers").value) || 1,
  };
  const miles = document.getElementById("trip-mileage").value.trim();
  const rate = document.getElementById("trip-mileage-rate").value.trim();
  if (miles) payload.mileage = miles;
  if (rate) payload.mileageRate = rate;

  setBusy(true);
  try {
    const job = await runCommand("estimate", payload);
    if (job.returncode === 0 && job.parsed) {
      renderTrip(target, job.parsed);
    } else {
      renderNotice(
        target,
        "error",
        "No estimate returned",
        cleanStderr(job.stderr) || job.error || "The command failed."
      );
    }
  } catch (error) {
    renderNotice(target, "error", "Could not run the command", error.message);
  } finally {
    setBusy(false);
  }
}

async function runOps(action) {
  const target = document.getElementById("ops-result");
  renderNotice(target, "warn", "Running…", "Watch the transcript below for live output.");
  setBusy(true);
  try {
    const job = await runCommand(action, {});
    const ok = job.returncode === 0;
    if (job.parsed) {
      const report = job.parsed;
      const issues = report.issues || [];
      renderNotice(
        target,
        report.valid ? "good" : "error",
        report.valid ? "Database is valid" : "Validation failed",
        `${issues.length} issue(s) reported.`,
        JSON.stringify(report.metrics, null, 2)
      );
    } else {
      renderNotice(
        target,
        ok ? "good" : "error",
        ok ? "Command finished" : `Command failed (exit ${job.returncode})`,
        job.command,
        (job.stdout || job.stderr || "").slice(-4000)
      );
    }
  } catch (error) {
    renderNotice(target, "error", "Could not run the command", error.message);
  } finally {
    setBusy(false);
  }
}

/** List the commands the dashboard deliberately will not run for you. */
function renderManualCommands(commands) {
  const target = document.getElementById("manual-commands");
  target.replaceChildren();
  (commands || []).forEach((entry) => {
    const row = el("div", "manual");
    row.appendChild(el("div", "manual-label", entry.label));
    const line = el("code", "manual-command", entry.command);
    const copy = el("button", "ghost manual-copy", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(entry.command);
        toast("Command copied.");
      } catch (error) {
        toast("Select the command and copy it manually.");
      }
    });
    row.append(line, copy);
    target.appendChild(row);
  });
}

/* ------------------------------------------------------------------- setup */

function renderContext(context) {
  const database = context.database || {};
  const map = context.map || {};
  document.getElementById("context-line").textContent = database.exists
    ? `${database.path} · FY ${(database.fiscalYears || []).join(", ") || "none"}`
    : `No database at ${database.path} — run a refresh first.`;

  const stats = document.getElementById("masthead-stats");
  stats.replaceChildren();
  const entries = [
    [(database.zipCount || 0).toLocaleString(), "ZIP codes"],
    [(database.rateCount || 0).toLocaleString(), "Rate records"],
    [(map.zctaCount || 0).toLocaleString(), "Mapped ZCTAs"],
  ];
  if (database.lastRefresh) {
    entries.push([String(database.lastRefresh.completedAt).slice(0, 10), "Last refresh"]);
  }
  entries.forEach(([value, label]) => {
    const stat = el("div", "stat");
    stat.appendChild(el("span", "stat-value", value));
    stat.appendChild(el("span", "stat-label", label));
    stats.appendChild(stat);
  });

  const today = context.today;
  const dateInput = document.getElementById("date-input");
  if (!dateInput.value) dateInput.value = today;
  const start = document.getElementById("trip-start");
  const end = document.getElementById("trip-end");
  if (!start.value) start.value = today;
  if (!end.value) {
    const later = new Date(`${today}T00:00:00`);
    later.setDate(later.getDate() + 3);
    end.value = later.toISOString().slice(0, 10);
  }
  renderManualCommands(context.manualCommands);
}

async function loadContext() {
  const response = await fetch("/api/context");
  app.context = await response.json();
  renderContext(app.context);
  return app.context;
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((other) => other.classList.remove("is-active"));
      document.querySelectorAll(".panel").forEach((panel) => panel.classList.remove("is-active"));
      tab.classList.add("is-active");
      document.getElementById(tab.dataset.panel).classList.add("is-active");
    });
  });
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

async function init() {
  setupTabs();
  setupBasemap();

  app.renderer = L.canvas({ padding: 0.4 });
  app.map = L.map("map", {
    preferCanvas: true,
    renderer: app.renderer,
    minZoom: 3,
    maxZoom: 17,
    worldCopyJump: false,
  }).setView(NATIONAL_VIEW.center, NATIONAL_VIEW.zoom);
  watchMapSize();
  // The constructor above has to set a view before any layer can be added, and it
  // resolves against whatever the container measured then. Claim it again through
  // the guarded path so a zero-size first layout cannot leave a bogus centre.
  viewWhenSized({ center: NATIONAL_VIEW.center, zoom: NATIONAL_VIEW.zoom });

  app.map.on("click", (event) => {
    if (app.mode !== "state" || app.busy) return;
    locatePoint(event.latlng).catch((error) => toast(error.message));
  });

  document.getElementById("back-to-states").addEventListener("click", backToStates);
  document.getElementById("clear-terminal").addEventListener("click", () => {
    document.getElementById("terminal").replaceChildren();
  });
  document.getElementById("rate-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const raw = document.getElementById("zip-input").value.trim();
    const zip = /^\d{5}(-\d{4})?$/.test(raw) ? raw.slice(0, 5) : raw;
    selectZip(zip, {}).catch((error) => toast(error.message));
  });
  document.getElementById("trip-form").addEventListener("submit", estimateTrip);
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => runOps(button.dataset.action));
  });

  const context = await loadContext();
  if (context.map && context.map.available) {
    await loadStates();
    if (!context.map.exactHitTesting) {
      toast("The source shapefile is missing, so clicks cannot be resolved exactly.");
    }
  } else {
    document.getElementById("map-hint").textContent =
      "No map layers found. Run: python scripts/build_map_data.py";
    toast((context.map && context.map.error) || "Map layers have not been generated.");
  }
}

init().catch((error) => {
  console.error(error);
  toast(`Startup failed: ${error.message}`);
});
