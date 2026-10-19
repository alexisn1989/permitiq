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

  const api = { esc, escLinkify, buildReviewCsv };
  Object.assign(root, api);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
