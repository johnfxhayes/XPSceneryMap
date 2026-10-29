const KIND_LABEL = { land: "Airport", sea: "Seaport", heli: "Heliport" };
const REASON_ORDER = [
  "missing folder",
  "could not read airport data",
  "no airport location",
  "too many airports",
  "no airport data",
];

const searchInput = document.querySelector("#search");
const kindInputs = document.querySelectorAll('input[name="kind"]');
const statusNode = document.querySelector("#status");
const listNode = document.querySelector("#list");
const skippedNode = document.querySelector("#skipped");
const rootNode = document.querySelector("#root");

const map = L.map("map", { worldCopyJump: false });
map.createPane("xpme");
map.getPane("xpme").style.zIndex = "340";
// Fade the whole layer once. Per-shape transparency mixes where packs overlap
// and draws darker seams where neighboring blocks meet.
map.getPane("xpme").style.opacity = "0.42";
map.createPane("ortho");
map.getPane("ortho").style.zIndex = "350";
// Leaflet's own credit is optional. The map tiles still need the OpenStreetMap credit.
map.attributionControl.setPrefix(false);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);
map.setView([20, 0], 2);

const xpmeRenderer = L.canvas({ pane: "xpme", padding: 0.5 });
const xpmeLayer = L.layerGroup().addTo(map);
const orthoLayer = L.layerGroup().addTo(map);
const markers = L.layerGroup().addTo(map);
const markerById = new Map();
const rectById = new Map();
const xpmeById = new Map();
let airports = [];
let skipped = [];
let orthoTiles = [];
let xpmeAreas = [];
let xpmeOverlaps = [];
const stripePatterns = new Map();
let selectedId = null;
let shiftKey = "";
let drawnOrtho = [];
let drawnXpme = [];
let drawnOverlaps = [];
let drawnAirports = [];

const legend = L.control({ position: "bottomright" });
let legendNode = null;
legend.onAdd = function () {
  legendNode = L.DomUtil.create("div", "legend");
  drawLegend([]);
  return legendNode;
};

const packChangesDialog = document.querySelector("#pack-changes");
const packChangesSummary = document.querySelector("#pack-changes-summary");
const packChangesBody = document.querySelector("#pack-changes-body");
const packChangesError = document.querySelector("#pack-changes-error");
const packChangesOk = document.querySelector("#pack-changes-ok");
const rescanButton = document.querySelector("#rescan");
const updateButton = document.querySelector("#update-root");
const themeButton = document.querySelector("#theme");
const orthoInput = document.querySelector("#ortho");
const xpmeInput = document.querySelector("#xpme");
const packageSearch = document.querySelector("#package-search");
const packageList = document.querySelector("#package-list");
const packagesHidden = document.querySelector("#packages-hidden");
const hiddenPacks = new Set(readHiddenPacks());
let packageSort = { key: "name", dir: "asc" };
const packageResize = document.querySelector("#package-resize");
const PACKAGE_LIST_MIN = 72;
const PACKAGE_LIST_DEFAULT = 168;
const AIRPORT_LIST_MIN = 160;
let packageListDesired = PACKAGE_LIST_DEFAULT;
legend.addTo(map);

searchInput.addEventListener("input", () => render(false));
for (const input of kindInputs) {
  input.addEventListener("change", () => {
    render(true);
    saveFilters();
  });
}
orthoInput.addEventListener("change", () => {
  render(false);
  saveFilters();
});
xpmeInput.addEventListener("change", () => {
  render(false);
  saveFilters();
});
packageSearch.addEventListener("input", () => drawPackageList());
setupPackageResize();
map.on("moveend", () => {
  const key = worldShifts().join(",");
  if (key === shiftKey) {
    return;
  }
  drawWorld(drawnOrtho, drawnXpme, drawnOverlaps, drawnAirports);
});
rescanButton.addEventListener("click", rescan);
updateButton.addEventListener("click", updateRoot);
themeButton.addEventListener("click", toggleTheme);
packChangesDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
});
packChangesOk.addEventListener("click", acknowledgePackChanges);
skippedNode.addEventListener("toggle", () => {
  requestAnimationFrame(fitPackageList);
}, true);

load();

function currentFilters() {
  const filters = {
    ortho: orthoInput.checked,
    xpme: xpmeInput.checked,
  };
  for (const input of kindInputs) {
    filters[input.value] = input.checked;
  }
  return filters;
}

function readPackageListHeight() {
  const node = document.querySelector("#package-list-height");
  if (!node || !node.textContent || node.textContent.trim() === "null") {
    return null;
  }
  const value = Number(node.textContent);
  return Number.isFinite(value) ? value : null;
}

function packageListLimit(startHeight) {
  const room = listNode.getBoundingClientRect().height - AIRPORT_LIST_MIN;
  return Math.max(PACKAGE_LIST_MIN, startHeight + Math.max(0, room));
}

function applyPackageListHeight(height) {
  const next = Math.round(height);
  packageList.style.height = `${next}px`;
  packageList.style.maxHeight = `${next}px`;
  packageResize.setAttribute("aria-valuenow", String(next));
}

function clampPackageListHeight(height, startHeight) {
  const max = packageListLimit(startHeight ?? packageList.getBoundingClientRect().height);
  return Math.round(Math.min(max, Math.max(PACKAGE_LIST_MIN, height)));
}

async function savePackageListHeight(height) {
  try {
    const response = await fetch("/api/filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: currentFilters(), package_list_height: height }),
    });
    if (!response.ok) {
      throw new Error("Could not save the package list size.");
    }
  } catch (error) {
    statusNode.textContent = error.message || "Could not save the package list size.";
  }
}

function fitSkippedLists() {
  const skipped = document.querySelector("#skipped");
  if (!skipped || skipped.hidden) {
    return;
  }
  const lists = [...skipped.querySelectorAll("details[open] ul")];
  for (const ul of skipped.querySelectorAll("details:not([open]) ul")) {
    ul.style.maxHeight = "";
  }
  if (!lists.length) {
    return;
  }
  const aside = document.querySelector("aside");
  const asideTop = aside.getBoundingClientRect().top;
  const aboveList = listNode.getBoundingClientRect().top - asideTop;
  const aboveWithoutPackages = aboveList - packageList.getBoundingClientRect().height;
  let chrome = skipped.getBoundingClientRect().height;
  for (const ul of lists) {
    chrome -= ul.getBoundingClientRect().height;
  }
  const cap = Math.round(window.innerHeight * 0.3);
  const maxUl = aside.clientHeight - aboveWithoutPackages - PACKAGE_LIST_MIN - AIRPORT_LIST_MIN - chrome;
  const preferred = aside.clientHeight - aboveWithoutPackages - packageListDesired - AIRPORT_LIST_MIN - chrome;
  const floor = Math.min(120, Math.max(0, maxUl));
  const each = Math.min(Math.max(0, maxUl), Math.max(Math.min(cap, Math.max(0, preferred)), floor)) / lists.length;
  for (const ul of lists) {
    ul.style.maxHeight = `${Math.max(0, Math.floor(each))}px`;
  }
}

function fitPackageList() {
  if (packageResize.classList.contains("dragging")) {
    return;
  }
  fitSkippedLists();
  const aside = document.querySelector("aside");
  const asideBottom = aside.getBoundingClientRect().bottom;
  const listTop = packageList.getBoundingClientRect().top;
  if (asideBottom - listTop < 100) {
    return;
  }
  const gripStyles = getComputedStyle(packageResize);
  const gripBlock = packageResize.offsetHeight
    + parseFloat(gripStyles.marginTop)
    + parseFloat(gripStyles.marginBottom);
  const statusStyles = getComputedStyle(statusNode);
  const statusBlock = statusNode.offsetHeight
    + parseFloat(statusStyles.marginTop)
    + parseFloat(statusStyles.marginBottom);
  const skipped = document.querySelector("#skipped");
  const skippedBlock = skipped && !skipped.hidden ? skipped.offsetHeight : 0;
  const available = asideBottom - listTop - gripBlock - statusBlock - skippedBlock - AIRPORT_LIST_MIN;
  const next = Math.round(Math.max(PACKAGE_LIST_MIN, Math.min(packageListDesired, available)));
  if (Math.abs(next - packageList.getBoundingClientRect().height) < 2) {
    return;
  }
  applyPackageListHeight(next);
  fitSkippedLists();
}

function setupPackageResize() {
  packageListDesired = readPackageListHeight() || PACKAGE_LIST_DEFAULT;
  packageResize.setAttribute("aria-valuemin", String(PACKAGE_LIST_MIN));
  window.addEventListener("resize", fitPackageList);
  requestAnimationFrame(fitPackageList);
  packageResize.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    packageResize.classList.add("dragging");
    try {
      packageResize.setPointerCapture(event.pointerId);
    } catch {
      // A real drag captures the pointer. A synthetic event cannot.
    }
    const startY = event.clientY;
    const startHeight = packageList.getBoundingClientRect().height;
    const max = packageListLimit(startHeight);
    const move = (pointer) => {
      const next = Math.min(max, Math.max(PACKAGE_LIST_MIN, startHeight + (pointer.clientY - startY)));
      applyPackageListHeight(next);
    };
    const end = () => {
      packageResize.removeEventListener("pointermove", move);
      packageResize.removeEventListener("pointerup", end);
      packageResize.removeEventListener("pointercancel", end);
      packageResize.classList.remove("dragging");
      packageListDesired = Math.round(parseFloat(packageList.style.height) || packageList.getBoundingClientRect().height);
      savePackageListHeight(packageListDesired);
      fitPackageList();
    };
    packageResize.addEventListener("pointermove", move);
    packageResize.addEventListener("pointerup", end);
    packageResize.addEventListener("pointercancel", end);
  });
  packageResize.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
      return;
    }
    event.preventDefault();
    const startHeight = packageList.getBoundingClientRect().height;
    const delta = event.key === "ArrowDown" ? 24 : -24;
    applyPackageListHeight(clampPackageListHeight(startHeight + delta, startHeight));
    packageListDesired = Math.round(parseFloat(packageList.style.height) || packageList.getBoundingClientRect().height);
    savePackageListHeight(packageListDesired);
    fitPackageList();
  });
}

function readHiddenPacks() {
  const node = document.querySelector("#hidden-packs");
  if (!node) {
    return [];
  }
  try {
    const data = JSON.parse(node.textContent || "[]");
    return Array.isArray(data) ? data.filter((name) => typeof name === "string" && name) : [];
  } catch {
    return [];
  }
}

async function saveFilters() {
  try {
    const response = await fetch("/api/filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: currentFilters(), hidden_packs: [...hiddenPacks] }),
    });
    if (!response.ok) {
      throw new Error("Could not save the filters.");
    }
  } catch (error) {
    statusNode.textContent = error.message || "Could not save the filters.";
  }
}

async function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  themeButton.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
  themeButton.setAttribute("aria-label", next === "dark" ? "Light mode" : "Dark mode");
  render(false);
  try {
    const response = await fetch("/api/theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: next }),
    });
    if (!response.ok) {
      throw new Error("Could not save the color mode.");
    }
  } catch (error) {
    statusNode.textContent = error.message || "Could not save the color mode.";
  }
}

async function load() {
  try {
    const response = await fetch("/api/airports");
    if (!response.ok) {
      throw new Error("The scan results could not be loaded.");
    }
    applyData(await response.json(), true);
    await showPackChanges();
  } catch (error) {
    statusNode.textContent = error.message || "Could not load airports.";
  }
}

async function rescan() {
  setBusy(true);
  statusNode.textContent = "Reading scenery…";
  try {
    const response = await fetch("/api/rescan", { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "The scenery could not be rescanned.");
    }
    applyData(data, false);
    await showPackChanges();
  } catch (error) {
    statusNode.textContent = error.message || "Could not rescan scenery.";
  } finally {
    setBusy(false);
  }
}

async function updateRoot() {
  setBusy(true);
  statusNode.textContent = "Choose your X-Plane 12 folder…";
  try {
    const response = await fetch("/api/xplane", { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (data.cancelled) {
      render(false);
      return;
    }
    if (!response.ok) {
      throw new Error(data.error || "The X-Plane folder could not be changed.");
    }
    applyData(data, true);
    await showPackChanges();
  } catch (error) {
    statusNode.textContent = error.message || "Could not change the X-Plane folder.";
  } finally {
    setBusy(false);
  }
}

function packageChangeSummary(added, removed) {
  const parts = [];
  if (added.length) {
    parts.push(packageChangeClause(added.length, "added"));
  }
  if (removed.length) {
    parts.push(packageChangeClause(removed.length, "removed"));
  }
  return `${parts.join(" and ")}.`;
}

function packageChangeClause(count, action) {
  const noun = count === 1 ? "package" : "packages";
  const verb = count === 1 ? "was" : "were";
  return `${count} ${noun} ${verb} ${action}`;
}

function packChangeList(title, names) {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const list = document.createElement("ul");
  for (const name of names) {
    const item = document.createElement("li");
    item.textContent = name;
    list.appendChild(item);
  }
  section.append(heading, list);
  return section;
}

async function showPackChanges() {
  try {
    const response = await fetch("/api/pack-changes");
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    const added = Array.isArray(data.added) ? data.added.filter((name) => typeof name === "string" && name) : [];
    const removed = Array.isArray(data.removed) ? data.removed.filter((name) => typeof name === "string" && name) : [];
    if (!added.length && !removed.length) {
      if (packChangesDialog.open) {
        packChangesDialog.close();
      }
      return;
    }
    packChangesSummary.textContent = packageChangeSummary(added, removed);
    packChangesBody.replaceChildren();
    if (added.length) {
      packChangesBody.append(packChangeList("Added", added));
    }
    if (removed.length) {
      packChangesBody.append(packChangeList("Removed", removed));
    }
    packChangesError.hidden = true;
    if (!packChangesDialog.open) {
      packChangesDialog.showModal();
    }
    packChangesOk.focus();
  } catch {
    // The map stays usable when the change check cannot run.
  }
}

async function acknowledgePackChanges() {
  packChangesOk.disabled = true;
  packChangesError.hidden = true;
  try {
    const response = await fetch("/api/pack-changes", { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "Could not save that acknowledgement.");
    }
    packChangesDialog.close();
  } catch (error) {
    packChangesError.hidden = false;
    packChangesError.textContent = error.message || "Could not save that acknowledgement.";
  } finally {
    packChangesOk.disabled = false;
  }
}

function setBusy(busy) {
  rescanButton.disabled = busy;
  updateButton.disabled = busy;
}

function applyData(data, fit) {
  airports = (data.airports || []).map((airport, index) => ({ ...airport, id: index }));
  skipped = data.skipped || [];
  orthoTiles = (data.ortho || []).map((tile, index) => ({ ...tile, id: `ortho-${index}` }));
  xpmeAreas = (data.xpme || []).map((area) => ({ ...area, id: `xpme-${area.region}` }));
  xpmeOverlaps = (data.xpme_overlaps || []).map((overlap, index) => ({ ...overlap, id: `xpme-overlap-${index}` }));
  xpmeInput.closest("label").hidden = !xpmeInstalled();
  rootNode.textContent = data.xplane_root || "";
  selectedId = null;
  drawSkipped(skipped);
  render(fit);
}

function render(fit) {
  const query = searchInput.value.trim().toLowerCase();
  drawPackageList();
  const pool = airports.filter((airport) => matchesKind(airport) && packShown(airport));
  const visible = pool.filter((airport) => matchesAirport(airport, query));
  const visibleOrtho = orthoInput.checked ? orthoTiles.filter((tile) => matchesOrtho(tile, query)) : [];
  const visibleXpme = xpmeEnabled() ? xpmeAreas.filter((area) => matchesXpme(area, query)) : [];
  const visibleRegions = new Set(visibleXpme.map((area) => area.region));
  const visibleOverlaps = xpmeOverlaps.filter((overlap) =>
    overlap.regions.some((region) => visibleRegions.has(region))
  );
  const rows = [
    ...visible
      .map((airport) => ({ type: "airport", item: airport }))
      .sort((a, b) => a.item.code.localeCompare(b.item.code) || a.item.name.localeCompare(b.item.name)),
    ...visibleOrtho
      .map((tile) => ({ type: "ortho", item: tile }))
      .sort((a, b) => a.item.grid.localeCompare(b.item.grid) || a.item.pack.localeCompare(b.item.pack)),
    ...visibleXpme.map((area) => ({ type: "xpme", item: area })),
  ];
  drawLegend(visibleXpme, visibleOverlaps.length > 0);
  drawList(rows, query);
  drawWorld(visibleOrtho, visibleXpme, visibleOverlaps, visible);
  const airportText = selectedKinds().length ? describeSelection(pool, visible, query) : "0 airports";
  const hiddenText = hiddenPackageNote();
  const parts = [
    `${airportText}${hiddenText}`,
    orthoSummary(visibleOrtho),
    xpmeSummary(visibleXpme),
    `${skipped.length} packs skipped (${countReasons(skipped)})`,
  ].filter(Boolean);
  statusNode.textContent = parts.join(" · ");
  requestAnimationFrame(fitPackageList);
  if (fit) {
    if (visible.length) {
      fitTo(visible);
    } else if (visibleOrtho.length) {
      fitTo(visibleOrtho.map((tile) => ({ lat: tile.south + 0.5, lon: tile.west + 0.5 })));
    } else if (visibleXpme.length) {
      fitTo(visibleXpme.flatMap((area) => areaCenters(area)));
    }
  }
}

function drawList(rows, query) {
  listNode.replaceChildren();
  if (!rows.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = emptyMessage(query);
    listNode.appendChild(item);
    return;
  }
  for (const row of rows) {
    const entry = row.item;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = String(entry.id);
    const faded = row.type === "airport" ? !entry.enabled : !(entry.enabled && entry.in_load_order);
    button.className = faded ? "disabled" : "";
    if (entry.id === selectedId) {
      button.classList.add("selected");
    }
    const code = row.type === "ortho" ? entry.grid : row.type === "xpme" ? "XPME" : entry.code;
    const name = row.type === "ortho" ? entry.pack : row.type === "xpme" ? entry.label : entry.name;
    const meta = row.type === "ortho" ? orthoMeta(entry) : row.type === "xpme" ? xpmeMeta(entry) : metaLine(entry);
    const dot = faded ? "disabled" : row.type === "ortho" ? "ortho" : row.type === "xpme" ? `xpme-${entry.region}` : dotClass(entry);
    button.innerHTML = `
      <span class="dot ${dot}"></span>
      <span class="code">${esc(code)}</span>
      <span>
        <span class="name">${esc(name)}</span>
        <span class="meta">${esc(meta)}</span>
      </span>`;
    button.addEventListener("click", () => {
      if (row.type === "ortho") {
        selectOrtho(entry, true);
      } else if (row.type === "xpme") {
        selectXpme(entry, true);
      } else {
        selectAirport(entry, true);
      }
    });
    item.appendChild(button);
    listNode.appendChild(item);
  }
}

function drawWorld(ortho, areas, overlaps, airportsShown) {
  drawnOrtho = ortho;
  drawnXpme = areas;
  drawnOverlaps = overlaps;
  drawnAirports = airportsShown;
  const shifts = worldShifts();
  shiftKey = shifts.join(",");
  drawOrtho(ortho, shifts);
  drawXpme(areas, overlaps, shifts);
  drawMarkers(airportsShown, shifts);
}

function worldShifts() {
  if (!map.getSize().x) {
    return [0];
  }
  const bounds = map.getBounds();
  const minCopy = Math.floor((bounds.getWest() - 360) / 360);
  const maxCopy = Math.floor((bounds.getEast() + 360) / 360);
  const shifts = [];
  for (let copy = minCopy; copy <= maxCopy; copy += 1) {
    shifts.push(copy * 360);
  }
  return shifts.length ? shifts : [0];
}

function homeShift(shifts) {
  return shifts.includes(0) ? 0 : shifts[0];
}

function drawOrtho(tiles, shifts) {
  orthoLayer.clearLayers();
  rectById.clear();
  const active = themeColor("--ortho");
  const idle = themeColor("--disabled");
  const keep = homeShift(shifts);
  for (const tile of tiles) {
    const loaded = tile.enabled && tile.in_load_order;
    const color = loaded ? active : idle;
    for (const shift of shifts) {
      const rect = L.rectangle(
        [[tile.south, tile.west + shift], [tile.south + 1, tile.west + 1 + shift]],
        {
          pane: "ortho",
          color,
          weight: 1,
          fillColor: color,
          fillOpacity: loaded ? 0.34 : 0.16,
        }
      );
      rect.bindTooltip(esc(tile.grid), { sticky: true, opacity: 1, className: "code-tip" });
      rect.bindPopup(orthoPopupHtml(tile), { maxWidth: 360 });
      rect.on("click", (event) => selectOrtho(tile, false, event.latlng));
      rect.addTo(orthoLayer);
      if (shift === keep) {
        rectById.set(tile.id, rect);
      }
    }
  }
}

function drawXpme(areas, overlaps, shifts) {
  xpmeLayer.clearLayers();
  xpmeById.clear();
  stripePatterns.clear();
  const idle = themeColor("--disabled");
  const keep = homeShift(shifts);
  for (const area of areas) {
    const loaded = area.enabled && area.in_load_order;
    const color = loaded ? xpmeColor(area.region) : idle;
    let largest = null;
    let largestSize = 0;
    for (const rect of area.rects || []) {
      for (const shift of shifts) {
        const shape = xpmeRectangle(rect, color, 0.02, shift);
        shape.bindTooltip(esc(area.label), { sticky: true, opacity: 1, className: "code-tip" });
        shape.bindPopup(xpmePopupHtml(area), { maxWidth: 360 });
        shape.on("click", () => selectXpme(area, false));
        shape.addTo(xpmeLayer);
        const size = rect.width * rect.height;
        if (shift === keep && size > largestSize) {
          largest = shape;
          largestSize = size;
        }
      }
    }
    xpmeById.set(area.id, largest);
  }
  for (const overlap of overlaps || []) {
    const pattern = stripePattern(overlap.regions.map((region) => xpmeColor(region)));
    const title = overlap.labels.join(" + ");
    for (const rect of overlap.rects || []) {
      for (const shift of shifts) {
        const shape = xpmeRectangle(rect, pattern, 0, shift);
        shape.bindTooltip(esc(title), { sticky: true, opacity: 1, className: "code-tip" });
        shape.bindPopup(overlapPopupHtml(overlap), { maxWidth: 360 });
        shape.addTo(xpmeLayer);
      }
    }
  }
}

function xpmeColor(region) {
  return themeColor(`--xpme-${region}`) || themeColor("--xpme");
}

function xpmeRectangle(rect, fill, pad, shift) {
  return L.rectangle(
    [[rect.south, rect.west + shift], [rect.south + rect.height + pad, rect.west + rect.width + pad + shift]],
    {
      pane: "xpme",
      renderer: xpmeRenderer,
      color: "#000000",
      weight: 0,
      fillColor: fill,
      fillOpacity: 1,
    }
  );
}

function stripePattern(colors) {
  const key = colors.join("|");
  const cached = stripePatterns.get(key);
  if (cached) {
    return cached;
  }
  const stripe = 8;
  const count = Math.max(colors.length, 1);
  const canvas = document.createElement("canvas");
  canvas.width = stripe * count;
  canvas.height = stripe * count;
  const ctx = canvas.getContext("2d");
  for (let band = -count * 2; band < count * 4; band += 1) {
    ctx.fillStyle = colors[((band % count) + count) % count];
    ctx.beginPath();
    const x = band * stripe;
    ctx.moveTo(x, 0);
    ctx.lineTo(x + stripe, 0);
    ctx.lineTo(x + stripe - canvas.height, canvas.height);
    ctx.lineTo(x - canvas.height, canvas.height);
    ctx.closePath();
    ctx.fill();
  }
  const pattern = ctx.createPattern(canvas, "repeat");
  stripePatterns.set(key, pattern);
  return pattern;
}

function overlapPopupHtml(overlap) {
  const places = overlap.labels.map((label, index) => popupField(label, overlap.paths[index], true)).join("");
  return `
    <div class="popup-code">Overlap</div>
    <div class="popup-scroll">
      ${popupField("Type", "XPME overlap")}
      ${popupField("Areas", overlap.labels.join(", "))}
      ${popupField("Tiles", Number(overlap.tiles).toLocaleString())}
      ${places}
    </div>`;
}

function xpmeInstalled() {
  return xpmeAreas.length > 0;
}

function xpmeEnabled() {
  return xpmeInstalled() && xpmeInput.checked;
}

function xpmeSummary(visibleXpme) {
  if (!xpmeInstalled()) {
    return "";
  }
  const total = xpmeAreas.length;
  const noun = total === 1 ? "XPME area" : "XPME areas";
  if (!xpmeInput.checked) {
    return "0 XPME areas shown";
  }
  if (visibleXpme.length === total) {
    return `${total} ${noun}`;
  }
  return `${visibleXpme.length} of ${total} ${noun}`;
}

function matchesXpme(area, query) {
  if (!query) {
    return true;
  }
  return [area.label, area.pack, area.path, "xpme"].join(" ").toLowerCase().includes(query);
}

function xpmeMeta(area) {
  const count = Number(area.tiles) || 0;
  const tiles = `${count.toLocaleString()} ${count === 1 ? "tile" : "tiles"}`;
  if (!area.in_load_order) {
    return `not in load order · ${tiles}`;
  }
  if (!area.enabled) {
    return `disabled · ${tiles}`;
  }
  return tiles;
}

function xpmePopupHtml(area) {
  const count = Number(area.tiles) || 0;
  return `
    <div class="popup-code">${esc(area.label)}</div>
    <div class="popup-scroll">
      ${popupField("Type", "XPME")}
      ${popupField("Status", orthoStatus(area))}
      ${popupField("Area", area.label)}
      ${popupField("Tiles", count.toLocaleString())}
      ${popupField("File location", area.path, true)}
    </div>`;
}

function areaCenters(area) {
  return (area.rects || []).map((rect) => ({
    lat: rect.south + rect.height / 2,
    lon: rect.west + rect.width / 2,
  }));
}

function selectXpme(area, openPopup) {
  selectedId = area.id;
  for (const button of listNode.querySelectorAll("button")) {
    button.classList.remove("selected");
  }
  const match = listNode.querySelector(`button[data-id="${CSS.escape(area.id)}"]`);
  if (match) {
    match.classList.add("selected");
    match.scrollIntoView({ block: "nearest" });
  }
  const layer = xpmeById.get(area.id);
  if (openPopup) {
    const centers = areaCenters(area);
    if (centers.length) {
      fitTo(centers);
    }
    if (layer) {
      layer.openPopup();
    }
  }
}

function drawLegend(areas, showOverlap) {
  if (!legendNode) {
    return;
  }
  const kinds = new Set(selectedKinds());
  const rows = [];
  if (kinds.has("land")) {
    rows.push(swatch("land", "Airport"));
  }
  if (kinds.has("heli")) {
    rows.push(swatch("heli", "Heliport"));
  }
  if (kinds.has("sea")) {
    rows.push(swatch("sea", "Seaport"));
  }
  if (orthoInput.checked) {
    rows.push(swatch("ortho", "Ortho"));
  }
  if (xpmeInput.checked) {
    for (const area of areas) {
      rows.push(swatch(`xpme-${area.region}`, area.label));
    }
    if (showOverlap) {
      rows.push(swatch("overlap", "Overlap"));
    }
  }
  legendNode.innerHTML = rows.join("<br>");
  legendNode.hidden = rows.length === 0;
}

function orthoSummary(visibleOrtho) {
  const total = orthoTiles.length;
  const noun = total === 1 ? "ortho tile" : "ortho tiles";
  if (!orthoInput.checked) {
    return "0 ortho tiles shown";
  }
  if (visibleOrtho.length === total) {
    return `${total} ${noun}`;
  }
  return `${visibleOrtho.length} of ${total} ${noun}`;
}

function matchesOrtho(tile, query) {
  if (!query) {
    return true;
  }
  return [tile.grid, tile.pack, tile.path].join(" ").toLowerCase().includes(query);
}

function orthoStatus(tile) {
  if (!tile.in_load_order) {
    return "Not in scenery_packs.ini";
  }
  return tile.enabled ? "Enabled" : "Disabled";
}

function orthoMeta(tile) {
  if (!tile.in_load_order) {
    return "not in load order";
  }
  if (!tile.enabled) {
    return "disabled";
  }
  return "";
}

function orthoPopupHtml(tile) {
  return `
    <div class="popup-code">${esc(tile.grid)}</div>
    <div class="popup-scroll">
      ${popupField("Type", "Ortho tile")}
      ${popupField("Status", orthoStatus(tile))}
      ${popupField("Grid", tile.grid)}
      ${popupField("File location", tile.path, true)}
    </div>`;
}

function popupField(label, value, path) {
  const cls = path ? "popup-path" : "popup-meta";
  return `<p class="popup-label">${esc(label)}</p><p class="${cls}">${esc(value)}</p>`;
}

function selectOrtho(tile, openPopup, at) {
  selectedId = tile.id;
  for (const button of listNode.querySelectorAll("button")) {
    button.classList.remove("selected");
  }
  const match = listNode.querySelector(`button[data-id="${tile.id}"]`);
  if (match) {
    match.classList.add("selected");
    match.scrollIntoView({ block: "nearest" });
  }
  map.setView(
    at ? [at.lat, at.lng] : [tile.south + 0.5, tile.west + 0.5],
    Math.max(map.getZoom(), 6)
  );
  const rect = rectById.get(tile.id);
  if (openPopup && rect) {
    rect.openPopup();
  }
}

function drawMarkers(visible, shifts) {
  markers.clearLayers();
  markerById.clear();
  const keep = homeShift(shifts);
  for (const airport of visible) {
    for (const shift of shifts) {
      const marker = L.circleMarker([airport.lat, airport.lon + shift], {
        radius: airport.enabled ? 6 : 5,
        color: "#ffffff",
        weight: 1,
        fillColor: markerFill(airport),
        fillOpacity: airport.enabled ? 0.92 : 0.55,
      });
      marker.bindTooltip(esc(airport.code), {
        direction: "top",
        offset: [0, -6],
        opacity: 1,
        className: "code-tip",
      });
      marker.bindPopup(popupHtml(airport), { maxWidth: 360 });
      marker.on("click", (event) => selectAirport(airport, false, event.latlng));
      marker.addTo(markers);
      if (shift === keep) {
        markerById.set(airport.id, marker);
      }
    }
  }
}

function drawSkipped(packs) {
  skippedNode.replaceChildren();
  if (!packs.length) {
    skippedNode.hidden = true;
    return;
  }
  skippedNode.hidden = false;
  const heading = document.createElement("h2");
  heading.textContent = "Skipped packs";
  skippedNode.appendChild(heading);
  const groups = new Map();
  for (const pack of packs) {
    const reason = pack.reason || "skipped";
    if (!groups.has(reason)) {
      groups.set(reason, []);
    }
    groups.get(reason).push(pack);
  }
  const reasons = [...groups.keys()].sort((a, b) => reasonRank(a) - reasonRank(b) || a.localeCompare(b));
  for (const reason of reasons) {
    const items = groups.get(reason);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${items.length} ${reason}`;
    details.appendChild(summary);
    const list = document.createElement("ul");
    for (const pack of items) {
      const item = document.createElement("li");
      const flags = [];
      if (!pack.enabled) flags.push("disabled");
      if (!pack.in_load_order) flags.push("not in scenery_packs.ini");
      item.innerHTML = `<div class="pack">${esc(pack.pack)}</div><div class="path">${esc(pack.path)}${flags.length ? " · " + esc(flags.join(", ")) : ""}</div>`;
      list.appendChild(item);
    }
    details.appendChild(list);
    skippedNode.appendChild(details);
  }
}

function selectAirport(airport, openPopup, at) {
  selectedId = airport.id;
  for (const button of listNode.querySelectorAll("button")) {
    button.classList.remove("selected");
  }
  const match = listNode.querySelector(`button[data-id="${airport.id}"]`);
  if (match) {
    match.classList.add("selected");
    match.scrollIntoView({ block: "nearest" });
  }
  map.setView(at ? [at.lat, at.lng] : [airport.lat, airport.lon], Math.max(map.getZoom(), 11));
  const marker = markerById.get(airport.id);
  if (openPopup && marker) {
    marker.openPopup();
  }
}

function fitTo(items) {
  if (!items.length) {
    return;
  }
  const bounds = L.latLngBounds(items.map((airport) => [airport.lat, airport.lon]));
  map.fitBounds(bounds, { padding: [32, 32], maxZoom: 6 });
}

function airportPacks() {
  const counts = new Map();
  for (const airport of airports) {
    counts.set(airport.pack, (counts.get(airport.pack) || 0) + 1);
  }
  return [...counts.entries()].map(([name, count]) => ({ name, count }));
}

function comparePacks(a, b) {
  const dir = packageSort.dir === "asc" ? 1 : -1;
  const byName = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  if (packageSort.key === "name") {
    return byName * dir || b.count - a.count;
  }
  return (a.count - b.count) * dir || byName;
}

function setPackageSort(key) {
  if (packageSort.key === key) {
    packageSort.dir = packageSort.dir === "asc" ? "desc" : "asc";
  } else {
    packageSort = { key, dir: key === "name" ? "asc" : "desc" };
  }
  drawPackageList(false);
}

function packageSortButton(key, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = key === "count" ? "count" : "name";
  const active = packageSort.key === key;
  if (active) {
    button.classList.add("active");
  }
  button.textContent = label;
  if (active) {
    const mark = document.createElement("span");
    mark.className = "sort-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = packageSort.dir === "asc" ? "↑" : "↓";
    button.append(mark);
  }
  button.setAttribute("aria-label", packageSortLabel(key, active));
  button.addEventListener("click", () => setPackageSort(key));
  return button;
}

function packageSortLabel(key, active) {
  if (!active) {
    return key === "name" ? "Sort by folder name" : "Sort by number of airports";
  }
  if (key === "name") {
    return packageSort.dir === "asc" ? "Folder, sorted A to Z" : "Folder, sorted Z to A";
  }
  return packageSort.dir === "asc" ? "Airports, sorted fewest first" : "Airports, sorted most first";
}

function packShown(airport) {
  return !hiddenPacks.has(airport.pack);
}

function hiddenPackageCount() {
  const names = new Set(airports.map((airport) => airport.pack));
  let count = 0;
  for (const name of hiddenPacks) {
    if (names.has(name)) {
      count += 1;
    }
  }
  return count;
}

function hiddenPackageNote() {
  const count = hiddenPackageCount();
  if (!count) {
    return "";
  }
  return ` · ${count} ${count === 1 ? "package" : "packages"} hidden`;
}

function drawPackageList(keepScroll = true) {
  const query = packageSearch.value.trim().toLowerCase();
  const packs = airportPacks()
    .filter((pack) => !query || pack.name.toLowerCase().includes(query))
    .sort(comparePacks);
  const hidden = hiddenPackageCount();
  packagesHidden.textContent = hidden ? `${hidden} hidden` : "";
  const scroll = keepScroll ? packageList.scrollTop : 0;
  packageList.replaceChildren();
  const head = document.createElement("li");
  head.className = "package-sort";
  head.append(document.createElement("span"), packageSortButton("name", "Folder"), packageSortButton("count", "Airports"));
  packageList.appendChild(head);
  if (!packs.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = query ? "No packages match that search." : "No airport packages.";
    packageList.appendChild(item);
    return;
  }
  for (const pack of packs) {
    const item = document.createElement("li");
    const shown = !hiddenPacks.has(pack.name);
    if (!shown) {
      item.classList.add("off");
    }
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = shown;
    input.setAttribute("aria-label", `Show ${pack.name}`);
    input.addEventListener("change", () => {
      if (input.checked) {
        hiddenPacks.delete(pack.name);
      } else {
        hiddenPacks.add(pack.name);
      }
      render(false);
      saveFilters();
    });
    const name = document.createElement("span");
    name.className = "pack-name";
    name.textContent = pack.name;
    const count = document.createElement("span");
    count.className = "pack-count";
    count.textContent = String(pack.count);
    label.append(input, name, count);
    item.appendChild(label);
    packageList.appendChild(item);
  }
  packageList.scrollTop = scroll;
}

function selectedKinds() {
  return [...kindInputs].filter((input) => input.checked).map((input) => input.value);
}

function matchesKind(airport) {
  return selectedKinds().includes(airport.kind);
}

function kindNoun(kind, count) {
  if (kind === "heli") {
    return count === 1 ? "heliport" : "heliports";
  }
  if (kind === "sea") {
    return count === 1 ? "seaport" : "seaports";
  }
  if (kind === "land") {
    return count === 1 ? "land airport" : "land airports";
  }
  return count === 1 ? "airport" : "airports";
}

function describeSelection(pool, visible, query) {
  const kinds = selectedKinds();
  if (!kinds.length) {
    return "0 shown";
  }
  if (kinds.length === kindInputs.length) {
    const total = query ? pool.length : visible.length;
    const noun = total === 1 ? "airport" : "airports";
    const shown = query ? `${visible.length} of ${pool.length}` : String(visible.length);
    return `${shown} ${noun}`;
  }
  const parts = kinds.map((kind) => {
    const count = pool.filter((airport) => airport.kind === kind).length;
    return `${count} ${kindNoun(kind, count)}`;
  });
  if (!query) {
    return parts.join(" and ");
  }
  return `${visible.length} of ${pool.length} (${parts.join(", ")})`;
}

function emptyMessage(query) {
  if (!selectedKinds().length && !orthoInput.checked && !xpmeEnabled()) {
    return "Select a type to show scenery.";
  }
  if (query) {
    return "Nothing matches that search.";
  }
  const kindPool = airports.filter((airport) => matchesKind(airport));
  if (kindPool.length && kindPool.every((airport) => !packShown(airport))) {
    return "Every package is hidden.";
  }
  return "None of the selected types are installed.";
}

function matchesAirport(airport, query) {
  if (!query) {
    return true;
  }
  return [airport.code, airport.name, airport.city, airport.state, airport.country, airport.pack, airport.path]
    .join(" ")
    .toLowerCase()
    .includes(query);
}

function countReasons(packs) {
  const counts = new Map();
  for (const pack of packs) {
    counts.set(pack.reason, (counts.get(pack.reason) || 0) + 1);
  }
  const parts = [...counts.entries()]
    .sort((a, b) => reasonRank(a[0]) - reasonRank(b[0]) || a[0].localeCompare(b[0]))
    .map(([reason, count]) => `${count} ${reason}`);
  return parts.join("; ") || "none";
}

function reasonRank(reason) {
  const index = REASON_ORDER.indexOf(reason);
  return index === -1 ? REASON_ORDER.length : index;
}

function metaLine(airport) {
  const place = [airport.city, airport.state, airport.country].filter(Boolean).join(", ");
  const bits = [place, airport.pack];
  if (!airport.enabled) bits.push("disabled");
  if (!airport.in_load_order) bits.push("not in load order");
  return bits.filter(Boolean).join(" · ");
}

function popupHtml(airport) {
  return `
    <div class="popup-code">${esc(airport.code)}</div>
    <div class="popup-scroll">
      ${elevationHtml(airport)}
      ${runwaysHtml(airport)}
      ${popupFacts(airport)}
      <p class="popup-path">${esc(airport.path)}</p>
    </div>`;
}

function popupFacts(airport) {
  const place = [airport.city, airport.state, airport.country].filter(Boolean).join(", ");
  const lines = [];
  if (place) lines.push(place);
  lines.push(`Type: ${KIND_LABEL[airport.kind] || "Airport"}`);
  lines.push(`Status: ${airport.enabled ? "Enabled" : "Disabled"}`);
  if (!airport.in_load_order) lines.push("Not in scenery_packs.ini");
  return lines.map((line) => `<p class="popup-meta">${esc(line)}</p>`).join("");
}

function elevationHtml(airport) {
  const text = formatFeet(airport.elevation_ft);
  if (!text) {
    return "";
  }
  return `<p class="popup-label">Elevation</p><p class="popup-elev">${esc(text)} ft</p>`;
}

function runwaysHtml(airport) {
  const rows = Array.isArray(airport.runways) ? airport.runways : [];
  const pads = rows.filter((row) => row.kind === "heli");
  const runways = rows.filter((row) => row.kind !== "heli");
  return `${runwayList(runways, "Runways")}${runwayList(pads, "Helipads")}`;
}

function runwayList(rows, label) {
  if (!rows.length) {
    return "";
  }
  const items = rows.map((row) => {
    const size = `${formatFeet(row.length_ft)} × ${formatFeet(row.width_ft)} ft`;
    return `<li><span class="rwy-id">${esc(row.id)}</span><span class="rwy-surface">${esc(row.surface)}</span><span class="rwy-size">${esc(size)}</span></li>`;
  }).join("");
  return `<p class="popup-label">${label}</p><ul class="runways">${items}</ul>`;
}

function formatFeet(value) {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "";
  }
  return Math.round(number).toLocaleString("en-US");
}

function dotClass(airport) {
  if (!airport.enabled) return "disabled";
  if (airport.kind === "sea" || airport.kind === "heli") return airport.kind;
  return "";
}

function markerFill(airport) {
  if (!airport.enabled) {
    return themeColor("--disabled");
  }
  if (airport.kind === "sea" || airport.kind === "heli") {
    return themeColor("--" + airport.kind);
  }
  return themeColor("--land");
}

function themeColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function swatch(kind, label) {
  return `<span class="${kind}"></span>${esc(label)}`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}
