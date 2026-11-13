"""Normalises one audit into the data every report sheet uses: issue records (one per URL per rule), tasks (one per fix),
link edges, coverage figures and audit notices. Built once, so all 11 sheets agree with each other.

Counts are kept apart on purpose:
    issue records  = one per (URL, rule)          → severity doughnut, Issue Details
    affected URLs  = unique pages per rule         → Issue Distribution bars, Action Plan
    site findings  = site-wide checks (no URL)     → shown separately, never mixed into per-page counts
"""
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import __version__
from .checks import MAX_EXTERNAL_CHECKS, MAX_IMAGE_CHECKS, MAX_INTERNAL_CHECKS, PRIORITY_RANK
from .htmltools import norm, parse_url
from .report import overall_score
from .workbook import _count_prefix, _detail, _split, _urls_in, build_issues

SEVERITIES = ["Critical", "High", "Medium", "Low"]

# Task priority (what to do first) is separate from issue severity (how bad one finding is). Rules:
PRIORITY_RULES = [
    ("P0", "Critical severity: pages fail to load or can't be indexed. Fix now."),
    ("P1", "High severity, or a Medium issue on a shared template (one fix repairs most pages)."),
    ("P2", "Other Medium severity issues."),
    ("P3", "Low severity: polish."),
]


def task_priority(severity, template):
    if severity == "Critical":
        return "P0"
    if severity == "High" or (severity == "Medium" and template):
        return "P1"
    return "P2" if severity == "Medium" else "P3"


# What a page should look like for each rule (the 'Expected result' column).
EXPECTED = {
    "Missing meta title": "A unique <title> of 30–60 characters",
    "Title length out of range": "A title of 30–60 characters",
    "Multiple title tags": "One <title> per page",
    "Missing meta description": "A unique meta description of 70–160 characters",
    "Description length out of range": "A meta description of 70–160 characters",
    "Missing H1": "One H1 that states the page topic",
    "Multiple H1 tags": "One H1 per page",
    "Duplicate meta title": "A title used by this page only",
    "Duplicate meta description": "A meta description used by this page only",
    "Duplicate H1": "An H1 used by this page only",
    "Title and H1 don't match": "Title and H1 describe the same topic",
    "Thin content": "Main content at or above the review threshold for this page type",
    "Duplicate content": "Main content not copied from another page",
    "Near-duplicate content": "Main content clearly different from other pages",
    "Placeholder text found": "No placeholder or sample text",
    "Missing canonical": "A self-referencing canonical tag",
    "Canonical points to another URL": "Canonical points to this page (unless it's a deliberate duplicate)",
    "Canonical points to a broken page": "Canonical points to a page that returns 200",
    "Canonical points to a redirect": "Canonical points straight to the final URL",
    "Canonical points to a noindex page": "Canonical points to an indexable page",
    "Multiple canonical tags": "One canonical tag",
    "Page is noindex": "Indexable, if the page should rank",
    "Server error (5xx)": "HTTP 200",
    "Intermittent server error (5xx)": "HTTP 200 on every request, also under load",
    "Page not found (404/410)": "HTTP 200, or a 301 to the closest live page",
    "Client error (4xx)": "HTTP 200 for public pages; no internal links to private ones",
    "Page fetch failed": "The page answers within 20 seconds",
    "Page not returning 200": "HTTP 200",
    "URL redirects": "Internal links point straight to the final URL",
    "Redirect chain (2+ hops)": "At most one redirect",
    "Broken links on page": "Every link on the page works",
    "Orphan page": "Linked from at least one other page",
    "Deep page (4+ clicks)": "Within 3 clicks of the homepage",
    "Images missing alt text": "Descriptive alt text on meaningful images",
    "Heavy images": "Images under 200 KB",
    "No structured data": "Relevant schema.org markup (JSON-LD)",
    "Structured data has errors": "JSON-LD that parses without errors",
    "Structured data missing key properties": "The recommended properties of the schema type",
    "Incomplete Open Graph tags": "og:title, og:description, og:image and og:url",
    "Missing viewport meta tag": "A viewport meta tag (width=device-width, initial-scale=1)",
    "Missing html lang attribute": "<html lang> set to the page language",
    "Hreflang errors": "Valid hreflang codes with return links",
    "Render-blocking scripts in head": "Scripts in <head> loaded with defer or async",
    "Poor Core Web Vitals": "LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (real users)",
    "Core Web Vitals need improvement": "LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (real users)",
    "Internal links using http://": "Internal links use https://",
    "Generic anchor text": "Link text that says where the link goes",
    "Links without anchor text": "Every link has text, an image alt or an aria-label",
    "Tracking parameters in internal links": "Internal links point to clean URLs (no utm_ or click IDs)",
    "Parameter URL duplicates another page": "Parameter variants point their canonical to the clean URL",
    "Canonical to a different page": "Canonical points to a page with the same content",
    "Paginated page canonicalised to page 1": "Each page of a series has a self-referencing canonical",
    "Noindex page with a canonical to another URL": "Either noindex or a canonical to another URL, not both",
    "URL format issues": "Short, lowercase, hyphenated URLs (review only: don't change established URLs for looks)",
    "Bot-check page instead of content": "The real page content, not a firewall check",
}
TECHNICAL_THEMES = {"Crawlability & indexing", "International", "Performance"}


def stable_id(prefix, *parts, n=8):
    """Deterministic ID: the same URL and rule give the same ID in every run."""
    return "%s-%s" % (prefix, hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:n].upper())


@dataclass
class Issue:
    issue_id: str
    rule: str
    severity: str
    scope: str           # "Page" or "Site"
    url: str             # "" for site-wide findings
    evidence: str
    expected: str
    fix: str
    task_id: str
    category: str
    row: int = -1        # index into rows (pages only)
    verification: str = "Not checked"


@dataclass
class Task:
    task_id: str
    priority: str
    severity: str
    rule: str
    fix_scope: str
    affected: int        # unique URLs (0 for site-wide findings)
    fix: str
    effort: str
    category: str
    template: bool
    issue_ids: list = field(default_factory=list)
    owner: str = ""
    status: str = "To Do"
    verification: str = "Not checked"


@dataclass
class Model:
    host: str
    run_id: str
    mode: str            # "domain" | "list"
    scope: str           # folder path or ""
    audit_type: str
    started: object
    finished: object
    settings: dict
    rows: list
    extras: list
    queue: list
    access: dict         # queue index -> crawl access
    page_idx: list
    issues: list
    tasks: list
    site_checks: list    # every site check (Pass/Info too)
    broken_rows: list
    score: int
    score_avg: int
    score_penalty: int
    link_graph: bool
    eligible: int        # HTML pages that returned 200 (denominator for percentages)
    sitemap_keys: set
    has_sitemap: bool
    coverage: list
    notices: list
    crawler: object = None
    post: object = None


def page_type(row):
    """From what the crawl detected; 'Unclassified' otherwise, never guessed."""
    p = parse_url(row.get("Final URL") or row["URL"]) or {}
    if (p.get("path") or "/") == "/" and not p.get("query"):
        return "Homepage"
    if str(row.get("Meta Title", "")).startswith("N/A"):
        return "Not an HTML page"
    arch = str(row.get("Archive Page", "") or "")
    if arch and arch != "No" and not arch.startswith("N/A"):
        return arch.split(" · ")[0].strip() or "Archive"
    return "Unclassified"


def build_model(crawler, post, site, started, finished, ai_out=None):
    rows, s = crawler.rows, crawler.s
    checks = site["checks"] + post.checks
    avg, penalty, overall = overall_score(rows, checks)
    link_graph = s["mode"] == "domain" and s.get("follow_links", True)
    eligible = sum(1 for r in rows if r.get("Final Status") == 200 and not str(r.get("Meta Title", "")).startswith("N/A"))
    groups = defaultdict(list)  # duplicate title / description / H1 details
    for r in rows:
        for col in ("Meta Title", "Meta Description", "H1 Text"):
            v = str(r.get(col, "")).strip().lower()
            if v and v not in ("missing", "n/a (non-html)"):
                groups[(col, v)].append(r["URL"])
    for b in post.broken_rows:  # pages linking to each error URL (error pages have no inlink count of their own)
        key = ("links_to", norm(b[2]))
        groups[key] = (groups.get(key) or 0) + (b[1] if isinstance(b[1], int) else 1)
    row_of = {id(r): i for i, r in enumerate(rows)}

    issues, tasks = [], []
    for g in build_issues(rows, checks, site["site_wide"]):
        rule, sev = g["name"], g["priority"] if g["priority"] in SEVERITIES else "Medium"
        if g["rows"]:
            scope_label = ("Template (shared by %d%% of pages)" % round(100 * g["share"])) if g["template"] else \
                "Each affected page (%d)" % g["pages"]
            task = Task(stable_id("T", "page", rule, n=6), task_priority(sev, g["template"]), sev, rule, scope_label, g["pages"],
                        g["fix"], g["effort"], g["category"], g["template"])
            for r in sorted(g["rows"], key=lambda r: r["URL"]):
                iid = stable_id("I", norm(r["URL"]), rule)
                issues.append(Issue(iid, rule, sev, "Page", r["URL"], _detail(rule, r, groups) or _evidence_fallback(rule, r),
                                    EXPECTED.get(rule, "No '%s' finding" % rule), g["fix"], task.task_id, g["category"], row_of[id(r)]))
                task.issue_ids.append(iid)
        else:
            task = Task(stable_id("T", "site", rule, n=6), task_priority(sev, False), sev, rule, "Site-wide setting", 0, g["fix"],
                        "Medium", g["category"], False)
            iid = stable_id("I", "site", rule)
            issues.append(Issue(iid, rule, sev, "Site", "", g["detail"], EXPECTED.get(rule, "Check passes"), g["fix"], task.task_id,
                                g["category"]))
            task.issue_ids.append(iid)
        tasks.append(task)
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    tasks.sort(key=lambda t: (order[t.priority], PRIORITY_RANK.get(t.severity, 2), -t.affected, t.rule))

    coverage, notices = _coverage(crawler, post, s, rows, link_graph)
    host = site.get("host") or crawler.host or ""
    if s["mode"] == "list":
        audit_type = "List of %d URLs (links are not followed)" % len(s.get("urls") or rows)
    elif crawler.scope:
        audit_type = "Folder %s/%s" % (crawler.scope, " (with the site-wide checks)" if site["site_wide"] else "")
    else:
        audit_type = "Whole website"
    return Model(host=host, run_id=str(getattr(crawler, "run_id", None) or "local"), mode=s["mode"], scope=crawler.scope,
                 audit_type=audit_type, started=started, finished=finished, settings=s, rows=rows, extras=crawler.extras,
                 queue=crawler.queue, access=dict(zip(getattr(crawler, "page_idx", range(len(rows))), getattr(crawler, "access", ["OK"] * len(rows)))),
                 page_idx=list(getattr(crawler, "page_idx", range(len(rows)))), issues=issues, tasks=tasks, site_checks=checks,
                 broken_rows=list(post.broken_rows), score=overall, score_avg=avg, score_penalty=penalty, link_graph=link_graph,
                 eligible=eligible, sitemap_keys=site.get("sitemap_keys") or set(), has_sitemap=bool(site.get("sitemap")),
                 coverage=coverage, notices=notices, crawler=crawler, post=post)


def _evidence_fallback(rule, r):
    return "Status %s" % r.get("Final Status", "") if "status" in rule.lower() else ""


def _coverage(crawler, post, s, rows, link_graph):
    """Rows for the Crawl Coverage sheet, and the factual notices for the dashboard."""
    queue, d = crawler.queue, crawler.discovery
    access = dict(zip(getattr(crawler, "page_idx", range(len(rows))), getattr(crawler, "access", ["OK"] * len(rows))))
    audited_idx = set(access)
    max_pages = s.get("max_pages") or len(queue)
    by_source, audited_by_source = Counter(), Counter()
    for i, (u, src) in enumerate(queue):
        by_source[src] += 1
        if i in audited_idx:
            audited_by_source[src] += 1
    over_limit = max(0, len(queue) - max_pages)
    not_reached = max(0, min(len(queue), max_pages) - len(audited_idx))
    refused = sum(1 for a in access.values() if a != "OK")
    pct = lambda a, b: (a / b) if b else None  # noqa: E731
    cov = [["All sources (unique URLs)", len(queue), len(audited_idx), len(queue) - len(audited_idx), pct(len(audited_idx), len(queue)),
            "; ".join(x for x in ("%d over the %d-URL limit" % (over_limit, max_pages) if over_limit else "",
                                  "%d not reached (crawl ended early)" % not_reached if not_reached else "") if x) or "Every discovered URL was audited",
            "Raise the URL limit or crawl again" if over_limit or not_reached else ""]]
    for src in ("Your list", "Homepage", "Start URL", "Sitemap", "Crawl"):
        if by_source[src]:
            label = {"Crawl": "Links on pages", "Sitemap": "XML sitemap"}.get(src, src)
            cov.append([label, by_source[src], audited_by_source[src], by_source[src] - audited_by_source[src],
                        pct(audited_by_source[src], by_source[src]), "Counted under the source that found each URL first", ""])
    if d.get("blocked"):
        cov.append(["Blocked by robots.txt", d["blocked"], 0, d["blocked"], 0.0, "robots.txt disallows them; SEO Lens respects it",
                    "Allow them in robots.txt if they should be crawled"])
    if refused:
        cov.append(["Refused by the site", refused, refused, 0, None, "Firewall, CAPTCHA, rate limit or timeout: audited without content",
                    "See the Blocked & failed list in the app; allow the SEOLens user-agent for your own site"])
    skipped = getattr(post, "skipped", {}) or {}
    res = getattr(post, "res", {}) or {}
    checked = Counter(k.split("|", 1)[0] for k in res)
    cov.append(["Internal link targets not crawled", checked["int"] + skipped.get("int", 0), checked["int"], skipped.get("int", 0),
                pct(checked["int"], checked["int"] + skipped.get("int", 0)),
                "Status checked for up to %d of the most-linked targets" % MAX_INTERNAL_CHECKS if skipped.get("int") else "All checked", ""])
    if skipped.get("ext_off"):
        cov.append(["External links", None, 0, None, None, "Not measured: external link checks were turned off", "Turn on 'Check external links'"])
    else:
        cov.append(["External links", checked["ext"] + skipped.get("ext", 0), checked["ext"], skipped.get("ext", 0),
                    pct(checked["ext"], checked["ext"] + skipped.get("ext", 0)),
                    "Up to %d of the most-linked external URLs are checked" % MAX_EXTERNAL_CHECKS if skipped.get("ext") else "All checked", ""])
    cov.append(["Image file sizes", checked["img"] + skipped.get("img", 0), checked["img"], skipped.get("img", 0),
                pct(checked["img"], checked["img"] + skipped.get("img", 0)),
                "Up to %d of the most-used images are measured" % MAX_IMAGE_CHECKS if skipped.get("img") else "All measured", ""])
    cwv = sum(1 for r in rows if r.get("Core Web Vitals"))
    cwv_data = sum(1 for r in rows if str(r.get("Core Web Vitals", "")) in ("Good", "Needs improvement", "Poor"))
    cov.append(["Core Web Vitals (CrUX)", cwv or None, cwv_data if cwv else None, (cwv - cwv_data) if cwv else None,
                pct(cwv_data, cwv) if cwv else None,
                "Real-user data from the Chrome UX Report" if cwv else "Not measured: no CrUX API key was given",
                "" if cwv else "Add a free CrUX API key under More settings"])
    rendered = getattr(crawler, "rendered", 0)
    if s.get("render_js", "off") != "off":
        st = Counter(("Measured" if v.startswith("Measured") else "Failed" if v.startswith("Failed") else
                      "Limit" if "limit" in v else "Skipped" if v.startswith("Not measured") else "n/a")
                     for v in (str(r.get("Rendering Check", "")) for r in rows) if v)
        eligible = st["Measured"] + st["Failed"] + st["Limit"] + st["Skipped"]
        cov.append(["JavaScript rendering", eligible, st["Measured"], eligible - st["Measured"], pct(st["Measured"], eligible),
                    "; ".join(x for x in ("%d rendered" % st["Measured"],
                                          "%d not selected (the HTML looked complete)" % st["Skipped"] if st["Skipped"] else "",
                                          "%d over the limit of %s rendered pages" % (st["Limit"], s.get("render_max", 100)) if st["Limit"] else "",
                                          "%d failed" % st["Failed"] if st["Failed"] else "") if x) +
                    ("" if getattr(crawler, "renderer", None) or rendered else " · " + str(getattr(crawler, "render_note", ""))),
                    "Raise the render limit or choose All under More settings" if st["Limit"] else ""])
    else:
        cov.append(["JavaScript rendering", None, None, None, None, "Not measured: raw HTML only (rendering off)", ""])
    if not link_graph:
        cov.append(["Click depth, inlinks, orphan pages", None, None, None, None, "Not measured: a URL-list audit doesn't follow links",
                    "Audit the whole site to measure them"])

    notices = []
    if over_limit:
        notices.append("%d discovered URLs were not audited: over the %d-URL limit." % (over_limit, max_pages))
    if not_reached:
        notices.append("The crawl ended early: %d discovered URLs were not reached." % not_reached)
    if refused:
        notices.append("%d page(s) were refused by the site (firewall, rate limit or timeout) and have no content checks." % refused)
    if d.get("blocked"):
        notices.append("%d URL(s) blocked by robots.txt were not crawled." % d["blocked"])
    if skipped.get("ext_off"):
        notices.append("External links were not checked (turned off).")
    elif skipped.get("ext"):
        notices.append("%d external URL(s) were not checked (limit %d)." % (skipped["ext"], MAX_EXTERNAL_CHECKS))
    if skipped.get("int"):
        notices.append("%d internal link target(s) were not checked (limit %d)." % (skipped["int"], MAX_INTERNAL_CHECKS))
    if skipped.get("img"):
        notices.append("%d image(s) were not measured for file size (limit %d)." % (skipped["img"], MAX_IMAGE_CHECKS))
    if not cwv:
        notices.append("Core Web Vitals not measured (no CrUX API key).")
    if s.get("render_js", "off") == "off":
        notices.append("JavaScript rendering was off: content added by JavaScript was not seen.")
    if not link_graph:
        notices.append("URL-list audit: click depth, inlinks and orphan pages are not measured.")
    return cov, notices


def evidence_urls(value):
    return _urls_in(value)


def inlinks(row):
    return _count_prefix(row.get("Linked From (pages)"))


def page_issues(row):
    return _split(row.get("Issues"))


def version():
    return __version__
