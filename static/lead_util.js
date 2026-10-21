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

  const api = { esc, escLinkify, buildReviewCsv, buildHistoryHtml };
  Object.assign(root, api);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
