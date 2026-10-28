/* PermitIQ lead detail: one Council land-use decision with its evidence,
   parcel, later permits (match reason + review state), flood screening and
   data-as-of, on one screen. The API is read-only: "Mark bad link" and
   "Confirm" put verdicts in a review queue kept in this browser; "Download
   review file" saves a CSV for `python scripts/review_land_use.py --links`,
   which validates and applies them. */

const $ = (id) => document.getElementById(id);
const QUEUE_KEY = "permitiq.linkReviewQueue.v1";
const key = new URLSearchParams(location.search).get("key");
const pcKey = new URLSearchParams(location.search).get("pc");
const filingId = new URLSearchParams(location.search).get("filing");
const caseId = new URLSearchParams(location.search).get("case");

// esc, escLinkify, buildReviewCsv, buildHistoryHtml, buildLeadFactsHtml, buildPcItemHtml, buildFilingHtml and buildCaseHtml come from /static/lead_util.js.

function pct(v) { return v == null ? "–" : `${(100 * v).toFixed(1)}%`; }

const STATUS_TEXT = {
  no_candidate_permit: ["Approved, no permit yet", "Verified approval; no matching permit in the permit data observed between the dates below. This does not mean nothing was built."],
  no_candidate_window_expired: ["No permit, window expired", "Verified approval; no matching permit within the link window."],
  candidate_permit: ["Candidate permit", "Possible matching permits found; a person has to confirm one."],
  confirmed_permit: ["Permit confirmed", "A reviewer confirmed a permit belongs to this project."],
  observation_uncertain: ["Permit window uncertain", "The approval predates the verified permit backfill, or the permit data is stale or of unknown age."],
  outcome_unverified: ["Outcome not verified", "Vote counts are never treated as approval; the outcome needs a review with evidence."],
  outcome_review_stale: ["Outcome changed — re-review", "The title, result, date or source conflicts changed after the review; the old verdict no longer applies."],
  quarantined: ["Quarantined", "Member vote rows disagree on title, result or date; a reviewer must resolve the conflict."],
  not_approved: ["Not approved", "The verified outcome is not an approval."],
  address_unparsed: ["No address", "No address or GPIN could be read from the title."],
};
const FLOOD_LABELS = {
  sfha_v: "SFHA – coastal V/VE", sfha_a: "SFHA – A/AE", shaded_x: "Shaded X (0.2%)", matched_non_sfha: "Mapped, outside SFHA",
  screening_sliver: "Hazard sliver below threshold – check", screening_partial_coverage: "Partly unmapped – check",
  no_polygon: "No FEMA polygon", not_geocoded: "Not geocoded", untagged: "Not yet tagged", no_parcel: "No parcel",
  ambiguous: "Ambiguous parcel", parcel_disputed: "Disputed parcel",
};
const FLOOD_SOURCE_TEXT = {
  decision_point: "from the decision's own geocoded address", confirmed_permit: "from a confirmed permit's point",
  candidate_permit: "from a candidate (unconfirmed) permit's point",
};
const TIER_TEXT = {
  gpin: "GPIN printed in the title", address_exact: "Same street address", same_parcel: "Same parcel",
  address_range: "Address within the printed range", manual: "Added by a reviewer",
};

// ---------- review queue (browser-local; the page works without storage) ----------
function loadQueue() {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch { return []; }
}
function saveQueue(q) {
  try { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); } catch { /* storage unavailable: queue lives for this page only */ }
}
let queue = loadQueue();

function queued(permitId) {
  return queue.find((q) => q.decision_key === key && String(q.permit_id) === String(permitId));
}

function renderQueueBanner() {
  $("queue-banner").hidden = queue.length === 0;
  $("queue-text").textContent = `${queue.length} link verdict(s) pending import — download the review file and run `
    + "python scripts/review_land_use.py --links <file>";
}

function enqueue(permitId, verdict) {
  const reviewer = (prompt(`${verdict === "rejected" ? "Mark bad link" : "Confirm link"} — your name (required):`) || "").trim();
  if (!reviewer) return;
  const note = (prompt("Note (why):") || "").trim();
  queue = queue.filter((q) => !(q.decision_key === key && String(q.permit_id) === String(permitId)));
  queue.push({ decision_key: key, permit_id: permitId, verdict, reviewer, note, queued_at: new Date().toISOString() });
  saveQueue(queue);
  render(current);
}

$("queue-download").addEventListener("click", () => {
  const blob = new Blob([buildReviewCsv(queue)], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `link_reviews_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});
$("queue-clear").addEventListener("click", () => {
  if (!confirm("Remove all pending verdicts from this browser?")) return;
  queue = [];
  saveQueue(queue);
  render(current);
});

// ---------- render ----------
let current = null;

function reviewState(l) {
  const q = queued(l.permit_id);
  if (q) return `<span class="state pending">pending import: ${esc(q.verdict)} by ${esc(q.reviewer)}</span>`;
  if (l.link_state === "confirmed") {
    return `<span class="state confirmed">confirmed${l.review_reviewer ? " by " + esc(l.review_reviewer) : ""}</span>`;
  }
  return `<span class="state candidate">candidate — not reviewed</span>`;
}

function dataAsOf(d) {
  const s = d.data_as_of.sources || {};
  const w = d.data_as_of.permit_window || d.coverage || {};
  const item = (label, src) => `<div><dt>${esc(label)}</dt><dd>${esc(s[src]?.as_of || "not recorded")}</dd></div>`;
  return `
    <dl class="asof">
      ${item("Council export", "council_export")}
      <div><dt>Permits observed</dt><dd>${esc(w.backfill_from || "?")} → ${esc(w.last_refresh_at || "?")}</dd></div>
      ${item("Parcel layer", "parcels")}
      ${item("FEMA flood load", "fema")}
    </dl>
    <p class="muted small">"No candidate permit" means no matching permit appeared in the permit data observed between
      ${esc(w.backfill_from || "?")} and ${esc(w.last_refresh_at || "?")}. It does not mean nothing was built.</p>`;
}

function render(d) {
  if (!d) return;
  const dec = d.decision;
  const [stLabel, stText] = STATUS_TEXT[dec.status] || [dec.status, ""];
  const rv = d.review;
  const conflicts = d.conflicts.map((c) => {
    const v = JSON.parse(c.variants_json);
    return `<li><b>${esc(c.field)}</b>: ${Object.entries(v).map(([t, n]) => `“${esc(t)}” (${n} member rows)`).join(" vs ")}</li>`;
  }).join("");
  const parcels = d.parcel_rows.map((p) => `
    <tr>
      <td>${esc(p.raw_text)}</td>
      <td>${p.display_gpin ? esc(p.display_gpin) : "–"}<div class="muted small">${esc(p.parcel_state)}</div></td>
      <td>${p.dispute_reason ? `<span class="warn">disputed (${esc(p.dispute_reason.replace("_", "-"))})</span>
            <div class="muted small">${esc(p.set_verdict || p.review_verdict || "not reviewed")}${p.alt_gpin ? " · other parcel " + esc(p.alt_gpin) : ""}</div>` : "—"}</td>
      <td>${esc(p.zoning || "–")} · ${esc(p.land_use || "–")}${p.acreage != null ? ` · ${esc(p.acreage)} ac` : ""}</td>
      <td>${p.parcel_flood_class ? esc(FLOOD_LABELS[p.parcel_flood_class] || p.parcel_flood_class) : "–"}
        ${p.parcel_flood_class ? `<div class="muted small">V ${pct(p.share_sfha_v)} · A ${pct(p.share_sfha_a)} · shaded X ${pct(p.share_shaded_x)} · other ${pct(p.share_other_mapped)} · unmapped ${pct(p.share_unmapped)}</div>` : ""}</td>
    </tr>`).join("");
  const links = d.links.map((l) => `
    <tr>
      <td><b>${esc(l.source_permit_id)}</b><div class="muted small">${esc(l.address)}</div><div class="muted small">${esc(l.permit_type || "")}</div></td>
      <td>${esc(l.applied_date || "–")}<div class="muted small">issued ${esc(l.issued_date || "–")}</div></td>
      <td>${l.is_new_construction == null ? "–" : l.is_new_construction ? "yes" : "no"}</td>
      <td>${esc(TIER_TEXT[l.match_tier] || l.match_tier)}<div class="muted small">${esc(l.matched_on || "")}</div></td>
      <td>point: ${esc(FLOOD_LABELS[l.flood_class] || l.flood_class || "–")}<div class="muted small">parcel: ${esc(FLOOD_LABELS[l.parcel_flood_class] || l.parcel_flood_class || "–")}</div></td>
      <td>${reviewState(l)}
        <div class="row-actions">
          <button class="btn small danger" data-verdict="rejected" data-permit="${esc(l.permit_id)}">Mark bad link</button>
          <button class="btn small" data-verdict="confirmed" data-permit="${esc(l.permit_id)}">Confirm</button>
          <button class="btn small" data-history="${esc(l.permit_id)}">Review history</button>
        </div></td>
    </tr>`).join("");
  const firstParcel = d.parcel_rows.find((p) => p.display_gpin);

  $("lead").innerHTML = `
    <section class="card">
      <div class="card-head">
        <h2>${esc(dec.address_text || "(no address extracted)")}</h2>
        <span class="status-pill s-${esc(dec.status)}">${esc(stLabel)}</span>
      </div>
      <p class="muted">${esc(stText)}</p>
      <h3>Council decision</h3>
      <p class="title">${esc(dec.title || "(title withheld: source rows conflict)")}${dec.title_truncated ? ' <span class="warn">[title cut at 400 characters in source]</span>' : ""}</p>
      <dl class="facts">
        <div><dt>Meeting</dt><dd>${esc(dec.meeting_date || "unknown")} · IQM2 meeting ${esc(d.meeting_id)}</dd></div>
        <div><dt>Votes</dt><dd>${esc(dec.yes_count)} yes · ${esc(dec.no_count)} no · ${esc(dec.absent_count)} absent · ${esc(dec.abstain_count)} abstain · result “${esc(dec.result_raw || "–")}”</dd></div>
        <div><dt>Type</dt><dd>${esc(dec.land_use_label)}</dd></div>
        <div><dt>Verified outcome</dt><dd>${rv ? `${esc(rv.verified_outcome)} by ${esc(rv.reviewer)} (${esc(rv.reviewed_at)})${rv.valid ? "" : ' <span class="warn">stale — facts changed since</span>'}` : '<span class="warn">not verified</span>'}</dd></div>
        ${rv ? `<div><dt>Evidence</dt><dd>${escLinkify(rv.evidence)}</dd></div>` : ""}
      </dl>
      ${conflicts ? `<h3>Source conflicts</h3><ul>${conflicts}</ul>` : ""}
    </section>

    <section class="card">
      <h3>Lead summary</h3>
      ${buildLeadFactsHtml(d.lead_facts)}
    </section>

    <section class="card">
      <h3>Parcel</h3>
      ${d.parcel_rows.length ? `<table class="grid"><thead><tr><th>From title</th><th>Parcel</th><th>Dispute</th><th>Zoning · use</th><th>Flood (screening estimate)</th></tr></thead><tbody>${parcels}</tbody></table>` : '<p class="muted">No address or GPIN in the title.</p>'}
      <div id="parcel-map" class="mini-map" ${firstParcel ? "" : "hidden"}></div>
      <p class="muted small">Parcel flood classes are a screening estimate from parcel-polygon area shares, not a flood-zone determination.</p>
    </section>

    <section class="card">
      <h3>Later permits</h3>
      ${d.links.length ? `<table class="grid"><thead><tr><th>Permit</th><th>Applied</th><th>New constr.</th><th>Match reason</th><th>Flood</th><th>Review</th></tr></thead><tbody>${links}</tbody></table>` : '<p class="muted">No candidate permits in the observed permit data.</p>'}
      <p class="muted small">Every automatic match is a candidate: a property match can belong to a different project on the same parcel.</p>
      <div id="history" hidden></div>
    </section>

    <section class="card">
      <h3>Flood screening</h3>
      <p>Decision point: ${esc(FLOOD_LABELS[dec.flood_class] || dec.flood_class || "–")} <span class="muted small">(${esc(FLOOD_SOURCE_TEXT[dec.flood_source] || "no source")})</span></p>
      <p>Parcel: ${esc(FLOOD_LABELS[dec.parcel_flood_class] || dec.parcel_flood_class || "–")}
        ${dec.parcel_flood_class ? `<span class="muted small">V ${pct(dec.parcel_share_sfha_v)} · A ${pct(dec.parcel_share_sfha_a)} · shaded X ${pct(dec.parcel_share_shaded_x)} · other ${pct(dec.parcel_share_other_mapped)} · unmapped ${pct(dec.parcel_share_unmapped)}</span>` : ""}</p>
    </section>

    <section class="card">
      <h3>Data as of</h3>
      ${dataAsOf(d)}
    </section>`;

  document.querySelectorAll("[data-verdict]").forEach((b) =>
    b.addEventListener("click", () => enqueue(Number(b.dataset.permit), b.dataset.verdict)));
  document.querySelectorAll("[data-history]").forEach((b) =>
    b.addEventListener("click", () => showHistory(Number(b.dataset.history))));
  renderQueueBanner();
  if (firstParcel && window.L) drawParcel(firstParcel.display_gpin);
}

async function showHistory(permitId) {
  const box = $("history");
  box.hidden = false;
  const link = current.links.find((l) => l.permit_id === permitId);
  try {
    const qs = new URLSearchParams({ decision_key: key, permit_id: String(permitId) });
    const res = await fetch(`/api/audit/decision_link_review?${qs}`);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    box.innerHTML = `<h3>Review history — permit ${esc(link ? link.source_permit_id : permitId)}</h3>${buildHistoryHtml(body)}`;
  } catch (err) {
    box.innerHTML = `<p class="warn">Could not load review history: ${esc(err.message)}</p>`;
  }
}

async function drawParcel(gpin) {
  try {
    const res = await fetch(`/api/parcels/${encodeURIComponent(gpin)}.geojson`);
    if (!res.ok) return;
    const map = L.map("parcel-map", { zoomControl: false, attributionControl: false });
    const layer = L.geoJSON(await res.json(), { style: { color: "#4f8ef7", weight: 2, fillOpacity: 0.15 } }).addTo(map);
    map.fitBounds(layer.getBounds().pad(0.4));
  } catch { /* the outline is optional */ }
}

async function bootPc() {
  try {
    const res = await fetch(`/api/land-use/pc-items/${encodeURIComponent(pcKey)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    $("lead").innerHTML = buildPcItemHtml(body);
  } catch (err) {
    $("lead").innerHTML = `<p class="warn">Could not load this hearing item: ${esc(err.message)}</p>`;
  }
}

async function bootFiling() {
  try {
    const res = await fetch(`/api/land-use/filings/${encodeURIComponent(filingId)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    $("lead").innerHTML = buildFilingHtml(body);
    if (window.L && body.geometry) {
      const map = L.map("filing-map", { zoomControl: false, attributionControl: false });
      const layer = L.geoJSON(body.geometry, { style: { color: "#38bdf8", weight: 2, fillOpacity: 0.15 } }).addTo(map);
      map.fitBounds(layer.getBounds().pad(0.3));
    }
  } catch (err) {
    $("lead").innerHTML = `<p class="warn">Could not load this application: ${esc(err.message)}</p>`;
  }
}

async function bootCase() {
  try {
    const res = await fetch(`/api/land-use/cases/${encodeURIComponent(caseId)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    $("lead").innerHTML = buildCaseHtml(body);
    const tag = document.querySelector(".tagline");
    if (tag) tag.textContent = "Lead detail · Chesapeake";
    const shape = body.geometry || (body.lat != null ? { type: "Point", coordinates: [body.lng, body.lat] } : null);
    if (window.L && shape) {
      const map = L.map("case-map", { zoomControl: false, attributionControl: false });
      const layer = L.geoJSON(shape, { style: { color: "#38bdf8", weight: 2, fillOpacity: 0.15 } }).addTo(map);
      if (shape.type === "Point") map.setView(layer.getBounds().getCenter(), 17);
      else map.fitBounds(layer.getBounds().pad(0.3));
    }
  } catch (err) {
    $("lead").innerHTML = `<p class="warn">Could not load this case: ${esc(err.message)}</p>`;
  }
}

async function boot() {
  if (caseId) { bootCase(); return; }
  if (filingId) { bootFiling(); return; }
  if (pcKey) { bootPc(); return; }
  if (!key) { $("lead").innerHTML = '<p class="warn">No decision key in the address (?key=…).</p>'; return; }
  try {
    const res = await fetch(`/api/land-use/decisions/${encodeURIComponent(key)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
    current = body;
    // a queued verdict that the database already reflects is no longer pending
    const settled = new Map(body.links.filter((l) => l.review_verdict).map((l) => [String(l.permit_id), l.review_verdict]));
    const before = queue.length;
    queue = queue.filter((q) => !(q.decision_key === key && settled.get(String(q.permit_id)) === q.verdict));
    if (queue.length !== before) saveQueue(queue);
    render(current);
  } catch (err) {
    $("lead").innerHTML = `<p class="warn">Could not load this lead: ${esc(err.message)}</p>`;
  }
}

boot();
