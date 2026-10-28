/* PermitIQ frontend: read-only permit map + list + detail drawer, backed by
   the Phase 7 /api/* endpoints. Structural patterns (esc(), api(), the
   marker diff/reconcile loop, debounced search) adapted from AssetIQ's
   static/app.js -- CRUD/modal-form/add-mode code dropped since permits are
   synced by run_ingest.py, never user-edited here. */
"use strict";

// blue = in the future (not yet started), green = ongoing (issued/active),
// gray = done/closed, red = expired/void.
const STATUS_BUCKET_COLORS = {
  active_pending: "#4f8ef7",
  issued: "#34c07c",
  finaled_closed: "#7d8798",
  expired_void: "#e05c5c",
  other: "#a56de2",
};
const STATUS_BUCKET_LABELS = {
  active_pending: "Active / pending",
  issued: "Issued",
  finaled_closed: "Finaled / closed",
  expired_void: "Expired / void",
  other: "Other",
};
const STATUS_BUCKET_ORDER = ["active_pending", "issued", "finaled_closed", "expired_void", "other"];
const PAGE_SIZE = 1000;

// Work-category classification, built from the real distinct permit_type
// values observed across both localities (2026-07-25) -- not a guess.
// Norfolk: Building, Electrical, Zoning, Mechanical, Plumbing, Fire
// Protection, Short Term Rental, Sign, Demolition, Occupancy Certificate
// (+2 variants), Operating, New Elevator, Portable Storage Unit, Amusement
// Device. Virginia Beach: Building, Mechanical, Electrical, Moving and
// Hauling, Plumbing, Utility, Gas, Elevator, Complaint, Fire, Civil,
// Amusement. Neither source has a dedicated "Roof" or "Addition" field --
// those are sub-categories WITHIN "Building", detectable only by matching
// real description text (474 real permits say "Reroof/Solar Panels"; 383+
// say "addition"/"expansion"). Flagged as inferred, not treated as if the
// source itself categorized them.
const WORK_CATEGORY_ICONS = {
  electrical: "⚡",
  plumbing: "💧",
  mechanical: "🌀",
  gas: "🔥",
  fire: "🧯",
  roof: "🔺",       // inferred from description text, see workCategory()
  addition: "🏗️",   // inferred from description text, see workCategory()
  building: "🏠",
  zoning: "📐",
  demolition: "🔨",
  elevator: "🛗",
  sign: "🪧",
  occupancy: "📋",
  rental: "🔑",
  amusement: "🎡",
  utility: "🔌",
  hauling: "🚚",
  civil: "🛣️",
  complaint: "⚠️",
  operating: "⚙️",
  storage: "📦",
  other: "📍",
};
const WORK_CATEGORY_LABELS = {
  electrical: "Electrical",
  plumbing: "Plumbing",
  mechanical: "Mechanical / HVAC",
  gas: "Gas",
  fire: "Fire protection",
  roof: "Roofing (inferred from description)",
  addition: "Addition / expansion (inferred from description)",
  building: "Building (general)",
  zoning: "Zoning",
  demolition: "Demolition",
  elevator: "Elevator",
  sign: "Sign",
  occupancy: "Occupancy certificate",
  rental: "Short-term rental",
  amusement: "Amusement device",
  utility: "Utility",
  hauling: "Moving / hauling",
  civil: "Civil",
  complaint: "Complaint",
  operating: "Operating",
  storage: "Portable storage",
  other: "Other / unclassified",
};

function workCategory(p) {
  const type = (p.permit_type || "").trim().toLowerCase();
  const desc = (p.description || "").toLowerCase();

  if (type === "electrical") return "electrical";
  if (type === "plumbing") return "plumbing";
  if (type === "mechanical") return "mechanical";
  if (type === "gas") return "gas";
  if (type === "fire protection" || type === "fire") return "fire";
  if (type === "building") {
    if (/re-?roof|roofing/.test(desc)) return "roof";
    if (/addition|expansion/.test(desc)) return "addition";
    return "building";
  }
  if (type === "zoning") return "zoning";
  if (type === "demolition") return "demolition";
  if (type === "elevator" || type === "new elevator") return "elevator";
  if (type === "sign") return "sign";
  if (type.startsWith("occupancy certificate") || type === "temporary occupancy certificate") return "occupancy";
  if (type === "short term rental") return "rental";
  if (type === "amusement device" || type === "amusement") return "amusement";
  if (type === "utility") return "utility";
  if (type === "moving and hauling") return "hauling";
  if (type === "civil") return "civil";
  if (type === "complaint") return "complaint";
  if (type === "operating") return "operating";
  if (type === "portable storage unit") return "storage";
  return "other";
}

// ---------- state ----------
let permits = []; // accumulated across "load more"
let stats = {};
let facetsData = { localities: [], permit_types: {}, statuses: {} };
const markers = new Map(); // permit id -> Leaflet marker
let selectedId = null;
let currentOffset = 0;
const filters = {
  locality: null, permit_type: "", status: "", status_bucket: null,
  q: "", issued_from: "", issued_to: "", flood_class: "",
};

// ---------- map ----------
const map = L.map("map", { zoomControl: false }).setView([36.85, -76.15], 10);
L.control.zoom({ position: "bottomright" }).addTo(map);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  maxZoom: 19,
}).addTo(map);

// ---------- dom ----------
const $ = (id) => document.getElementById(id);
const listEl = $("permit-list");
const toastEl = $("toast");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatTimestamp(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso;
  }
}

function localityLabel(key) {
  const found = facetsData.localities.find((l) => l.key === key);
  return found ? found.label : key;
}

// ---------- api ----------
async function api(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d) => d.msg).join("; ")
      : body.detail || `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body;
}

function buildParams(extra = {}) {
  const params = new URLSearchParams();
  if (filters.locality) params.set("locality", filters.locality);
  if (filters.permit_type) params.set("permit_type", filters.permit_type);
  if (filters.status) params.set("status", filters.status);
  if (filters.status_bucket) params.set("status_bucket", filters.status_bucket);
  if (filters.q) params.set("q", filters.q);
  if (filters.issued_from) params.set("issued_from", filters.issued_from);
  if (filters.issued_to) params.set("issued_to", filters.issued_to);
  if (filters.flood_class) params.set("flood_class", filters.flood_class);
  for (const [k, v] of Object.entries(extra)) params.set(k, v);
  return params;
}

// ---------- boot ----------
async function boot() {
  $("permit-list").innerHTML = `<li class="empty">Loading permits…</li>`;
  try {
    const [facetsRes, dataStatusRes, overallStatsRes] = await Promise.all([
      api("/api/facets"),
      api("/api/data-status"),
      api("/api/stats"), // unfiltered -- see note on data-as-of below
    ]);
    facetsData = facetsRes;
    renderLocalityChips();
    renderLegend();
    renderSourceCards(dataStatusRes.localities);
    populateTypeStatusOptions();

    // "Data as of" reflects overall database freshness, not the current
    // filter's subset -- computed once here from an unfiltered /api/stats
    // call so it never flips to "No data ingested yet" just because a
    // narrow filter happens to match zero rows.
    $("data-as-of").textContent = overallStatsRes.latest_ingested_at
      ? `Data as of ${formatTimestamp(overallStatsRes.latest_ingested_at)}`
      : "No data ingested yet";

    await refresh({ reset: true, fit: true });
  } catch (err) {
    toast(`Failed to load PermitIQ: ${err.message}`, true);
  }
}

// ---------- refresh ----------
// refreshSeq guards against out-of-order responses: if the user toggles two
// filters in quick succession, an earlier request that resolves LATER than
// a newer one must not overwrite the newer (correct) result on screen.
let refreshSeq = 0;

async function refresh({ reset = true, fit = false } = {}) {
  const seq = ++refreshSeq;
  if (reset) {
    currentOffset = 0;
    permits = [];
    for (const m of markers.values()) m.remove();
    markers.clear();
    selectedId = null;
  }
  const listParams = buildParams({ limit: PAGE_SIZE, offset: currentOffset });
  const [listRes, statsRes] = await Promise.all([
    api(`/api/permits?${listParams}`),
    api(`/api/stats?${buildParams()}`),
  ]);
  if (seq !== refreshSeq) return; // superseded by a newer refresh -- discard

  stats = statsRes;
  permits = reset ? listRes.permits : permits.concat(listRes.permits);

  renderStats();
  renderList();
  renderMarkers();
  renderPageBanner(listRes.count);

  $("export-btn").href = `/api/export.geojson?${buildParams()}`;
  $("export-csv-btn").href = `/api/export.csv?${buildParams()}`;

  if (fit) {
    const pts = permits.filter((p) => p.lat != null).map((p) => [p.lat, p.lng]);
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15));
  }
}

function refreshOrToast(options) {
  refresh(options).catch((err) => toast(`Failed to update: ${err.message}`, true));
}

// ---------- rendering: stats / legend / chips ----------
function renderStats() {
  $("stats").innerHTML = `
    <div class="stat"><b>${(stats.total ?? 0).toLocaleString()}</b><span>Total</span></div>
    <div class="stat s-mapped"><b>${(stats.mapped ?? 0).toLocaleString()}</b><span>Mapped</span></div>
    <div class="stat s-unmapped"><b>${(stats.unmapped ?? 0).toLocaleString()}</b><span>Unmapped</span></div>
    <div class="stat"><b>${stats.mapped_percent ?? 0}%</b><span>Mapped %</span></div>
  `;
}

function renderLegend() {
  $("legend").innerHTML = STATUS_BUCKET_ORDER.map((b) => `
    <button class="legend-item ${filters.status_bucket === b ? "active" : ""}" data-bucket="${b}">
      <span class="legend-dot" style="background:${STATUS_BUCKET_COLORS[b]}"></span>${esc(STATUS_BUCKET_LABELS[b])}
    </button>`).join("");
}

function renderLocalityChips() {
  $("locality-chips").innerHTML = facetsData.localities.map((loc) => {
    const disabled = loc.availability !== "available";
    const cls = ["locality-chip"];
    if (filters.locality === loc.key) cls.push("active");
    if (loc.availability === "source_under_review") cls.push("under-review");
    const title =
      loc.availability === "source_under_review"
        ? "Real permit system exists but no confirmed open dataset yet"
        : loc.availability === "unavailable"
        ? "No confirmed open permit dataset for this locality yet"
        : "";
    return `<button class="${cls.join(" ")}" data-locality="${esc(loc.key)}" ${disabled ? "disabled" : ""} title="${esc(title)}">${esc(loc.label)}</button>`;
  }).join("");
}

function renderPageBanner(totalCount) {
  const banner = $("page-banner");
  if (totalCount > permits.length) {
    banner.hidden = false;
    $("page-banner-text").textContent =
      `Showing ${permits.length.toLocaleString()} of ${totalCount.toLocaleString()} matching permits.`;
    $("load-more-btn").hidden = false;
  } else {
    banner.hidden = false;
    $("page-banner-text").textContent = `Showing all ${permits.length.toLocaleString()} matching permits.`;
    $("load-more-btn").hidden = true;
    if (permits.length === 0) banner.hidden = true;
  }
}

function populateTypeStatusOptions() {
  const activeLocalities = filters.locality ? [filters.locality] : Object.keys(facetsData.permit_types || {});
  const types = new Set();
  const statuses = new Set();
  for (const loc of activeLocalities) {
    (facetsData.permit_types[loc] || []).forEach((t) => types.add(t));
    (facetsData.statuses[loc] || []).forEach((s) => statuses.add(s));
  }
  const typeSel = $("filter-type");
  const statusSel = $("filter-status");
  const prevType = typeSel.value;
  const prevStatus = statusSel.value;
  const sortedTypes = [...types].sort();
  const sortedStatuses = [...statuses].sort();
  typeSel.innerHTML = `<option value="">All permit types</option>` +
    sortedTypes.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  statusSel.innerHTML = `<option value="">All statuses</option>` +
    sortedStatuses.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  if (sortedTypes.includes(prevType)) typeSel.value = prevType;
  if (sortedStatuses.includes(prevStatus)) statusSel.value = prevStatus;
}

// ---------- rendering: list / markers / popup / drawer ----------
function renderList() {
  if (!permits.length) {
    listEl.innerHTML = `<li class="empty">No permits match the current filters.</li>`;
    return;
  }
  listEl.innerHTML = permits.map((p) => {
    const category = workCategory(p);
    return `
    <li class="permit-item ${p.id === selectedId ? "selected" : ""}" data-id="${p.id}">
      <span class="dot ${esc(p.status_bucket)}" title="${esc(WORK_CATEGORY_LABELS[category])}">${WORK_CATEGORY_ICONS[category]}</span>
      <span class="meta">
        <b>${esc(p.address || "(no address on file)")}</b>
        <span>${esc(p.permit_type || "Unknown type")} · ${esc(p.status || "Unknown status")}</span>
        <span>${esc(localityLabel(p.locality))}${p.issued_date ? " · issued " + esc(p.issued_date) : ""}</span>
      </span>
    </li>`;
  }).join("");
}

function popupHtml(p) {
  const category = workCategory(p);
  return `
    <div class="popup">
      <h3>${WORK_CATEGORY_ICONS[category]} ${esc(p.address || "(no address on file)")}</h3>
      <p class="pop-sub">${esc(WORK_CATEGORY_LABELS[category])} · ${esc(p.permit_type || "Unknown type")} · ${esc(localityLabel(p.locality))}</p>
      <span class="badge ${esc(p.status_bucket)}">${esc(p.status || "Unknown")}</span>
      ${floodBadge(p)}
      ${parcelHtml(p)}
      <div class="pop-actions">
        <button class="btn small" data-detail="${p.id}">View full details</button>
      </div>
    </div>`;
}

// Cache built L.divIcon instances per (category, status_bucket) pair --
// there are only ~21 categories x 5 buckets, so this avoids rebuilding an
// identical icon for every one of hundreds of markers on each render.
const _iconCache = new Map();
function markerIcon(p) {
  const category = workCategory(p);
  const key = category + "|" + p.status_bucket;
  let icon = _iconCache.get(key);
  if (!icon) {
    const color = STATUS_BUCKET_COLORS[p.status_bucket] || STATUS_BUCKET_COLORS.other;
    const emoji = WORK_CATEGORY_ICONS[category] || WORK_CATEGORY_ICONS.other;
    icon = L.divIcon({
      className: "permit-marker",
      html: `<div class="marker-badge" style="background:${color}">${emoji}</div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13],
      popupAnchor: [0, -14],
    });
    _iconCache.set(key, icon);
  }
  return icon;
}

function renderMarkers() {
  const mapped = permits.filter((p) => p.lat != null && p.lng != null);
  const keep = new Set(mapped.map((p) => p.id));
  for (const [id, m] of markers) {
    if (!keep.has(id)) {
      m.remove();
      markers.delete(id);
    }
  }
  for (const p of mapped) {
    let m = markers.get(p.id);
    if (m) {
      m.setLatLng([p.lat, p.lng]).setIcon(markerIcon(p)).setPopupContent(popupHtml(p));
    } else {
      m = L.marker([p.lat, p.lng], { icon: markerIcon(p) }).addTo(map).bindPopup(popupHtml(p));
      m.on("click", () => selectPermit(p.id, { pan: false, showDetail: false }));
      markers.set(p.id, m);
    }
  }
}

function selectPermit(id, { pan = true, showDetail = true } = {}) {
  selectedId = id;
  renderList();
  const item = listEl.querySelector(`[data-id="${id}"]`);
  if (item) item.scrollIntoView({ block: "nearest" });
  const p = permits.find((x) => x.id === id);
  const m = markers.get(id);
  if (p && m && pan) map.panTo([p.lat, p.lng]);
  if (p && showDetail) showDrawer(p);
}

function showDrawer(p) {
  const fields = [
    ["Permit number", p.source_permit_id],
    ["Locality", localityLabel(p.locality)],
    ["Type", p.permit_type],
    ["Applied", p.applied_date],
    ["Issued", p.issued_date],
    ["Finaled", p.finaled_date],
    ["Expires", p.expiration_date],
    ["Valuation", p.valuation != null ? `$${Number(p.valuation).toLocaleString()}` : null],
    ["Contractor", p.contractor],
    ["FEMA flood class", FLOOD_CLASS_LABELS[p.flood_class] || p.flood_class],
    ["FEMA zone", p.fld_zone ? p.fld_zone + (p.zone_subty ? ` (${p.zone_subty})` : "") : null],
    ["Parcel (GPIN)", p.parcel_gpin ? `${p.parcel_gpin} · ${p.parcel_method}${p.parcel_disputed ? " · disputed" : ""}` : null],
    ["Zoning / land use", p.zoning || p.land_use ? `${p.zoning || "?"} / ${p.land_use || "?"}` : null],
    ["Acreage", p.acreage != null ? String(p.acreage) : null],
    ["Parcel flood (screening estimate)", p.parcel_flood_class ? `${PARCEL_FLOOD_LABELS[p.parcel_flood_class] || p.parcel_flood_class} — ${parcelShares(p)}` : null],
    ["Description", p.description],
  ];
  const rows = fields.map(([label, value]) => {
    const display = value ? esc(value) : `<span class="not-reported">not reported</span>`;
    return `<dt>${esc(label)}</dt><dd>${display}</dd>`;
  }).join("");

  $("drawer-body").innerHTML = `
    <h2>${esc(p.address || "(no address on file)")}</h2>
    <p class="drawer-sub">${esc(localityLabel(p.locality))}</p>
    <span class="badge ${esc(p.status_bucket)}">${esc(p.status || "Unknown status")}</span>
    <dl>${rows}</dl>
    <p class="source-line">
      Source: ${esc(p.source_name || "unknown")}
      ${p.source_url ? ` — <a href="${esc(p.source_url)}" target="_blank" rel="noopener">view dataset</a>` : ""}
    </p>`;
  $("drawer-backdrop").hidden = false;
}

function closeDrawer() {
  $("drawer-backdrop").hidden = true;
}

function renderSourceCards(localities) {
  $("source-cards").innerHTML = localities.map((loc) => `
    <div class="source-card">
      <h3>${esc(loc.label)} <span class="avail-badge ${esc(loc.availability)}">${esc(loc.availability.replace(/_/g, " "))}</span></h3>
      ${loc.source_name ? `<p>Source: ${esc(loc.source_name)}</p>` : ""}
      <p>Records: ${loc.record_count.toLocaleString()} · Mapped: ${loc.mapped_count.toLocaleString()} (${loc.mapped_percent}%)</p>
      ${loc.last_successful_ingestion ? `<p>Last synced: ${esc(formatTimestamp(loc.last_successful_ingestion))}</p>` : ""}
      ${loc.earliest_permit_date ? `<p>Coverage: ${esc(loc.earliest_permit_date)} to ${esc(loc.latest_permit_date)}</p>` : ""}
    </div>`).join("");
}

// ---------- toast ----------
let toastTimer;
function toast(msg, isError = true) {
  toastEl.textContent = msg;
  toastEl.classList.toggle("error", isError);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toastEl.hidden = true), 4000);
}

// ---------- event wiring ----------
document.addEventListener("click", (e) => {
  const detailBtn = e.target.closest("[data-detail]");
  if (detailBtn) selectPermit(Number(detailBtn.dataset.detail), { pan: true, showDetail: true });
});

listEl.addEventListener("click", (e) => {
  const item = e.target.closest(".permit-item");
  if (item) selectPermit(Number(item.dataset.id), { pan: true, showDetail: true });
});

$("locality-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".locality-chip");
  if (!chip || chip.disabled) return;
  const key = chip.dataset.locality;
  filters.locality = filters.locality === key ? null : key;
  renderLocalityChips();
  populateTypeStatusOptions();
  refreshOrToast({ reset: true, fit: true });
});

$("legend").addEventListener("click", (e) => {
  const btn = e.target.closest(".legend-item");
  if (!btn) return;
  const bucket = btn.dataset.bucket;
  filters.status_bucket = filters.status_bucket === bucket ? null : bucket;
  renderLegend();
  refreshOrToast({ reset: true });
});

let searchTimer;
$("filter-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    filters.q = $("filter-q").value.trim();
    refreshOrToast({ reset: true });
  }, 250);
});
$("filter-type").addEventListener("change", () => {
  filters.permit_type = $("filter-type").value;
  refreshOrToast({ reset: true });
});
$("filter-status").addEventListener("change", () => {
  filters.status = $("filter-status").value;
  refreshOrToast({ reset: true });
});
$("filter-issued-from").addEventListener("change", () => {
  filters.issued_from = $("filter-issued-from").value;
  refreshOrToast({ reset: true });
});
$("filter-issued-to").addEventListener("change", () => {
  filters.issued_to = $("filter-issued-to").value;
  refreshOrToast({ reset: true });
});
$("filter-clear-btn").addEventListener("click", () => {
  filters.permit_type = "";
  filters.status = "";
  filters.q = "";
  filters.issued_from = "";
  filters.issued_to = "";
  filters.status_bucket = null;
  filters.flood_class = "";
  renderFloodControls();
  $("filter-q").value = "";
  $("filter-type").value = "";
  $("filter-status").value = "";
  $("filter-issued-from").value = "";
  $("filter-issued-to").value = "";
  renderLegend();
  refreshOrToast({ reset: true });
});

$("load-more-btn").addEventListener("click", () => {
  currentOffset += PAGE_SIZE;
  refreshOrToast({ reset: false, fit: false });
});

$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "drawer-backdrop") closeDrawer();
});

$("sources-btn").addEventListener("click", () => {
  $("sources-modal").hidden = false;
});
$("sources-close").addEventListener("click", () => {
  $("sources-modal").hidden = true;
});
$("sources-modal").addEventListener("click", (e) => {
  if (e.target.id === "sources-modal") $("sources-modal").hidden = true;
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeDrawer();
    $("sources-modal").hidden = true;
  }
});

// ---------- FEMA flood layer ----------
// Served as fixed grid cells (see /api/flood-zones/index.json); only the
// cells intersecting the viewport are fetched, cached per etag.
const SFHA_FILTER = "sfha_v,sfha_a";
const FLOOD_CLASS_LABELS = {
  sfha_v: "SFHA – coastal V/VE",
  sfha_a: "SFHA – A/AE",
  shaded_x: "Shaded X (0.2% annual chance)",
  matched_non_sfha: "Mapped, outside SFHA",
  no_polygon: "No FEMA polygon matched",
  not_geocoded: "Not geocoded",
  untagged: "Not yet tagged",
  out_of_scope: "Not covered (outside Virginia Beach)",
};
const FLOOD_COLORS = { sfha_v: "#e0457b", sfha_a: "#3d8bfd", shaded_x: "#f2c94c" };
const floodLayer = L.layerGroup();
const floodCellCache = new Map(); // "x/y" -> { etag, layer }
let floodIndex = null;

function floodBadge(p) {
  if (!p.flood_class) return "";
  return `<span class="flood-badge ${esc(p.flood_class)}">${esc(FLOOD_CLASS_LABELS[p.flood_class] || p.flood_class)}${p.fld_zone ? " · " + esc(p.fld_zone) : ""}</span>`;
}

function renderFloodControls() {
  $("flood-sfha-chip").classList.toggle("active", filters.flood_class === SFHA_FILTER);
  $("flood-legend").innerHTML = $("flood-layer-toggle").checked
    ? Object.entries(FLOOD_COLORS).map(([c, color]) => `
      <span class="flood-legend-item"><span class="flood-swatch" style="background:${color}"></span>${esc(FLOOD_CLASS_LABELS[c])}</span>`).join("")
    : "";
}

async function loadFloodCells() {
  if (!map.hasLayer(floodLayer)) return;
  if (!floodIndex) {
    try {
      floodIndex = await api("/api/flood-zones/index.json");
    } catch (err) {
      toast(`Flood layer unavailable: ${err.message}`, true);
      $("flood-layer-toggle").checked = false;
      map.removeLayer(floodLayer);
      renderFloodControls();
      return;
    }
  }
  const view = map.getBounds();
  for (const cell of floodIndex.cells) {
    const [minx, miny, maxx, maxy] = cell.bbox;
    if (!view.intersects(L.latLngBounds([miny, minx], [maxy, maxx]))) continue;
    const key = `${cell.x}/${cell.y}`;
    const cached = floodCellCache.get(key);
    if (cached && cached.etag === cell.etag) {
      if (!floodLayer.hasLayer(cached.layer)) floodLayer.addLayer(cached.layer);
      continue;
    }
    floodCellCache.set(key, { etag: cell.etag, layer: null }); // claim before the await
    const res = await fetch(`/api/flood-zones/${key}.geojson`);
    if (!res.ok) { floodCellCache.delete(key); continue; }
    const layer = L.geoJSON(await res.json(), {
      interactive: false,
      style: (f) => ({
        color: FLOOD_COLORS[f.properties.flood_class] || "#888",
        weight: 0.5, fillOpacity: 0.28, opacity: 0.6,
      }),
    });
    floodCellCache.set(key, { etag: cell.etag, layer });
    floodLayer.addLayer(layer);
  }
}

$("flood-layer-toggle").addEventListener("change", (e) => {
  if (e.target.checked) {
    floodLayer.addTo(map);
    loadFloodCells();
  } else {
    map.removeLayer(floodLayer);
  }
  renderFloodControls();
});
map.on("moveend", () => loadFloodCells());
$("flood-sfha-chip").addEventListener("click", () => {
  filters.flood_class = filters.flood_class === SFHA_FILTER ? "" : SFHA_FILTER;
  renderFloodControls();
  refreshOrToast({ reset: true });
});
renderFloodControls();

// ---------- Council land-use decisions (Stage 2) ----------
// Status comes from the API (land_use_decision_status view); approval only
// from human review, so most decisions show as "outcome unverified".
const LANDUSE_STATUS = {
  no_candidate_permit: { label: "Approved, no permit yet", color: "#2ecc71" },
  candidate_permit: { label: "Candidate permit (unconfirmed)", color: "#f2c94c" },
  confirmed_permit: { label: "Permit confirmed", color: "#3d8bfd" },
  no_candidate_window_expired: { label: "No permit, window expired", color: "#93a0b8" },
  observation_uncertain: { label: "Permit window uncertain", color: "#b07cff" },
  outcome_review_stale: { label: "Outcome changed — re-review", color: "#e67e22" },
  outcome_unverified: { label: "Outcome unverified", color: "#6b7689" },
  not_approved: { label: "Not approved", color: "#e0457b" },
  address_unparsed: { label: "No address", color: "#6b7689" },
  quarantined: { label: "Quarantined (source conflict)", color: "#c0392b" },
};
const landUseLayer = L.layerGroup();
// Own pane above permit markers so decisions are never hidden under them.
map.createPane("landuse").style.zIndex = 650;
let landUseLeadsOnly = false;
let landUseMeta = { count: 0, mapped: 0, coverage: null };

function landUsePopup(d) {
  const st = LANDUSE_STATUS[d.status] || { label: d.status };
  return `
    <div class="popup">
      <h3>${esc(d.address_text || "(no address extracted)")}</h3>
      <p class="pop-sub">${esc(d.land_use_label)} · Council ${esc(d.meeting_date || "date unknown")}</p>
      <span class="flood-badge">${esc(st.label)}</span>
      ${d.flood_class ? `<span class="flood-badge ${esc(d.flood_class)}">${esc(FLOOD_CLASS_LABELS[d.flood_class] || d.flood_class)}</span>` : ""}
      ${parcelHtml(d)}
      <p class="pop-sub">${esc((d.title || "").slice(0, 220))}${d.title_truncated ? "…" : ""}</p>
      <p class="pop-sub">Votes: ${esc(d.yes_count)} yes · ${esc(d.no_count)} no · ${esc(d.absent_count)} absent
        ${d.verified_outcome ? " · verified: " + esc(d.verified_outcome) : " · outcome not verified"}</p>
      <div class="landuse-links" data-key="${esc(d.decision_key)}">Loading linked permits…</div>
      <div class="pop-actions"><a class="btn small" href="/static/lead.html?key=${encodeURIComponent(d.decision_key)}">Open lead details</a></div>
    </div>`;
}

async function fillLandUseLinks(key, el) {
  try {
    const res = await api(`/api/land-use/decisions/${encodeURIComponent(key)}`);
    el.innerHTML = res.links.length
      ? res.links.map((l) => `<div>${esc(l.link_state)} · ${esc(l.match_tier)} · ${esc(l.source_permit_id)} ${esc(l.permit_type || "")} applied ${esc(l.applied_date || "?")}</div>`).join("")
      : "No linked permits";
  } catch (err) {
    el.textContent = `Could not load links: ${err.message}`;
  }
}

function renderLandUseControls() {
  $("landuse-leads-chip").classList.toggle("active", landUseLeadsOnly);
  if (!$("landuse-layer-toggle").checked) { $("landuse-legend").innerHTML = ""; return; }
  const cov = landUseMeta.coverage;
  $("landuse-legend").innerHTML = Object.entries(LANDUSE_STATUS).map(([, v]) => `
    <span class="flood-legend-item"><span class="flood-swatch" style="background:${v.color}"></span>${esc(v.label)}</span>`).join("")
    + `<span class="flood-legend-item">${esc(landUseMeta.mapped)} of ${esc(landUseMeta.count)} decisions mapped`
    + (cov ? ` · permits observed ${esc(cov.backfill_from)} – ${esc(cov.last_refresh_at || "unknown")}` : " · permit window not recorded")
    + `</span>`;
}

async function loadLandUse() {
  landUseLayer.clearLayers();
  const q = landUseLeadsOnly ? "?status=no_candidate_permit&limit=500" : "?limit=500";
  try {
    const res = await api(`/api/land-use/decisions${q}`);
    const mapped = res.decisions.filter((d) => d.lat != null);
    landUseMeta = { count: res.count, mapped: mapped.length, coverage: res.coverage };
    for (const d of mapped) {
      const st = LANDUSE_STATUS[d.status] || { color: "#888" };
      L.circleMarker([d.lat, d.lng], { pane: "landuse", radius: 8, color: "#0b1220", weight: 1.5, fillColor: st.color, fillOpacity: 0.95 })
        .bindPopup(landUsePopup(d))
        .on("popupopen", (e) => {
          const el = e.popup.getElement().querySelector(".landuse-links");
          if (el) fillLandUseLinks(el.dataset.key, el);
        })
        .addTo(landUseLayer);
    }
  } catch (err) {
    toast(`Land-use layer unavailable: ${err.message}`, true);
  }
  renderLandUseControls();
}

$("landuse-layer-toggle").addEventListener("change", (e) => {
  if (e.target.checked) {
    landUseLayer.addTo(map);
    loadLandUse();
  } else {
    map.removeLayer(landUseLayer);
    renderLandUseControls();
  }
});
$("landuse-leads-chip").addEventListener("click", () => {
  landUseLeadsOnly = !landUseLeadsOnly;
  if (!$("landuse-layer-toggle").checked) {
    $("landuse-layer-toggle").checked = true;
    landUseLayer.addTo(map);
  }
  loadLandUse();
});
renderLandUseControls();

// ---------- Planning Commission hearings (earliest signal) ----------
// A PC recommendation is advice to City Council, never an approval. Pins sit at
// the centre of the item's parcel (by GPIN); items without one are counted.
const PC_STATUS = {
  scheduled: { label: "Scheduled for hearing", color: "#3b82f6" },
  recommended_approval: { label: "PC recommended approval", color: "#a78bfa" },
  recommended_denial: { label: "PC recommended denial", color: "#f87171" },
  deferred: { label: "Deferred", color: "#fbbf24" },
  withdrawn: { label: "Withdrawn", color: "#6b7280" },
  recommendation_unmapped: { label: "Recommendation not recognized", color: "#9ca3af" },
  no_recommendation_found: { label: "No recommendation found", color: "#9ca3af" },
};
const pcLayer = L.layerGroup();
map.createPane("pc").style.zIndex = 640;
let pcMeta = { count: 0, mapped: 0, coverage: null };

function renderPcControls() {
  if (!$("pc-layer-toggle").checked) { $("pc-legend").innerHTML = ""; return; }
  const cov = pcMeta.coverage;
  $("pc-legend").innerHTML = Object.values(PC_STATUS).slice(0, 5).map((v) => `
    <span class="flood-legend-item"><span class="flood-swatch" style="background:${v.color}"></span>${esc(v.label)}</span>`).join("")
    + `<span class="flood-legend-item">${esc(pcMeta.mapped)} of ${esc(pcMeta.count)} hearing items mapped`
    + (cov ? ` · agendas found for ${esc(cov.agenda_found)} of ${esc(cov.months)} months${cov.verified ? "" : " (coverage unverified)"}` : "")
    + `. A recommendation is advice to City Council, not an approval.</span>`;
}

async function loadPc() {
  pcLayer.clearLayers();
  try {
    const res = await api("/api/land-use/pc-items?limit=2000");
    const mapped = res.items.filter((d) => d.lat != null);
    pcMeta = { count: res.count, mapped: mapped.length, coverage: res.coverage };
    for (const d of mapped) {
      const st = PC_STATUS[d.status] || { label: d.status, color: "#888" };
      const who = d.applicants.names.length ? d.applicants.names.map(esc).join("; ") : "Applicant: individual (name not shown)";
      L.circleMarker([d.lat, d.lng], { pane: "pc", radius: 7, color: "#0b1220", weight: 1.5, fillColor: st.color, fillOpacity: 0.9 })
        .bindPopup(`<div class="popup"><b>${esc(d.request || "Planning Commission item")}</b>
          <div>${esc(st.label)} · hearing ${esc(d.meeting_date)}</div>
          ${d.council ? `<div>${esc((FILING_STAGE[d.council.stage] || { label: d.council.stage }).label)} · ${esc(d.council.council_date)}</div>` : ""}
          <div>${who}</div><div class="muted">${esc(d.addresses || "")}</div>
          <a href="/static/lead.html?pc=${encodeURIComponent(d.item_key)}">Open hearing details</a></div>`)
        .addTo(pcLayer);
    }
  } catch (err) {
    toast(`Planning Commission layer unavailable: ${err.message}`, true);
  }
  renderPcControls();
}

$("pc-layer-toggle").addEventListener("change", (e) => {
  if (e.target.checked) {
    pcLayer.addTo(map);
    loadPc();
  } else {
    map.removeLayer(pcLayer);
    renderPcControls();
  }
});

// ---------- applications filed (earliest signal) ----------
// The city's filing layer has no status: a filing is "filed — outcome not yet known"
// until a Planning Commission record with the same Accela number says more, and the
// Clerk's Summary of Council Actions gives the Council outcome.
const FILING_STAGE = {
  filed: { label: "Filed — outcome not yet known", color: "#22d3ee" },
  scheduled: { label: "Scheduled for hearing", color: "#3b82f6" },
  recommended_approval: { label: "PC recommended approval", color: "#a78bfa" },
  recommended_denial: { label: "PC recommended denial", color: "#f87171" },
  deferred: { label: "Deferred", color: "#fbbf24" },
  withdrawn: { label: "Withdrawn", color: "#6b7280" },
  council_approved: { label: "Council approved", color: "#22c55e" },
  council_approved_with_conditions: { label: "Council approved (conditions)", color: "#16a34a" },
  council_denied: { label: "Council denied", color: "#dc2626" },
  council_deferred: { label: "Council deferred", color: "#f59e0b" },
  council_withdrawn: { label: "Withdrawn at Council", color: "#4b5563" },
  council_conflicting: { label: "Council actions differ", color: "#e879f9" },
};
const filingLayer = L.layerGroup();
map.createPane("filing").style.zIndex = 630;
let filingMeta = { count: 0, load: null };

function renderFilingControls() {
  if (!$("filing-layer-toggle").checked) { $("filing-legend").innerHTML = ""; return; }
  const load = filingMeta.load;
  $("filing-legend").innerHTML = Object.values(FILING_STAGE).map((v) => `
    <span class="flood-legend-item"><span class="flood-swatch" style="background:${v.color}"></span>${esc(v.label)}</span>`).join("")
    + `<span class="flood-legend-item">${esc(filingMeta.count)} applications`
    + (load ? ` · newest filed ${esc(load.max_date_sub)}` : " · no filing load") + `. A filing is not an approval.</span>`;
}

async function loadFilings() {
  filingLayer.clearLayers();
  try {
    const res = await api("/api/land-use/filings?limit=5000");
    filingMeta = { count: res.count, load: res.load };
    for (const f of res.filings) {
      const st = FILING_STAGE[f.stage] || { label: f.stage, color: "#888" };
      const who = f.applicant.name ? esc(f.applicant.name) : (f.applicant.withheld ? "Applicant: individual (name not shown)" : "");
      L.circleMarker([f.lat, f.lng], { pane: "filing", radius: 6, color: "#0b1220", weight: 1.2, fillColor: st.color, fillOpacity: 0.9 })
        .bindPopup(`<div class="popup"><b>${esc(f.plan_type_label)}</b>
          <div>${esc(st.label)} · filed ${esc(f.date_sub)}</div><div>${who}</div>
          <a href="/static/lead.html?filing=${encodeURIComponent(f.pln_id)}">Open application details</a></div>`)
        .addTo(filingLayer);
    }
  } catch (err) {
    toast(`Applications layer unavailable: ${err.message}`, true);
  }
  renderFilingControls();
}

$("filing-layer-toggle").addEventListener("change", (e) => {
  if (e.target.checked) {
    filingLayer.addTo(map);
    loadFilings();
  } else {
    map.removeLayer(filingLayer);
    renderFilingControls();
  }
});

// ---------- parcels (Stage 3) ----------
// Parcel flood is a screening estimate from area shares (the city's parcel
// data is "not valid for flood-zone determination"); shares are always shown.
const PARCEL_FLOOD_LABELS = {
  sfha_v: "SFHA – coastal V/VE", sfha_a: "SFHA – A/AE", shaded_x: "Shaded X (0.2%)",
  matched_non_sfha: "Mapped, outside SFHA", screening_sliver: "Hazard sliver below threshold – check",
  screening_partial_coverage: "Partly unmapped – check", no_polygon: "No FEMA polygon",
};
const parcelLayer = L.layerGroup().addTo(map);

function pct(v) { return v == null ? "?" : `${(100 * v).toFixed(1)}%`; }
function parcelShares(o) {
  return `V ${pct(o.parcel_share_sfha_v)} · A ${pct(o.parcel_share_sfha_a)} · shaded X ${pct(o.parcel_share_shaded_x)}`
    + ` · other ${pct(o.parcel_share_other_mapped)} · unmapped ${pct(o.parcel_share_unmapped)}`;
}

function parcelHtml(o) {
  if (!o.parcel_gpin) return o.parcel_method ? `<p class="pop-sub">Parcel: ${esc(o.parcel_method)}</p>` : "";
  return `
    <div class="parcel-box">
      <div>Parcel ${esc(o.parcel_gpin)} · ${esc(o.parcel_method)}${o.parcel_disputed ? ' · <b>disputed</b>' : ""}</div>
      <div>${esc(o.zoning || "?")} · ${esc(o.land_use || "?")}${o.acreage != null ? " · " + esc(o.acreage) + " ac" : ""}</div>
      ${o.parcel_flood_class ? `<div>Parcel flood (screening estimate): ${esc(PARCEL_FLOOD_LABELS[o.parcel_flood_class] || o.parcel_flood_class)}</div>
      <div class="parcel-shares">${esc(parcelShares(o))}</div>` : ""}
      <button class="btn small" data-parcel="${esc(o.parcel_gpin)}">Show parcel</button>
    </div>`;
}

async function showParcel(gpin) {
  try {
    const res = await fetch(`/api/parcels/${encodeURIComponent(gpin)}.geojson`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    parcelLayer.clearLayers();
    const layer = L.geoJSON(await res.json(), { interactive: false, style: { color: "#ffffff", weight: 2, fill: false, dashArray: "4 3" } });
    parcelLayer.addLayer(layer);
    map.fitBounds(layer.getBounds().pad(0.6));
  } catch (err) {
    toast(`Parcel unavailable: ${err.message}`, true);
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-parcel]");
  if (btn) showParcel(btn.dataset.parcel);
});

// ---------- boot ----------
boot();
