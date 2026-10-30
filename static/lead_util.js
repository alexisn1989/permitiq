/* Lead-page helpers. Pure functions, no DOM, so tests can run them under
   node: HTML escaping (every string from the API goes through esc /
   escLinkify), and the review-queue CSV, whose output must be accepted as-is
   by `python scripts/review_land_use.py --links` (columns decision_key,
   permit_id, verdict, reviewer, note). */
(function (root) {
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // Escaped text in which bare http(s) URLs become links; the href is escaped too,
  // and only http(s) is ever linked (no javascript: or data: URLs).
  function escLinkify(s) {
    const parts = String(s ?? "").split(/(https?:\/\/[^\s<>"']+)/g);
    return parts.map((p, i) => (i % 2
      ? `<a href="${esc(p)}" target="_blank" rel="noopener noreferrer">${esc(p)}</a>`
      : esc(p))).join("");
  }

  const HEADER = ["decision_key", "permit_id", "verdict", "reviewer", "note"];

  function cell(v) {
    const s = String(v == null ? "" : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  // queue: [{decision_key, permit_id, verdict: "confirmed"|"rejected", reviewer, note}]
  function buildReviewCsv(queue) {
    const lines = [HEADER.join(",")];
    for (const q of queue) lines.push(HEADER.map((h) => cell(q[h])).join(","));
    return lines.join("\r\n") + "\r\n";
  }

  const ACTION_TEXT = { insert: "added", update: "changed", replace: "replaced", delete: "removed",
                        baseline_snapshot: "baseline snapshot" };

  // body: GET /api/audit/... response. Escaped HTML; the review's author and the
  // (self-reported) acting user are shown separately.
  function buildHistoryHtml(body) {
    const rows = (body.history || []).map((h) => {
      const v = h.new || h.old || {};
      const im = h.import || {};
      const author = h.action === "delete" ? `author of removed review: ${esc(h.review_author)}` : `by ${esc(h.review_author)}`;
      const src = im.source_file ? `${esc(im.source_file)} <span class="muted">sha256 ${esc(String(im.source_sha256 || "").slice(0, 12))}…</span>`
        : '<span class="muted">source file not recorded</span>';
      return `<li><b>${esc(ACTION_TEXT[h.action] || h.action)}</b> ${esc(h.recorded_at)} — ${esc(v.verdict || v.verified_outcome || "")} ${author}`
        + `<div class="muted small">import ${esc(im.import_id)} (${esc(im.kind)}) · run by ${esc(im.acting_user || "unknown")} (self-reported) · ${src}</div></li>`;
    }).join("");
    return (rows ? `<ul class="history">${rows}</ul>` : '<p class="muted">No recorded history.</p>')
      + (body.baseline_note ? `<p class="muted small">${esc(body.baseline_note)}</p>` : "")
      + `<p class="muted small">${esc(body.notice || "")}</p>`;
  }

  const SCALE_LABEL = { acres: "acres", units: "units", lots: "lots", square_feet: "sq ft" };

  function fmtNum(v) { return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }); }

  // f: lead_facts from GET /api/land-use/decisions/{key}. Escaped HTML. Title-derived values
  // are shown with the exact text they came from and labeled unverified.
  function buildLeadFactsHtml(f) {
    if (!f) return "";
    const a = f.applicant || {};
    const who = a.kind === "organization" ? `${esc(a.name)} <span class="muted small">from “${esc(a.text)}”</span>`
      : a.kind === "withheld" ? '<span class="muted">individual applicant — name not shown</span>'
      : '<span class="muted">not found in title</span>';
    const req = f.request && f.request.text ? esc(f.request.text) : '<span class="muted">not found in title</span>';
    const sizes = (f.scale_from_title || []).map((x) =>
      `${esc(fmtNum(x.value))} ${esc(SCALE_LABEL[x.kind] || x.kind)} <span class="muted small">(“${esc(x.text)}”)</span>`);
    if (f.parcel_acres != null) {
      sizes.push(`${esc(fmtNum(f.parcel_acres))} acres <span class="muted small">(parcel layer, ${esc(f.parcels)} parcel${f.parcels === 1 ? "" : "s"})</span>`);
    }
    const t = f.timing || {};
    let when;
    if (!t.shown) {
      when = `<span class="muted">${esc(t.reason || "not available")}</span>`;
    } else {
      when = `approved ${esc(t.approved_on)} · ${esc(t.days_since_approval ?? "–")} days to the last permit refresh (${esc(t.observed_to || "–")}) · link window ends ${esc(t.window_end)}`;
      const b = t.benchmark || {};
      if (t.typical_first_application) {
        const r = t.typical_first_application;
        const span = b.p25_days === b.p75_days ? `${esc(b.median_days)} days`
          : `${esc(b.p25_days)}–${esc(b.p75_days)} days (median ${esc(b.median_days)})`;
        const dates = r.from === r.to ? esc(r.from) : `${esc(r.from)} to ${esc(r.to)}`;
        when += `<div class="muted small">Across confirmed approvals (n=${esc(b.n)}), the first permit application came ${span} after approval: for this lead, ${dates}. A benchmark, not a prediction.</div>`;
        if (t.past_typical_range) {
          when += '<div class="warn small">No permit observed yet, and this approval is older than that typical range.</div>';
        }
      } else if (b.configured === false) {
        when += '<div class="muted small">Benchmark not configured.</div>';
      } else {
        when += `<div class="muted small">Not enough confirmed approvals for a benchmark yet (n=${esc(b.n ?? 0)}, needs ${esc(b.min_n ?? "more")}).</div>`;
      }
    }
    return `<dl class="facts">
        <div><dt>Applicant</dt><dd>${who}</dd></div>
        <div><dt>Request</dt><dd>${req}</dd></div>
        <div><dt>Size</dt><dd>${sizes.length ? sizes.join("<br>") : '<span class="muted">not stated in title</span>'}</dd></div>
        <div><dt>Timing</dt><dd>${when}</dd></div>
      </dl>
      <p class="muted small">${esc(f.source_note)}${f.title_truncated ? " The title was cut at 400 characters in the source, so details may be missing." : ""}</p>`;
  }

  const PC_STATUS_TEXT = {
    scheduled: "Scheduled for a Planning Commission hearing",
    recommended_approval: "Planning Commission recommended approval — City Council decides",
    recommended_denial: "Planning Commission recommended denial — City Council decides",
    deferred: "Deferred by the Planning Commission",
    withdrawn: "Withdrawn",
    recommendation_unmapped: "Recommendation recorded but not recognized — see the minutes",
    no_recommendation_found: "Minutes found, but no recommendation for this item",
  };
  // Council stages come from the City Clerk's Summary of Council Actions (the official record)
  const COUNCIL_STAGE_TEXT = {
    council_approved: "Approved by City Council",
    council_approved_with_conditions: "Approved by City Council, with conditions",
    council_denied: "Denied by City Council",
    council_withdrawn: "Withdrawn at City Council",
    council_deferred: "Deferred by City Council",
    council_conflicting: "City Council actions on this item differ — see the Clerk's record",
  };
  const OUTCOME_TEXT = {
    approved: "Approved", approved_with_conditions: "Approved with conditions", denied: "Denied",
    deferred: "Deferred", deferred_indefinitely: "Deferred indefinitely", withdrawn: "Withdrawn",
    unmapped: "Not recognized",
  };
  const FLOOD_SHORT = { sfha_v: "SFHA – coastal V/VE", sfha_a: "SFHA – A/AE", shaded_x: "Shaded X (0.2%)",
    matched_non_sfha: "Outside the SFHA", screening_sliver: "Outside the SFHA (sliver)", no_polygon: "No FEMA polygon" };

  function names(n, what) {
    if (!n) return "–";
    const shown = (n.names || []).map(esc).join("; ");
    const hidden = n.withheld ? `${n.withheld} individual ${what}${n.withheld === 1 ? "" : "s"} — name not shown` : "";
    return [shown, hidden ? `<span class="muted">${hidden}</span>` : ""].filter(Boolean).join("<br>") || "–";
  }

  // item: GET /api/land-use/pc-items/{key}. Escaped HTML for the Planning Commission lead page.
  const BOARD_TEXT = { cbpa: "Chesapeake Bay Preservation Area Board", wetlands_board: "Wetlands Board",
    hrb: "Historical Review Board" };

  // sc: the site_constraints block of a lead (GET /api/parcels/{gpin}/constraints shape). Escaped HTML.
  function buildConstraintsHtml(sc) {
    if (!sc) return "";
    const rows = (sc.constraints || []).map((c) => {
      const by = c.classification ? "" : (c.detail && c.detail.by || []).slice(0, 4).map((b) =>
        `${esc(b.name)} ${esc(fmtNum(b.acres))} ac`).join("; ");
      return `<tr><td>${esc(c.label)}</td><td>${esc(c.fact)}${by ? `<br><span class="muted small">${by}</span>` : ""}</td>
        <td>${esc(c.gpin)}</td><td class="muted small">${esc((c.as_of || "").slice(0, 10))}</td></tr>`;
    }).join("");
    const hist = (sc.board_history || []).slice(0, 12).map((h) =>
      `<li>${esc(h.date_sub)} — ${esc(BOARD_TEXT[h.board] || h.board)}${h.pln_type ? ` (${esc(h.pln_type)})` : ""} · ${esc(h.pln_id)}${h.applicant && h.applicant.name ? ` · ${esc(h.applicant.name)}` : ""}</li>`).join("");
    const checked = (sc.layers_checked || []).map((l) => esc(l.label)).join(", ");
    return `
      <section class="card">
        <h3>Site constraints (screening)</h3>
        ${rows ? `<table class="grid"><thead><tr><th>Layer</th><th>Measured on the parcel</th><th>Parcel</th><th>Layer as of</th></tr></thead><tbody>${rows}</tbody></table>`
          : `<p class="muted">${checked ? "None of the checked layers touches these parcels." : "No constraint layers loaded yet."}</p>`}
        <h3>History with the city's boards on these parcels</h3>
        ${hist ? `<ul>${hist}</ul>` : `<p class="muted">${(sc.boards_checked || []).length ? "No prior board actions found." : (sc.boards_note || "Board histories not loaded yet.")}</p>`}
        <p class="muted small">Layers checked: ${checked || "none"}. Not covered: ${esc((sc.not_covered || []).join("; "))}.</p>
        <p class="muted small">${esc(sc.notice || "")}</p>
      </section>`;
  }

  // actions: Clerk items linked to a PC item (council_actions in the API). Escaped HTML.
  function buildCouncilHtml(council, actions, notice) {
    const rows = (actions || []).map((a) => {
      const tally = a.aye == null ? "tally not readable" : `${esc(a.aye)}–${esc(a.nay)}`;
      const defer = a.deferred_to ? ` to ${esc(a.deferred_to)}` : "";
      const names = a.applicant && a.applicant.names.length ? esc(a.applicant.names.join("; ")) : "";
      const withheld = a.applicant && a.applicant.withheld ? ' <span class="muted small">(individuals not shown)</span>' : "";
      return `<li>${esc(a.meeting_date)} · item ${esc(a.item_label || "label not readable")} — <b>${esc(OUTCOME_TEXT[a.outcome] || a.outcome)}${defer}</b>, ${tally}
        <span class="muted small">(“${esc(a.motion_raw || "")}”)</span>${names ? `<br>${names}` : ""}${withheld}
        ${a.ambiguous ? `<br><span class="warn small">Possible match only: ${esc(a.reason || "")}</span>` : ""}
        <br><a href="${esc(a.source_url)}" target="_blank" rel="noopener noreferrer">Clerk's summary, page ${esc(a.page)}</a></li>`;
    }).join("");
    const head = council
      ? `<p><span class="status-pill">${esc(COUNCIL_STAGE_TEXT[council.stage] || council.stage)}</span> ${esc(council.council_date)}${council.deferred_to ? ` · to ${esc(council.deferred_to)}` : ""}</p>`
      : '<p class="muted">No Council action found yet in the Clerk\'s summaries.</p>';
    return `
      <section class="card">
        <h3>City Council action (Clerk's record)</h3>
        ${head}
        ${rows ? `<ul>${rows}</ul>` : ""}
        <p class="muted small">${esc(notice || "")}</p>
      </section>`;
  }

  function buildPcItemHtml(it) {
    const r = it.recommendation || {};
    const vote = r.aye == null ? "" : ` · vote ${esc(r.aye)}–${esc(r.nay)} (abstain ${esc(r.abstain)}, absent ${esc(r.absent)})`;
    const src = it.status_source === "annotated_agenda"
      ? ` <span class="muted small">(from the annotated agenda: “${esc(it.agenda_result_raw)}${it.agenda_council_on ? ` — Council on ${esc(it.agenda_council_on)}` : ""}”)</span>` : "";
    const sizes = (it.size_from_description || []).map((x) => `${esc(fmtNum(x.value))} ${esc(SCALE_LABEL[x.kind] || x.kind)} <span class="muted small">(“${esc(x.text)}”)</span>`);
    const parcels = (it.parcels || []).map((p) => p.gpin
      ? `<tr><td>${esc(p.agenda_gpin)}</td><td>${esc(p.gpin)}${p.ambiguous ? ' <span class="warn small">one of several parcels</span>' : ""}</td><td>${esc(p.zoning || "–")} · ${esc(p.land_use || "–")}${p.acreage != null ? ` · ${esc(fmtNum(p.acreage))} ac` : ""}</td><td>${esc(FLOOD_SHORT[p.parcel_flood_class] || p.parcel_flood_class || "–")}</td></tr>`
      : `<tr><td>${esc(p.agenda_gpin)}</td><td colspan="3" class="muted">not in the active parcel layer</td></tr>`).join("");
    const cands = (it.council_candidates || []).map((c) =>
      `<li><a href="/static/lead.html?key=${encodeURIComponent(c.decision_key)}">${esc(c.meeting_date)} — ${esc((c.title || "").slice(0, 140))}</a> <span class="muted small">(matched on ${esc(c.matched_on)}, ${esc(c.council_after_days)} days after the hearing; ${esc(c.status)})</span></li>`).join("");
    return `
      <section class="card">
        <div class="card-head"><h2>${esc(it.addresses || it.request || it.item_key)}</h2>
          <span class="status-pill">${esc(it.council ? COUNCIL_STAGE_TEXT[it.council.stage] || it.council.stage : PC_STATUS_TEXT[it.status] || it.status)}</span></div>
        <p class="muted">${esc(PC_STATUS_TEXT[it.status] || it.status)}${vote}${src}</p>
        <h3>Planning Commission hearing</h3>
        <dl class="facts">
          <div><dt>Hearing</dt><dd>${esc(it.meeting_date)} · item ${esc(it.item_label)}${it.section ? ` · ${esc(it.section)}` : ""}</dd></div>
          <div><dt>Applicant</dt><dd>${names(it.applicants, "applicant")}</dd></div>
          <div><dt>Property owner</dt><dd>${names(it.owners, "owner")}</dd></div>
          <div><dt>Request</dt><dd>${esc(it.request || "–")}</dd></div>
          <div><dt>Description</dt><dd>${esc(it.description || "–")}</dd></div>
          <div><dt>Size</dt><dd>${sizes.length ? sizes.join("<br>") : '<span class="muted">not stated</span>'}</dd></div>
          <div><dt>Council district</dt><dd>${esc(it.council_district_text || "–")}</dd></div>
          <div><dt>Accela records</dt><dd>${esc((it.accela_records || []).join(", ") || "–")}</dd></div>
          <div><dt>Staff planner</dt><dd>${esc(it.staff_planner || "–")}</dd></div>
          <div><dt>Sources</dt><dd>${escLinkify((it.agenda || {}).url || "")}${r.minutes_url ? `<br>${escLinkify(r.minutes_url)}` : ""}</dd></div>
        </dl>
        ${(it.warnings || []).length ? `<p class="warn small">Parsing notes: ${esc(it.warnings.join("; "))}</p>` : ""}
      </section>
      <section class="card">
        <h3>Parcels</h3>
        ${parcels ? `<table class="grid"><thead><tr><th>Agenda GPIN</th><th>Parcel</th><th>Zoning · use</th><th>Flood (screening estimate)</th></tr></thead><tbody>${parcels}</tbody></table>` : '<p class="muted">No GPIN on the agenda.</p>'}
      </section>
      ${buildConstraintsHtml(it.site_constraints)}
      ${buildCouncilHtml(it.council, it.council_actions, it.council_notice)}
      <section class="card">
        <h3>Possible City Council decisions (vote data)</h3>
        ${cands ? `<ul>${cands}</ul>` : '<p class="muted">None found yet in the Council data.</p>'}
        <p class="muted small">Matched by parcel or street address within 12 months after the hearing: candidates, not confirmed.</p>
      </section>
      <p class="muted small">${esc(it.notice || "")}</p>`;
  }

  const FILING_STAGE_TEXT = Object.assign({ filed: "Filed — outcome not yet known" }, PC_STATUS_TEXT, COUNCIL_STAGE_TEXT);

  function party(p, what) {
    if (!p || (!p.name && !p.withheld)) return '<span class="muted">not recorded</span>';
    return p.name ? esc(p.name) : `<span class="muted">${what} is an individual — name not shown</span>`;
  }

  // f: GET /api/land-use/filings/{pln_id}. Escaped HTML for the filing lead page.
  function buildFilingHtml(f) {
    const parcels = (f.parcels || []).filter((p) => p.gpin).map((p) =>
      `<tr><td>${esc(p.gpin)}</td><td>${esc(p.zoning || "–")} · ${esc(p.land_use || "–")}${p.acreage != null ? ` · ${esc(fmtNum(p.acreage))} ac` : ""}</td><td>${esc(FLOOD_SHORT[p.parcel_flood_class] || p.parcel_flood_class || "–")}</td></tr>`).join("");
    const hearings = (f.pc_items || []).map((i) =>
      `<li><a href="/static/lead.html?pc=${encodeURIComponent(i.item_key)}">${esc(i.meeting_date)} — ${esc(i.request || "hearing item")}</a> <span class="muted small">(${esc(PC_STATUS_TEXT[i.status] || i.status)})</span></li>`).join("");
    const permits = (f.permits_on_site || []).map((p) =>
      `<li>${esc(p.source_permit_id)} · ${esc(p.permit_type || "")} · applied ${esc(p.applied_date)} <span class="muted small">${esc(p.address || "")}</span></li>`).join("");
    return `
      <section class="card">
        <div class="card-head"><h2>${esc(f.plan_type_label)} · ${esc(f.pln_id)}</h2>
          <span class="status-pill">${esc(FILING_STAGE_TEXT[f.stage] || f.stage)}</span></div>
        <p class="muted">Application filed ${esc(f.date_sub)}${f.hearing_date ? ` · Planning Commission hearing ${esc(f.hearing_date)}` : ""}${f.council_date ? ` · City Council ${esc(f.council_date)}` : ""}</p>
        <h3>Application</h3>
        <dl class="facts">
          <div><dt>Filed</dt><dd>${esc(f.date_sub)}</dd></div>
          <div><dt>Type</dt><dd>${esc(f.plan_type_label)}${f.pln_type && f.plan_type_label !== f.pln_type ? ` <span class="muted small">(${esc(f.pln_type)})</span>` : ""}</dd></div>
          <div><dt>Applicant</dt><dd>${party(f.applicant, "The applicant")}</dd></div>
          <div><dt>Project</dt><dd>${party(f.project, "The project name")}</dd></div>
          <div><dt>Accela record</dt><dd>${esc(f.pln_id)}${f.deeplink ? `<br>${escLinkify(f.deeplink)}` : ""}</dd></div>
        </dl>
        <div id="filing-map" class="mini-map"></div>
      </section>
      <section class="card">
        <h3>Parcels inside the application boundary</h3>
        ${parcels ? `<table class="grid"><thead><tr><th>Parcel</th><th>Zoning · use</th><th>Flood (screening estimate)</th></tr></thead><tbody>${parcels}</tbody></table>` : '<p class="muted">No parcel found inside the boundary.</p>'}
      </section>
      ${buildConstraintsHtml(f.site_constraints)}
      <section class="card">
        <h3>Planning Commission</h3>
        ${hearings ? `<ul>${hearings}</ul>` : '<p class="muted">No hearing found yet for this record.</p>'}
      </section>
      ${f.pc_item_key ? buildCouncilHtml(f.council_date ? { stage: f.stage, council_date: f.council_date } : null, f.council_actions, "") : ""}
      <section class="card">
        <h3>Permits on these parcels since filing</h3>
        ${permits ? `<ul>${permits}</ul>` : '<p class="muted">None in the permit data.</p>'}
        <p class="muted small">Same-parcel permits are candidates: a permit on the site may belong to a different project.</p>
      </section>
      <p class="muted small">${esc(f.notice || "")}</p>`;
  }

  const CASE_STAGE_TEXT = { approved: "Approved (city record)", denied: "Denied", withdrawn: "Withdrawn",
    pending: "Pending", incomplete: "Incomplete", other: "Final action as printed" };

  const COUNCIL_CHECK_TEXT = {
    agrees: "The Council vote agrees with the city's case record.",
    disagrees: "The Council vote does not match the city's case record: listed for review (the city's record stands).",
    city_not_recorded: "The city's case record does not (yet) show this Council action.",
    continued: "The Council continued (deferred) this case; no deciding vote in the export.",
    no_decision: "No approve or deny vote on this case in the export.",
    undated: "Council votes found, but the meeting date is not recorded.",
  };

  // c: case detail. The Council vote card: each vote with its counted tally and each member's vote.
  function buildCouncilVotesHtml(c) {
    const cov = c.council_votes_coverage;
    if (!cov) return "";
    const sched = (c.council_scheduled || []).map((a) =>
      `<li>Scheduled for City Council ${esc(a.meeting_date)}${a.item_ref ? ` (item ${esc(a.item_ref)})` : ""}${a.agenda_url ? ` · ${escLinkify(a.agenda_url)}` : ""}</li>`).join("");
    const votes = (c.council_votes || []).map((v) => {
      const listed = (v.members || []).map((m) => `${esc(m.member)} ${esc(m.vote)}`).join(", ");
      const who = v.members_exceptions_only ? (listed ? `${listed}; every other member voted with the majority (the record lists exceptions only)` : "No exceptions recorded (the record lists only absent, no and abstain)") : listed;
      const tally = `${v.yes}–${v.no}${v.abstain ? `, ${v.abstain} abstain` : ""}${v.absent ? `, ${v.absent} absent` : ""}`;
      const t = v.testimony;
      const spoke = t ? `<br><span class="muted small">Public hearing speakers: ${esc(t.support)} support, ${esc(t.oppose)} oppose, ${esc(t.comment)} comment${t.present ? `, ${esc(t.present)} present` : ""} (names not shown)</span>` : "";
      return `<li><b>${esc(v.meeting_date || "date not recorded")}</b> · ${esc(v.motion || "motion")} · ${esc(v.result)} ${esc(tally)}${v.tally_mismatch ? ` <span class="muted small">(printed tally ${esc(v.printed_tally)} differs from the members' votes)</span>` : ""}<br><span class="muted small">${who}</span>${spoke}</li>`;
    }).join("");
    return `
      <section class="card">
        <h3>City Council votes</h3>
        ${sched ? `<ul>${sched}</ul>` : ""}
        ${votes ? `<ul>${votes}</ul>` : `<p class="muted">No Council vote on this case in the export (meetings ${esc(cov.date_min)} to ${esc(cov.date_max)}).</p>`}
        ${c.council_check ? `<p class="${c.council_check === "disagrees" ? "warn" : "muted"} small">${esc(COUNCIL_CHECK_TEXT[c.council_check] || c.council_check)}</p>` : ""}
        <p class="muted small">From a VoteIQ export of the city's meeting videos; the city's case record stays authoritative.</p>
      </section>`;
  }

  // c: GET /api/land-use/cases/{case_id} (a pipeline city's Development Tracking case). Escaped HTML.
  function buildCaseHtml(c) {
    const act = (a) => (a && (a.action || a.date)) ? `${esc(a.action || "–")}${a.date ? ` · ${esc(a.date)}` : ""}` : '<span class="muted">not recorded</span>';
    const parcels = (c.parcels || []).map((p) =>
      `<tr><td>${esc(p.gpin)}</td><td>${esc(p.prop_class || "–")}${p.acreage != null ? ` · ${esc(fmtNum(p.acreage))} ac` : ""}</td><td>${esc(FLOOD_SHORT[p.parcel_flood_class] || p.parcel_flood_class || "–")}</td></tr>`).join("");
    const recs = (c.development_records || []).map((r) =>
      `<li>${esc(r.record_id)} · ${esc(r.record_type || "")} · ${esc(r.status || "")}${r.submitted_date ? ` · submitted ${esc(r.submitted_date)}` : ""}${r.approved_date ? ` · approved ${esc(r.approved_date)}` : ""}</li>`).join("");
    const permits = (c.permits_on_site || []).map((p) =>
      `<li>${esc(p.source_permit_id)} · ${esc(p.permit_type || "")} · applied ${esc(p.applied_date)} <span class="muted small">${esc(p.status || "")}</span></li>`).join("");
    const zoning = c.zoning_from || c.zoning_to ? `${esc(c.zoning_from || "–")} → ${esc(c.zoning_to || "–")}` : '<span class="muted">not recorded</span>';
    return `
      <section class="card">
        <div class="card-head"><h2>${esc(c.case_type || "Land-use case")} · ${esc(c.case_id)}</h2>
          <span class="status-pill">${esc(CASE_STAGE_TEXT[c.final.stage] || c.final.stage)}</span></div>
        <p class="muted">${[c.address ? esc(c.address) : "", c.applied_date ? `applied ${esc(c.applied_date)}` : "", c.final.date ? `final action ${esc(c.final.date)}` : "", c.legacy ? "legacy record (2014 import)" : ""].filter(Boolean).join(" · ")}</p>
        <dl class="facts">
          <div><dt>Project</dt><dd>${party(c.project, "The project name")}</dd></div>
          <div><dt>Zoning</dt><dd>${zoning}</dd></div>
          <div><dt>Housing units (as published)</dt><dd>${c.housing_units != null ? esc(fmtNum(c.housing_units)) : '<span class="muted">not recorded</span>'}</dd></div>
          <div><dt>Staff recommendation</dt><dd>${esc(c.staff_recommendation || "–")}</dd></div>
          <div><dt>Planning Commission</dt><dd>${act(c.planning_commission)}</dd></div>
          <div><dt>City Council</dt><dd>${act(c.city_council)}</dd></div>
          <div><dt>Architectural review</dt><dd>${act(c.architectural_review)}</dd></div>
          <div><dt>Final (as printed)</dt><dd>${esc(c.final.as_printed || "–")}</dd></div>
          <div><dt>City record</dt><dd>${c.source_link ? escLinkify(c.source_link) : esc(c.case_id)}</dd></div>
        </dl>
        <div id="case-map" class="mini-map"></div>
      </section>
      <section class="card">
        <h3>Parcels</h3>
        ${parcels ? `<table class="grid"><thead><tr><th>Parcel</th><th>Class · area</th><th>Flood (screening estimate)</th></tr></thead><tbody>${parcels}</tbody></table>` : '<p class="muted">No parcel linked.</p>'}
      </section>
      ${buildCouncilVotesHtml(c)}
      ${buildConstraintsHtml(c.site_constraints)}
      <section class="card">
        <h3>Building permits on these parcels${c.permits_since ? ` since ${esc(c.permits_since)}` : ""}</h3>
        ${permits ? `<ul>${permits}</ul>` : `<p class="muted">${c.permits_observed ? "None in the permit data." : "No permit data loaded for this city."}</p>`}
        <p class="muted small">${c.permits_observed ? `Permit data: records from ${esc(c.permits_observed.from)} (last imported ${esc((c.permits_observed.to || "").slice(0, 10))}). ` : ""}Same-parcel permits are candidates: a permit on the site may belong to a different project.</p>
      </section>
      ${recs || c.locality === "chesapeake" ? `<section class="card">
        <h3>Development plans and right-of-way permits on these parcels</h3>
        ${recs ? `<ul>${recs}</ul>` : '<p class="muted">None published.</p>'}
      </section>` : ""}
      <p class="muted small">${esc(c.notice || "")}</p>`;
  }

  const api = { esc, escLinkify, buildReviewCsv, buildHistoryHtml, buildLeadFactsHtml, buildPcItemHtml, buildFilingHtml,
    buildCouncilHtml, buildConstraintsHtml, buildCaseHtml, buildCouncilVotesHtml };
  Object.assign(root, api);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
