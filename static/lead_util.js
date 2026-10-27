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

  const api = { esc, escLinkify, buildReviewCsv, buildHistoryHtml, buildLeadFactsHtml };
  Object.assign(root, api);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
