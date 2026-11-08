"""The Excel audit report: a one-screen Summary, an Action Plan, one row per page per issue, then the detail sheets.

Sheet order (what a reader needs first comes first):
    Summary · Action Plan · Issue URLs · Pages · Response Codes · Links · Images · Internal Linking · Site Health ·
    Sitemap & Discovery · About This Audit · All Data
"""
import re
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from . import __version__
from .analyze import _RULES
from .checks import PRIORITY_RANK, fix_text, priority
from .htmltools import norm
from .report import AUTHOR_URL, COLUMNS, _colour_pages, _internal_linking, _v, overall_score

# ---------------------------------------------------------------- look

INK, MUTED, LINE = "1F2933", "616E7C", "D9DEE4"
NAVY = "1F3864"
FILL = {k: PatternFill("solid", fgColor=v) for k, v in {
    "navy": NAVY, "panel": "F4F6F8", "Critical": "F4CCCC", "High": "FCE5CD", "Medium": "FFF2CC", "Low": "E8F0FE",
    "good": "D9EAD3", "warn": "FFF2CC", "bad": "F4CCCC", "grey": "EEEEEE"}.items()}
THIN = Side(style="thin", color=LINE)
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
LEVEL = {"Critical": "Error", "High": "Error", "Medium": "Warning", "Low": "Notice"}

# ---------------------------------------------------------------- issue catalogue

# Site-wide checks that describe the same problem as a page-level issue: one row in the Action Plan, not two.
MERGE = {"Broken internal links": "Broken links on page", "Orphan pages": "Orphan page", "Heavy images": "Heavy images",
         "Near-duplicate pages": "Near-duplicate content"}
SPLIT_FIX = {
    "Server error (5xx)": "These URLs return a 5xx server error. Check the server logs and fix the page code or hosting; "
                          "if a page is gone for good, return 404/410 or 301-redirect it, then update the links to it.",
    "Page not found (4xx)": "Restore these pages or 301-redirect them to the closest live page, then update the internal links "
                            "and the sitemap.",
}
OWNER_RULES = [
    ("Content", ["meta title", "Title length", "meta description", "Description length", "H1", "Title and H1",
                 "Title year", "Thin content", "Duplicate content", "Near-duplicate", "Placeholder", "alt text", "Generic anchor",
                 "date", "Date", "Author"]),
    ("SEO", ["Orphan", "Deep page", "Broken links", "Broken external links", "Internal links to redirects", "No structured data", "missing key properties",
             "Open Graph", "og:", "Thin archive", "Missing from sitemap", "missing from sitemap"]),
]
DETAIL_COLUMNS = [
    (r"5xx|4xx|fetch failed|not returning 200", "Final Status"), (r"redirect", "Redirect Chain"), (r"[Cc]anonical", "Canonical URL"),
    (r"noindex", "Meta Robots"), (r"meta title|Title length|Multiple title", "Meta Title"), (r"Duplicate meta title", "Duplicate Title"),
    (r"meta description|Description length", "Meta Description"), (r"Duplicate meta description", "Duplicate Description"),
    (r"Duplicate H1", "Duplicate H1"), (r"H1", "H1 Text"), (r"Thin content", "Word Count"),
    (r"Near-duplicate", "Near Duplicate"), (r"Duplicate content", "Duplicate Content"), (r"alt text", "Images Missing Alt (URLs)"),
    (r"width/height", "Images Missing Width/Height (URLs)"), (r"Heavy images", "Heavy Images (200 KB+)"),
    (r"Broken links", "Broken Links (on page)"), (r"Orphan", "Orphan Page"), (r"Deep page", "Click Depth"),
    (r"Structured data has errors", "Schema Parse Errors"), (r"missing key properties", "Schema Missing Properties"),
    (r"structured data", "Schema Types"), (r"Open Graph", "Open Graph"), (r"[Hh]reflang", "Hreflang Issues"),
    (r"html lang|og:locale", "HTML Lang"), (r"Render-blocking", "Render-Blocking Scripts"), (r"performance", "Performance Risk"),
    (r"analytics|Analytics", "Tracking Detected"), (r"JavaScript", "JS-Rendering Risk"), (r"[Dd]ate", "Date Check"),
    (r"Placeholder", "Placeholder Text"), (r"URL format", "URL Issues"),
]


def owner_of(name):
    for owner, keys in OWNER_RULES:
        if any(k in name for k in keys):
            return owner
    return "Developer"


def _split(v):
    return [i.strip() for i in str(v or "").split(";") if i.strip()]


def _clean(result):
    return re.sub(r":\s+https?://[\s\S]*$", " (URLs in the Site Health sheet)", str(result))


CATEGORY_FIX = {"Server error (5xx)": "Crawlability & indexing", "Page not found (4xx)": "Crawlability & indexing",
                "Page fetch failed": "Crawlability & indexing", "Images missing alt text": "On-page SEO"}


MAX_LINKS = 10
# Image issues in the order their URLs are listed: an image already listed for a page under an earlier one isn't repeated.
IMAGE_ISSUES = [("Heavy images", "Heavy Images (200 KB+)"), ("Images missing alt text", "Images Missing Alt (URLs)")]
# Only measured when links are followed across the site: left out of URL-list audits instead of showing N/A.
LINK_GRAPH_COLUMNS = ["Linked From (pages)", "Orphan Page", "Click Depth", "Link Score (0-100)"]


def _blank(v):
    return v in (None, "") or (isinstance(v, str) and (not v.strip() or v.startswith("N/A")))


def _na(v):
    """No 'N/A' anywhere: 'N/A – not an HTML page (pdf)' becomes 'Not an HTML page (pdf)'; a bare N/A becomes empty."""
    if isinstance(v, str) and v.startswith("N/A"):
        rest = re.sub(r"^N/A\s*[–\-:]+\s*", "", v) if re.match(r"^N/A\s*[–\-:]", v) else ""
        return rest[:1].upper() + rest[1:] if rest else ""
    return v


def _useful_columns(columns, rows, link_graph=True):
    """The columns worth showing: not hidden, not link-graph columns when links weren't followed, not empty or all N/A."""
    from .report import HIDDEN_COLUMNS
    out = []
    for c in columns:
        if c in HIDDEN_COLUMNS or (not link_graph and c in LINK_GRAPH_COLUMNS):
            continue
        if c != "URL" and all(_blank(r.get(c)) for r in rows):
            continue
        out.append(c)
    return out


def _urls_in(value):
    """Image URLs in a list cell ('u1, u2' or '10: u1 (1.4 MB), u2 (300 KB)')."""
    return [u.rstrip(",") for u in re.findall(r"https?://[^\s,]+", str(value or ""))]


def _url_list(urls, total=None, more_where=""):
    total = len(urls) if total is None else total
    shown = urls[:MAX_LINKS]
    rest = total - len(shown)
    return ", ".join(shown) + (" (+%d more%s)" % (rest, more_where) if rest > 0 else "")


def _image_detail(name, row):
    seen = []
    for issue, col in IMAGE_ISSUES:
        urls = _urls_in(row.get(col))
        if issue == name:
            new = [u for u in urls if u not in seen]
            repeat = len(urls) - len(new)
            earlier = [i for i, c in IMAGE_ISSUES[:IMAGE_ISSUES.index((issue, col))] if set(_urls_in(row.get(c))) & set(urls)]
            out = "%d image%s" % (len(urls), "" if len(urls) == 1 else "s")
            if new:
                out += ": " + _url_list(new, more_where=" in the Images sheet")
            if repeat:
                out += "%s%d already listed under %s" % ("; " if new else ": ", repeat, " / ".join(earlier))
            return out
        seen += urls
    return ""


def _same_value_detail(name, row, groups):
    """Duplicate title / description / H1: the other pages that share it (up to 10)."""
    col = {"Duplicate meta title": "Meta Title", "Duplicate meta description": "Meta Description", "Duplicate H1": "H1 Text"}[name]
    key = str(row.get(col, "")).strip().lower()
    others = [u for u in groups.get((col, key), []) if u != row["URL"]]
    if not others:
        return ""
    return '"%s" is also on %d page%s: %s' % (str(row.get(col, ""))[:80], len(others), "" if len(others) == 1 else "s",
                                              _url_list(others))


def _detail(name, row, groups=None):
    if name in SPLIT_FIX:
        n = _count_prefix(row.get("Linked From (pages)"))
        return "Linked from %d page%s" % (n, "" if n == 1 else "s") if n else "Not linked from crawled pages (found in the sitemap)"
    if name == "Intermittent server error (5xx)":
        return str(row.get("Server Errors", ""))
    if name == "Broken links on page":
        urls = row.get("_broken_urls") or []
        return "%d broken link%s: %s" % (len(urls), "" if len(urls) == 1 else "s", _url_list(urls, more_where=" in the Links sheet")) if urls else \
            str(row.get("Broken Links (on page)", ""))
    if name == "Thin content":
        return "%s words of main content (under 300 counts as thin)" % row.get("Word Count", "")
    if name in ("Duplicate meta title", "Duplicate meta description", "Duplicate H1"):
        return _same_value_detail(name, row, groups or {})
    if name in dict(IMAGE_ISSUES):
        return _image_detail(name, row)
    for rx, col in DETAIL_COLUMNS:
        if re.search(rx, name):
            v = row.get(col, "")
            if col.endswith("(URLs)") and v:
                n = len([u for u in str(v).split(", ") if u.strip()])
                return "%d image%s: %s" % (n, "" if n == 1 else "s", str(v)[:160])
            if col == "Meta Title" and "length" in name:
                return "%s (%s characters)" % (str(v)[:120], row.get("Title Length", ""))
            if col == "Meta Description" and "length" in name:
                return "%s (%s characters)" % (str(v)[:120], row.get("Description Length", ""))
            return str(v)[:200]
    return ""


def build_issues(rows, checks, site_wide=True):
    """[{name, priority, category, owner, effort, pages, share, scope, fix, detail, rows}] sorted for action."""
    from .overview import theme_of
    n = max(1, len(rows))
    html = sum(1 for r in rows if r.get("Final Status") == 200 and not str(r.get("Meta Title", "")).startswith("N/A"))
    per = defaultdict(list)
    for r in rows:
        for name in _split(r.get("Issues")):
            if name == "Page not returning 200":
                st = r.get("Final Status")
                name = ("Server error (5xx)" if isinstance(st, int) and st >= 500 else
                        "Page not found (4xx)" if isinstance(st, int) and 400 <= st < 500 else name)
            per[name].append(r)
    issues, by_name = [], {}
    for name, lst in per.items():
        prio = "Critical" if name in SPLIT_FIX else priority(name)
        template = len(lst) >= 20 and len(lst) >= 0.8 * html
        owner = owner_of(name)
        effort = "Low" if template or len(lst) <= 10 else "High" if owner == "Content" and len(lst) > 50 else "Medium"
        it = {"name": name, "priority": prio, "category": CATEGORY_FIX.get(name) or theme_of(name), "owner": owner, "effort": effort,
              "pages": len(lst), "share": len(lst) / n, "template": template,
              "scope": "Site-wide (template fix)" if template else "%d page%s" % (len(lst), "" if len(lst) == 1 else "s"),
              "fix": SPLIT_FIX.get(name) or fix_text(name), "detail": "", "rows": lst}
        issues.append(it)
        by_name[name] = it
    for c in checks:
        if c["status"] not in ("Fail", "Warn"):
            continue
        merged = by_name.get(MERGE.get(c["check"], ""))
        if merged:
            merged["detail"] = _clean(c["result"])
            continue
        issues.append({"name": c["check"], "priority": c["priority"] or "Medium", "category": c["category"],
                       "owner": owner_of(c["check"]), "effort": "Medium", "pages": None, "share": None, "template": False,
                       "scope": "Whole site" if site_wide else "These pages", "fix": c["fix"], "detail": _clean(c["result"]), "rows": []})
    issues.sort(key=lambda x: (PRIORITY_RANK.get(x["priority"], 2), -(x["pages"] if x["pages"] is not None else n + 1)))
    return issues


# ---------------------------------------------------------------- helpers

def _table(ws, top_row, headers, data, name, widths=None, wrap_cols=(), number_formats=None):
    """Writes headers + rows as a styled Excel table (filter buttons, banded rows). Returns the first data row."""
    for c, h in enumerate(headers, 1):
        ws.cell(row=top_row, column=c, value=h)
    for i, row in enumerate(data, top_row + 1):
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=i, column=c, value=_v(v))
            cell.alignment = WRAP if c in wrap_cols else TOP
    last = top_row + max(1, len(data))
    if not data:
        ws.cell(row=top_row + 1, column=1, value="Nothing to list.")
    ref = "A%d:%s%d" % (top_row, get_column_letter(len(headers)), last)
    t = Table(displayName=name, ref=ref)
    t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(t)
    for c, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    for col, fmt in (number_formats or {}).items():
        for i in range(top_row + 1, last + 1):
            ws.cell(row=i, column=col).number_format = fmt
    return top_row + 1


def _title(ws, text, sub=None):
    ws.cell(row=1, column=1, value=text).font = Font(size=16, bold=True, color=INK)
    if sub:
        ws.cell(row=2, column=1, value=sub).font = Font(size=10, color=MUTED)
    ws.sheet_view.showGridLines = False


def _paint_priority(ws, first_row, count, col):
    for i in range(first_row, first_row + count):
        cell = ws.cell(row=i, column=col)
        if cell.value in FILL:
            cell.fill = FILL[cell.value]


def _count_prefix(v):
    m = re.match(r"^\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- the workbook

def write_report(path, crawler, post, site, started, finished, ai_out=None):
    rows, s = crawler.rows, crawler.s
    checks = site["checks"] + post.checks
    avg, penalty, overall = overall_score(rows, checks)
    issues = build_issues(rows, checks, site["site_wide"])
    # Click depth, orphans, Link Score and the Internal Linking sheet exist only when links were followed across the site.
    link_graph = s["mode"] == "domain" and s.get("follow_links", True)
    wb = Workbook()
    wb.remove(wb.active)
    sheets = {name: wb.create_sheet(name) for name in (
        "Summary", "Action Plan", "Issue URLs", "Pages", "Response Codes", "Links", "Images", "Internal Linking",
        "Site Health", "Sitemap & Discovery", "About This Audit", "All Data")}

    first_url_row = _issue_urls(sheets["Issue URLs"], issues, rows)
    _action_plan(sheets["Action Plan"], issues, first_url_row)
    keep = {"Summary", "Action Plan", "Site Health", "About This Audit", "All Data", "Pages", "Sitemap & Discovery"}
    if first_url_row:
        keep.add("Issue URLs")
    _pages(sheets["Pages"], rows, link_graph)
    if _response_codes(sheets["Response Codes"], rows):
        keep.add("Response Codes")
    if _links(sheets["Links"], post):
        keep.add("Links")
    if _images(sheets["Images"], crawler, post):
        keep.add("Images")
    if link_graph:
        _internal_linking(sheets["Internal Linking"], crawler, post)
        keep.add("Internal Linking")
    _site_health(sheets["Site Health"], checks)
    _discovery(sheets["Sitemap & Discovery"], crawler, rows, site)
    _about(sheets["About This Audit"], crawler, site, started, finished, link_graph)
    _all_data(sheets["All Data"], rows, link_graph)
    for name in list(sheets):  # a sheet with nothing to say is left out, not shown empty
        if name not in keep:
            wb.remove(sheets.pop(name))
    _summary(sheets["Summary"], crawler, rows, issues, overall, site, finished, first_url_row, list(sheets), link_graph)
    for name in ("Summary", "Action Plan", "Issue URLs"):
        if name in sheets:
            sheets[name].sheet_properties.tabColor = NAVY
    wb.active = 0
    wb.save(path)
    return overall


def _summary(ws, crawler, rows, issues, overall, site, finished, first_url_row, sheet_names, link_graph):
    host = site.get("host") or crawler.host or ""
    mode = crawler.s["mode"]
    if mode == "list":
        scope, kind = host or "URL list", "%d URLs from your list" % len(rows)
    elif crawler.scope:
        scope, kind = "%s%s/" % (host, crawler.scope), "folder audit · %d pages" % len(rows)
    else:
        scope, kind = host, "%d pages crawled" % len(rows)
    _title(ws, "SEO Audit Report: %s" % scope, "%s · %s" % (finished.strftime("%d %b %Y, %H:%M"), kind))
    for c, w in enumerate([5, 12, 56, 24, 10, 10, 16], 1):
        ws.column_dimensions[get_column_letter(c)].width = w

    # Health score
    grade = "Good" if overall >= 80 else "Needs improvement" if overall >= 60 else "Poor"
    ws.cell(row=4, column=2, value="Health score").font = Font(size=10, color=MUTED)
    sc = ws.cell(row=5, column=2, value="%d / 100" % overall)
    sc.font = Font(size=22, bold=True, color=INK)
    sc.fill = FILL["good" if overall >= 80 else "warn" if overall >= 60 else "bad"]
    ws.merge_cells(start_row=5, start_column=2, end_row=5, end_column=3)
    ws.row_dimensions[5].height = 34
    ws.cell(row=6, column=2, value=grade).font = Font(size=11, bold=True, color=MUTED)

    # Key numbers (one row of tiles)
    status = [r.get("Final Status") for r in rows]
    kpis = [("Pages crawled", len(rows)),
            ("Indexable", sum(1 for r in rows if r.get("Indexable") == "Yes")),
            ("Broken (4xx/5xx)", sum(1 for x in status if isinstance(x, int) and x >= 400)),
            ("Redirected", sum(1 for r in rows if r.get("Redirect Hops")))]
    if link_graph:
        kpis.append(("Orphan pages", sum(1 for r in rows if str(r.get("Orphan Page", "")).startswith("Yes"))))
    kpis.append(("Blocked", sum(1 for r in rows if r.get("Crawl Access", "OK") != "OK")))
    for k, (label, value) in enumerate(kpis):
        col = 2 + k  # columns B..G
        a = ws.cell(row=8, column=col, value=label)
        a.font = Font(size=9, color=MUTED)
        a.alignment = Alignment(wrap_text=True, vertical="bottom")
        b = ws.cell(row=9, column=col, value=value)
        b.font = Font(size=16, bold=True, color=INK)
        b.alignment = Alignment(horizontal="left")
        for cell in (a, b):
            cell.fill = FILL["panel"]
    ws.row_dimensions[9].height = 26

    # Top 10 actions
    r0 = 11
    ws.cell(row=r0, column=2, value="Top 10 actions").font = Font(size=12, bold=True, color=INK)
    ws.cell(row=r0, column=4, value="In order of impact: fix these first").font = Font(size=10, color=MUTED)
    head = ["#", "Priority", "What to fix", "Scope", "Pages", "Effort", "See"]
    for c, h in enumerate(head, 1):
        cell = ws.cell(row=r0 + 1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL["navy"]
    for k, x in enumerate(issues[:10], 1):
        r = r0 + 1 + k
        vals = [k, x["priority"], x["name"] + (": " + x["detail"] if x["detail"] and x["pages"] is None else ""),
                x["scope"], x["pages"] if x["pages"] is not None else "–", x["effort"], "List of pages" if x["rows"] else "Site Health"]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=_v(v))
            cell.border = BOX
            cell.alignment = WRAP if c == 3 else TOP
        ws.cell(row=r, column=2).fill = FILL[x["priority"]] if x["priority"] in FILL else FILL["grey"]
        link = ws.cell(row=r, column=7)
        target = ("#'Issue URLs'!A%d" % first_url_row[x["name"]]) if x["name"] in first_url_row else "#'Site Health'!A1"
        link.hyperlink = target
        link.font = Font(color="1A73E8", underline="single")
        ws.row_dimensions[r].height = 30 if len(str(vals[2])) > 48 else 16

    # How to use this report
    r1 = r0 + 13
    ws.cell(row=r1, column=2, value="How to use this report").font = Font(size=12, bold=True, color=INK)
    guide = [(n, w) for n, w in (
        ("Action Plan", "Every issue, with priority, effort and how to fix it."),
        ("Issue URLs", "One row per page per issue. Filter the Issue column to get your to-do list."),
        ("Pages", "The key SEO fields of every page, one row each."),
        ("Response Codes", "Errors and redirects, with the full redirect chain."),
        ("Links", "Each broken or redirecting link once, with the pages it is on."),
        ("Images", "Images over 200 KB or without alt text, each listed once."),
        ("Internal Linking", "Weak, deep and orphan pages, key pages and folders."),
        ("All Data", "Every column the crawler collected, for advanced filtering.")) if n in sheet_names]
    for k, (name, what) in enumerate(guide, 1):
        a = ws.cell(row=r1 + k, column=2, value=name)
        a.hyperlink = "#'%s'!A1" % name
        a.font = Font(color="1A73E8", underline="single")
        ws.cell(row=r1 + k, column=3, value=what).font = Font(color=INK)
    c = ws.cell(row=r1 + len(guide) + 2, column=2, value="Made by Supriya Mallick")
    c.hyperlink = AUTHOR_URL
    c.font = Font(size=9, color=MUTED, underline="single")
    ws.freeze_panes = "A4"


def _action_plan(ws, issues, first_url_row):
    headers = ["Priority", "Issue", "Category", "Scope", "Pages", "% of pages", "Effort", "How to fix", "Detail", "Pages list"]
    data = [[x["priority"], x["name"], x["category"], x["scope"], x["pages"] if x["pages"] is not None else "",
             x["share"] if x["share"] is not None else "", x["effort"], x["fix"], x["detail"],
             "Open list" if x["name"] in first_url_row else ""] for x in issues]
    first = _table(ws, 1, headers, data, "ActionPlan", [10, 38, 22, 22, 8, 10, 8, 70, 50, 11], wrap_cols=(2, 8, 9),
                   number_formats={6: "0%"})
    _paint_priority(ws, first, len(data), 1)
    for k, x in enumerate(issues):
        if x["name"] in first_url_row:
            cell = ws.cell(row=first + k, column=10)
            cell.hyperlink = "#'Issue URLs'!A%d" % first_url_row[x["name"]]
            cell.font = Font(color="1A73E8", underline="single")
    ws.freeze_panes = "C2"


def _issue_urls(ws, issues, rows):
    groups = defaultdict(list)  # (column, value) -> URLs, for the duplicate title / description / H1 details
    for r in rows:
        for col in ("Meta Title", "Meta Description", "H1 Text"):
            v = str(r.get(col, "")).strip().lower()
            if v and v not in ("missing", "n/a (non-html)"):
                groups[(col, v)].append(r["URL"])
    data, first = [], {}
    for x in issues:
        if not x["rows"]:
            continue
        first[x["name"]] = len(data) + 2
        for r in sorted(x["rows"], key=lambda r: r["URL"]):
            data.append([x["name"], x["priority"], r["URL"], r.get("Final Status", ""), _na(r.get("Indexable", "")), _na(_detail(x["name"], r, groups))])
    start = _table(ws, 1, ["Issue", "Priority", "URL", "Status", "Indexable", "Detail"], data, "IssueURLs",
                   [36, 10, 70, 8, 26, 70], wrap_cols=(6,))
    _paint_priority(ws, start, len(data), 2)
    ws.freeze_panes = "A2"
    return first


def _pages(ws, rows, link_graph=True):
    # (header, width, value) — link-graph columns only when links were followed; Core Web Vitals only when CrUX ran.
    def main_issue(r):
        iss = sorted(_split(r.get("Issues")), key=lambda n: PRIORITY_RANK.get(priority(n), 2))
        return iss[0] if iss else ""
    spec = [("URL", 60, lambda r: r["URL"]), ("Status", 8, lambda r: r.get("Final Status", "")),
            ("Indexable", 24, lambda r: r.get("Indexable", "")), ("Title", 45, lambda r: r.get("Meta Title", "")),
            ("Title length", 8, lambda r: r.get("Title Length", "")), ("Meta description", 50, lambda r: r.get("Meta Description", "")),
            ("Description length", 8, lambda r: r.get("Description Length", "")), ("H1", 35, lambda r: r.get("H1 Text", "")),
            ("Word count", 9, lambda r: r.get("Word Count", "")), ("Canonical", 45, lambda r: r.get("Canonical URL", ""))]
    if link_graph:
        spec += [("Click depth", 8, lambda r: r.get("Click Depth", "")), ("Inlinks", 8, lambda r: _count_prefix(r.get("Linked From (pages)"))),
                 ("Link Score", 8, lambda r: r.get("Link Score (0-100)", ""))]
    if any(r.get("Core Web Vitals") for r in rows):
        spec += [("Core Web Vitals", 18, lambda r: r.get("Core Web Vitals", "")), ("LCP (s)", 8, lambda r: r.get("LCP (s)", "")),
                 ("INP (ms)", 8, lambda r: r.get("INP (ms)", "")), ("CLS", 7, lambda r: r.get("CLS", ""))]
    spec += [("Page score", 8, lambda r: r.get("Page Score", "")),
             ("Errors", 8, lambda r: (r.get("Critical") or 0) + (r.get("High") or 0)), ("Warnings", 9, lambda r: r.get("Medium") or 0),
             ("Notices", 8, lambda r: r.get("Low") or 0), ("Main issue", 32, main_issue)]
    headers = [h for h, _w, _f in spec]
    data = [[_na(f(r)) for _h, _w, f in spec] for r in rows]
    first = _table(ws, 1, headers, data, "Pages", [w for _h, w, _f in spec])
    col = {h: i + 1 for i, h in enumerate(headers)}
    for i, r in enumerate(rows, first):
        sc = r.get("Page Score")
        if isinstance(sc, int):
            ws.cell(row=i, column=col["Page score"]).fill = FILL["good" if sc >= 80 else "warn" if sc >= 60 else "bad"]
        st = r.get("Final Status")
        if st != 200:
            ws.cell(row=i, column=col["Status"]).fill = FILL["bad" if isinstance(st, int) and st >= 400 or st == "Error" else "warn"]
        cwv = str(r.get("Core Web Vitals", ""))
        if "Core Web Vitals" in col and cwv in ("Good", "Needs improvement", "Poor"):
            ws.cell(row=i, column=col["Core Web Vitals"]).fill = FILL[{"Good": "good", "Needs improvement": "warn", "Poor": "bad"}[cwv]]
    ws.freeze_panes = "B2"


def _response_codes(ws, rows):
    """Returns False (sheet left out) when every page answered 200 OK directly."""
    bad = [r for r in rows if r.get("Final Status") != 200 or r.get("Redirect Hops")]
    if not bad:
        return False
    _title(ws, "Response codes", "Every URL that didn't answer 200 OK directly: errors first, then redirects.")
    counts = Counter(str(r.get("Final Status", "")) for r in rows)
    first = _table(ws, 4, ["Status code", "Pages"], sorted(counts.items()), "StatusCounts", [14, 10])
    inl = lambda r: _count_prefix(r.get("Linked From (pages)"))  # noqa: E731
    bad.sort(key=lambda r: (0 if isinstance(r.get("Final Status"), int) and r["Final Status"] >= 500 else
                            1 if isinstance(r.get("Final Status"), int) and r["Final Status"] >= 400 else 2, -inl(r)))
    top = first + len(counts) + 2
    ws.cell(row=top - 1, column=1, value="URLs").font = Font(size=12, bold=True, color=INK)
    data = [[r["URL"], r.get("Initial Status", ""), r.get("Final Status", ""), r.get("Redirect Hops", "") or 0,
             r.get("Redirect Chain", ""), r.get("Final URL", ""), inl(r), r.get("Crawl Access", "OK")] for r in bad]
    _table(ws, top, ["URL", "First status", "Final status", "Redirect hops", "Redirect chain", "Final URL", "Inlinks", "Crawl access"],
           data, "ResponseCodes", [60, 11, 11, 11, 60, 50, 9, 22], wrap_cols=(5,))
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 11
    return True


LINK_FIX = {"Broken": "Remove the link, or point it to a working page (or 301-redirect the missing page to its closest match).",
            "Redirect": "Point the link straight to the final URL, so visitors and crawlers skip the redirect.",
            "Links to noindex": "Link to an indexable page instead, or make the target indexable if it should rank.",
            "Links to canonicalised": "Link straight to the canonical URL, the page that ranks."}


def _links(ws, post):
    """One row per problem link URL, with the pages it is on. Returns False (sheet left out) when there are none."""
    groups = {}
    for b in post.broken_rows:
        found, pages, url, anchor, where, typ, status, result = b[:8]
        g = groups.setdefault((url, result), {"url": url, "result": result, "status": status, "type": typ, "pages": 0,
                                             "on": [], "anchors": Counter(), "where": set()})
        g["pages"] += pages if isinstance(pages, int) else 1
        if found not in g["on"]:
            g["on"].append(found)
        if anchor:
            g["anchors"][anchor] += 1
        g["where"].add(where)
    main = [g for g in groups.values() if g["result"] != "Couldn't verify"]
    unverified = [g for g in groups.values() if g["result"] == "Couldn't verify"]
    if not main and not unverified:
        return False
    order = {"Broken": 0, "Redirect": 1, "Links to noindex": 2, "Links to canonicalised": 3}
    main.sort(key=lambda g: (order.get(g["result"], 9), -g["pages"]))
    _title(ws, "Links to fix", "Each problem link once: what's wrong, how many pages it is on and which ones.")
    row_of = lambda g: [g["result"], g["url"], g["status"], g["type"], g["pages"],  # noqa: E731
                        _url_list(g["on"], total=max(g["pages"], len(g["on"]))) if g["pages"] > len(g["on"]) or len(g["on"]) > MAX_LINKS
                        else ", ".join(g["on"]),
                        g["anchors"].most_common(1)[0][0] if g["anchors"] else "", " / ".join(sorted(g["where"])),
                        LINK_FIX.get(g["result"], "")]
    headers = ["Problem", "Link URL", "Status", "Type", "On pages", "Pages (up to 10)", "Anchor text", "Where", "How to fix"]
    widths = [18, 55, 24, 10, 9, 70, 28, 22, 55]
    top = 4
    if main:
        first = _table(ws, top, headers, [row_of(g) for g in main], "Links", widths, wrap_cols=(6, 9))
        for i, g in enumerate(main, first):
            ws.cell(row=i, column=1).fill = FILL["bad" if g["result"] == "Broken" else "warn"]
        top = first + len(main) + 2
    if unverified:
        ws.cell(row=top - 1, column=1, value="Couldn't verify: these sites refuse automated checks (open them in a browser to be sure)").font = \
            Font(size=11, bold=True, color=INK)
        _table(ws, top, ["Link URL", "Status", "On pages", "Pages (up to 10)"],
               [[g["url"], g["status"], g["pages"], _url_list(g["on"], total=max(g["pages"], len(g["on"])))] for g in unverified],
               "LinksUnverified", wrap_cols=(4,))
        if not main:
            for c, w in enumerate([55, 24, 9, 70], 1):
                ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A5"
    return True


def _images(ws, crawler, post):
    """Only images that need work: over 200 KB, or missing alt text. Returns False (sheet left out) when there are none."""
    used, alt, size, example = Counter(), Counter(), {}, {}
    for r, ex in zip(crawler.rows, crawler.extras):
        for u in ex.get("images") or []:
            used[u] += 1
            example.setdefault(u, r["URL"])
        for u in _urls_in(r.get("Images Missing Alt (URLs)")):
            alt[u] += 1
            example.setdefault(u, r["URL"])
        for u, num, unit in re.findall(r"(https?://\S+?) \(([\d.]+) (KB|MB)\)", str(r.get("Heavy Images (200 KB+)", "") or "")):
            size[u] = round(float(num) * (1024 if unit == "MB" else 1))
            example.setdefault(u, r["URL"])
    for key, res in (post.res or {}).items():
        if key.startswith("img|") and res.get("s"):
            for u in example:
                if norm(u) == key[4:]:
                    size.setdefault(u, round(res["s"] / 1024))
    heavy = lambda u: isinstance(size.get(u), (int, float)) and size[u] >= 200  # noqa: E731
    imgs = [u for u in example if heavy(u) or alt[u]]
    if not imgs:
        return False
    imgs.sort(key=lambda u: (not heavy(u), -(size.get(u) or 0), -alt[u], -used[u]))
    fmt = lambda kb: "%.1f MB" % (kb / 1024) if kb >= 1024 else "%d KB" % kb  # noqa: E731
    data = [[u, max(used[u], alt[u], 1), example[u], "Yes (%s)" % fmt(size[u]) if heavy(u) else "", alt[u] or ""] for u in imgs]
    first = _table(ws, 1, ["Image URL", "Pages using it", "Example page", "Heavy (over 200 KB)", "Pages where alt text is missing"],
                   data, "Images", [70, 10, 55, 16, 14])
    for i, d in enumerate(data, first):
        if d[3]:
            ws.cell(row=i, column=4).fill = FILL["bad"]
        if d[4]:
            ws.cell(row=i, column=5).fill = FILL["warn"]
    ws.freeze_panes = "A2"
    return True


def _site_health(ws, checks):
    basis = _RULES.get("SITE_BASIS", {})
    rank = {"Fail": 0, "Warn": 1, "Info": 2, "Pass": 3}
    data = [[c["status"], c["priority"], c["category"], c["check"], c["result"], c["fix"], basis.get(c["check"], "")]
            for c in sorted(checks, key=lambda c: (rank.get(c["status"], 2), PRIORITY_RANK.get(c["priority"], 3)))]
    first = _table(ws, 1, ["Status", "Priority", "Category", "Check", "Result", "Recommendation", "Why this check"], data, "SiteHealth",
                   [8, 10, 16, 32, 80, 60, 50], wrap_cols=(5, 6, 7))
    for i, d in enumerate(data, first):
        ws.cell(row=i, column=1).fill = FILL[{"Pass": "good", "Warn": "warn", "Fail": "bad"}.get(d[0], "grey")]
    ws.freeze_panes = "A2"


def _discovery(ws, crawler, rows, site):
    s = crawler.s
    access = dict(zip(getattr(crawler, "page_idx", range(len(rows))), getattr(crawler, "access", ["OK"] * len(rows))))
    keys = site.get("sitemap_keys") or set()
    data = []
    for i, (u, src) in enumerate(crawler.queue):
        a = access.get(i)
        data.append([u, src, "Audited" if a == "OK" else ("Not readable: " + a) if a else
                     "Not audited (over the page limit)" if i >= s["max_pages"] else "Not audited (crawl ended early)",
                     "Yes" if src == "Sitemap" or norm(u) in keys else "No" if site.get("sitemap") else "No sitemap found"])
    _table(ws, 1, ["URL", "Found via", "Status", "In sitemap"], data, "Discovery", [70, 14, 30, 14])
    ws.freeze_panes = "A2"


def _about(ws, crawler, site, started, finished, link_graph=True):
    s = crawler.s
    _title(ws, "About this audit")
    d = crawler.discovery
    secs = int((finished - started).total_seconds())
    if s["mode"] == "list":
        mode = "Your list of URLs (links are not followed)"
    elif crawler.scope:
        mode = "Folder %s/%s" % (crawler.scope, " (with the site-wide checks)" if site["site_wide"] else "")
    else:
        mode = "Whole website"
    found = [("Your list", d["listed"]), ("Start page", d["start"]), ("XML sitemap", d["sitemap"]), ("Links on pages", d["crawl"]),
             ("Blocked by robots.txt", d["blocked"])]
    rows = [("Website", site.get("host") or crawler.host), ("Audit type", mode),
            ("Audit date", finished.strftime("%d %b %Y, %H:%M")),
            ("Time taken", "%d min %d s\n%d requests" % (secs // 60, secs % 60, crawler.fetcher.count)),
            ("URLs found from", "\n".join("%s: %d" % (n, v) for n, v in found if v)),
            ("Speed", "%s, ended at %d pages at a time%s" % (s.get("speed", "Medium"), crawler.conc,
                                                           "\nSlowed down %d times: %s" % (crawler.slowdowns, crawler.slow_reason) if crawler.slowdowns else "")),
            ("robots.txt", "Respected" if s.get("respect_robots", True) else "Ignored (site owner's choice)")]
    if s.get("render_js", "off") != "off":
        rows.append(("JavaScript rendering", crawler.render_note or "%d pages rendered" % crawler.rendered))
    rows += [("Priorities", "Errors = Critical and High\nWarnings = Medium\nNotices = Low"),
             ("Scope 'Site-wide (template fix)'", "The issue is on 80% or more of the pages, so it usually comes from a shared template and is fixed once."),
             ("Effort", "An estimate:\nLow = one fix or a few pages\nMedium = several pages\nHigh = many pages to edit one by one")]
    k = 3
    for a, b in rows:
        ws.cell(row=k, column=1, value=a).font = Font(bold=True, color=INK)
        ws.cell(row=k, column=1).alignment = TOP
        ws.cell(row=k, column=2, value=_v(b)).alignment = WRAP
        ws.row_dimensions[k].height = 15 * (str(b).count("\n") + 1)
        k += 1
    k += 1
    ws.cell(row=k, column=1, value="What was checked").font = Font(size=12, bold=True, color=INK)
    # Only what this audit type really checked: no site-wide checks for a folder or a list, no link graph for a list,
    # nothing that was removed from SEO Lens.
    drop = (r"Heading hierarchy|Split anchor|_blank without noopener|Contact signals|Search Console|GA4|search traffic|Core Web Vitals|CrUX|"
            r"Homepage response time|Text-to-HTML|only work with JavaScript|'Load more'|Products|Expected schema|Low product|"
            r"Author|Published|modified|[Dd]ate|JavaScript-rendering|Script count|[Pp]erformance risk|width|height|Bulk")
    if not link_graph:
        drop += r"|[Cc]lick depth|[Oo]rphan|Link Score|PageRank|inlink|[Ll]inked from"
    drop_rx = re.compile(drop)
    for group, items in _RULES["AUDIT_PARAMETERS"]:
        if group in ("Optional APIs", "E-commerce") or (group == "Site-wide" and not site["site_wide"]):
            continue
        items = [x for x in items if not drop_rx.search(x)]
        if group == "Performance signals" and any(r.get("Core Web Vitals") for r in crawler.rows):
            items.append("Core Web Vitals from real Chrome users (CrUX): LCP, INP, CLS")
        if items:
            k += 1
            ws.cell(row=k, column=1, value=group).font = Font(bold=True, color=INK)
            ws.cell(row=k, column=1).alignment = TOP
            ws.cell(row=k, column=2, value="\n".join(items)).alignment = WRAP
            ws.row_dimensions[k].height = 15 * len(items)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110


def _all_data(ws, rows, link_graph=True):
    columns = _useful_columns(COLUMNS, rows, link_graph)
    for c, h in enumerate(columns, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL["navy"]
        cell.alignment = WRAP
    image_cols = [c for c in ("Images Missing Alt (URLs)", "Heavy Images (200 KB+)") if c in columns]  # order in COLUMNS
    for i, r in enumerate(rows, 2):
        shown = {}  # image URL -> the column it was first listed in, on this page
        cells = {}
        for col in image_cols:
            urls = _urls_in(r.get(col))
            if not urls:
                cells[col] = r.get(col, "")
                continue
            new = [u for u in urls if u not in shown]
            again = Counter(shown[u] for u in urls if u in shown)
            if col.startswith("Heavy"):  # keep each file's size: "u (1.4 MB)"
                sizes = dict(re.findall(r"(https?://[^\s,]+) \(([\d.]+ (?:KB|MB))\)", str(r.get(col))))
                parts = ["%s (%s)" % (u, sizes[u]) if u in sizes else u for u in new]
            else:
                parts = new
            note = "; ".join("%d already listed in '%s'" % (n, c.replace(" (URLs)", "")) for c, n in again.items())
            cells[col] = "%d%s: %s" % (len(urls), " (%s)" % note if note else "", ", ".join(parts) or "no new images")
            for u in new:
                shown[u] = col
        for c, col in enumerate(columns, 1):
            v = cells[col] if col in cells else r.get(col, "")
            v = _na(v)  # never show N/A: a cell that doesn't apply stays empty
            if isinstance(v, str) and len(v) > 1000:
                v = v[:1000] + " …"
            ws.cell(row=i, column=c, value=_v(v))
    for c, col in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(c)].width = 45 if col in ("URL", "Final URL", "Meta Title", "Issues") else 18
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(columns)), len(rows) + 1)
    ws.freeze_panes = "B2"
    _colour_pages(ws, rows, columns)
