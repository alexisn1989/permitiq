"""Read-only client for the Virginia Beach City Clerk's Laserfiche WebLink
archive (edocs.vbgov.com), the source of the Summary of Council Actions.

WebLink is a JavaScript app; its own JSON services list folders and return
each page's text layer (the summaries are scans with OCR text). A session
cookie from the welcome page is required first. Only the three calls below
are used, all reads.

  folder_listing(folder_id) -> entries (name, entry id, type, page count)
  page_text(doc_id, page)   -> the page's text layer, or "" when it has none
"""

from __future__ import annotations

import time

import requests

BASE = "https://edocs.vbgov.com/WebLink"
REPO = "CityClerk"
SUMMARY_ACTIONS_FOLDER = 840  # "SUMMARY ACTIONS": one sub-folder per year ("SUMMARIES 2024")
TIMEOUT = 60
PAGE_LIMIT = 500


class EdocsError(RuntimeError):
    pass


class Client:
    def __init__(self, session: requests.Session | None = None, retries: int = 3, pause: float = 0.3):
        self.s = session or requests.Session()
        self.retries, self.pause, self._ready = retries, pause, False

    def _post(self, service: str, body: dict) -> dict:
        if not self._ready:
            self.s.get(f"{BASE}/Welcome.aspx", timeout=TIMEOUT).raise_for_status()
            self._ready = True
        last = None
        for attempt in range(self.retries):
            try:
                r = self.s.post(f"{BASE}/{service}", json=body, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if self.pause:
                    time.sleep(self.pause)
                return data.get("data") if isinstance(data, dict) and "data" in data else data
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(2 ** attempt)
        raise EdocsError(f"{service}: {last}")

    def folder_listing(self, folder_id: int) -> list[dict]:
        """Every entry of a folder: {entry_id, name, type, page_count, created, modified}.
        Raises EdocsError when fewer entries arrive than the folder reports."""
        out, start, total = [], 0, None
        while total is None or start < total:
            d = self._post("FolderListingService.aspx/GetFolderListing2", {
                "repoName": REPO, "folderId": folder_id, "getNewListing": True, "start": start,
                "end": start + PAGE_LIMIT, "sortColumn": "", "sortAscending": True})
            total = int(d.get("totalEntries") or 0)
            rows = d.get("results") or []
            if not rows:
                break
            for e in rows:
                vals = e.get("data") or []
                out.append({"entry_id": int(e["entryId"]), "name": (e.get("name") or "").strip(),
                            "type": e.get("type"), "page_count": vals[1] if len(vals) > 1 else None,
                            "created": vals[10] if len(vals) > 10 else None,
                            "modified": vals[11] if len(vals) > 11 else None})
            start += len(rows)
        if len(out) != total:
            raise EdocsError(f"folder {folder_id}: {total} entries reported, {len(out)} received")
        return out

    def page_text(self, doc_id: int, page: int) -> str:
        d = self._post("DocumentService.aspx/GetTextHtmlForPage", {
            "repoName": REPO, "documentId": doc_id, "pageNum": page, "showAnn": False, "searchUuid": ""})
        t = d.get("text") if isinstance(d, dict) else None
        return t if isinstance(t, str) else ""


def doc_url(doc_id: int) -> str:
    """The public viewer link for a document (what users are shown)."""
    return f"{BASE}/DocView.aspx?id={doc_id}&dbid=0&repo={REPO}"


def year_folders(client: Client, years: list[int]) -> dict[int, int]:
    """Year -> folder id under SUMMARY ACTIONS, for the years present."""
    found = {}
    for e in client.folder_listing(SUMMARY_ACTIONS_FOLDER):
        parts = e["name"].split()
        if len(parts) == 2 and parts[0].upper() == "SUMMARIES" and parts[1].isdigit() and int(parts[1]) in years:
            found[int(parts[1])] = e["entry_id"]
    return found
