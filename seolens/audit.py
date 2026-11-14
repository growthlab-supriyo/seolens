"""Runs one audit from start to finish: crawl → checks → Site Health → scores → Excel report + Site Architecture Map."""
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from .advanced import run_advanced
from .checks import PostChecks
from .crux import add_crux
from .crawler import Crawler, Paused, Stopped  # noqa: F401 - re-exported
from .report import write_site_map
from .workbook import write_report
from .sitehealth import run_site_health
from .store import REPORTS

DEFAULT_SETTINGS = {
    "mode": "domain",            # "domain" (whole site or a folder) or "list"
    "site": "",
    "urls": [],
    "speed": "Medium",           # Polite | Medium | Fast  (see throttle.SPEEDS)
    "max_pages": 500,            # up to 10,000
    "follow_links": True,
    "include_query": False,
    "respect_robots": True,
    "check_external": True,
    "site_wide_health": False,   # folder audits: also run the whole-domain checks
    "site_type": "auto",
    "render_js": "off",          # off | auto = Selective (start page + pages that look empty) | all
    "render_max": 100,           # most pages rendered per audit (25-500)
    "content_min": 300,          # main-content review threshold in words (100-1,000), adjusted by page type
    "url_max": 115,              # URL length review threshold in characters (75-250)
    "out_dir": str(REPORTS),
}


def report_tz(s):
    """The time zone of the person who started the crawl (sent by their browser), so the report shows their local time."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    try:
        if s.get("tz"):
            return ZoneInfo(s["tz"])
    except Exception:  # noqa: BLE001 - unknown zone name
        pass
    off = s.get("tz_offset")
    return timezone(timedelta(minutes=-off)) if isinstance(off, (int, float)) else None


class _Step:
    """Logs '▶ name' when a stage starts and '✓ name (12 s)' when it ends, and shows it as the current step."""

    def __init__(self, report, phase, name, detail=""):
        self.report, self.phase, self.name, self.detail = report, phase, name, detail

    def __enter__(self):
        import time
        self.t = time.time()
        self.report({"phase": self.phase, "step": self.name, "log": "▶ " + self.name + (": " + self.detail if self.detail else "")})
        return self

    def __exit__(self, kind, *_):
        import time
        if kind is None:
            self.report({"log": "✓ %s (%.0f s)" % (self.name, time.time() - self.t)})


def run_audit(settings, report=lambda st: None, stop=None, store=None, run_id=None, resume=False):
    """Returns {"xlsx", "map", "overall", "pages"}; raises Paused / Stopped / ValueError."""
    s = dict(DEFAULT_SETTINGS, **settings)
    stop = stop or threading.Event()
    tz = report_tz(s)
    started = datetime.now(tz)
    report({"phase": "discovering", "started": started.timestamp(), "log": "Audit resumed" if resume else "Audit started"})
    crawler = Crawler(s, report, stop, store, run_id).run(resume=resume)
    if not crawler.rows:
        raise ValueError("No pages were found to audit.")
    if store:
        store.update_run(run_id, status="finishing")
    report({"log": "Crawl finished: %d pages audited. Now checking links, images and the whole site" % len(crawler.rows)})
    post = PostChecks(crawler, report, stop)
    with _Step(report, "checking", "Link targets and images"):
        post.run_requests()
    if stop.is_set():
        raise Stopped()
    with _Step(report, "site", "Site Health", "robots.txt, sitemap, HTTPS, www and the site-wide checks"):
        site = run_site_health(crawler, report)
    with _Step(report, "site", "Internal links", "link graph, click depth, orphan pages, Link Score, broken links"):
        post.analyze_links(site)
    with _Step(report, "site", "Cross-page checks", "canonical and hreflang targets, duplicates, near-duplicates"):
        post.cross_page()
    with _Step(report, "site", "Advanced checks"):
        post.advanced = run_advanced(crawler, post, site, report)
    crux_key = s.get("crux_key") or os.environ.get("SEOLENS_CRUX_KEY", "")
    if crux_key:
        with _Step(report, "checking", "Core Web Vitals (CrUX)"):
            add_crux(crawler, post, site, crux_key, report, stop)
    with _Step(report, "scoring", "Scoring pages", "duplicates, page scores, health score"):
        post.scores()
    finished = datetime.now(tz)
    out = Path(s["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^a-z0-9.-]+", "-", (crawler.host or "audit").lower().replace("www.", "")) + \
        ("-" + crawler.scope.strip("/").replace("/", "-") if crawler.scope else "") + \
        ("-list" if s["mode"] == "list" else "") + finished.strftime("_%Y-%m-%d_%H%M") + ("_run%d" % run_id if run_id else "")
    xlsx = str(out / (name + ".xlsx"))
    with _Step(report, "summary", "Writing the Excel report", "11 sheets + raw data"):
        overall = write_report(xlsx, crawler, post, site, started, finished, None)
    map_path = str(out / (name + " - Site Architecture Map.html"))
    with _Step(report, "summary", "Drawing the site architecture map"):
        if not write_site_map(map_path, crawler, post):
            map_path = None
    if store:
        # The final rows (scores, duplicates, click depth, Link Score) go back into the database for the dashboard.
        with _Step(report, "summary", "Saving the results for the dashboard"):
            store.save_pages_rows(run_id, list(zip(crawler.page_idx, crawler.rows)))
            from .snapshot import capture
            store.save_snapshot(run_id, capture(crawler, post, site, started, finished))
    checks = site["checks"] + post.checks
    result = {"xlsx": xlsx, "map": map_path, "overall": overall, "pages": len(crawler.rows),
              "checks": [c for c in checks if c["status"] in ("Fail", "Warn")],
              "broken": [list(b) for b in post.broken_rows[:5000]], "advanced": post.advanced.get("counts", {})}
    report({"phase": "done", "finished": finished.timestamp(), "xlsx": xlsx, "map": map_path, "overall": overall,
            "log": "Audit complete: %d pages · Overall Health Score %d/100" % (len(crawler.rows), overall)})
    return result
