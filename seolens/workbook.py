"""Issue helpers shared by the Excel report: grouping issues, evidence text, image lists.
The workbook itself is built in report_sheets.py (with report_data.py, report_style.py and report_dashboard.py).
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
    "Page not found (404/410)": "Restore these pages or 301-redirect them to the closest live page, then update the internal links "
                                "and the sitemap.",
    "Client error (4xx)": "These URLs refuse the request (e.g. 401 login required, 403 forbidden). If they should be public, allow "
                          "them; otherwise remove the internal links and sitemap entries pointing to them.",
}
SPLIT_PRIORITY = {"Server error (5xx)": "Critical", "Page not found (404/410)": "Critical", "Client error (4xx)": "High"}
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


CATEGORY_FIX = {"Server error (5xx)": "Crawlability & indexing", "Page not found (404/410)": "Crawlability & indexing", "Client error (4xx)": "Crawlability & indexing",
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
        # Error pages don't get an inlink count in the crawl, so links to them are counted from the link checks.
        n = _count_prefix(row.get("Linked From (pages)")) or (groups or {}).get(("links_to", norm(row["URL"])), 0)
        status = "HTTP %s" % row.get("Final Status", "")
        return "%s · linked from %d page%s" % (status, n, "" if n == 1 else "s") if n else "%s · no audited page links to it" % status
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
    from .themes import theme_of
    n = max(1, len(rows))
    html = sum(1 for r in rows if r.get("Final Status") == 200 and not str(r.get("Meta Title", "")).startswith("N/A"))
    per = defaultdict(list)
    for r in rows:
        for name in _split(r.get("Issues")):
            if name == "Page not returning 200":
                st = r.get("Final Status")
                name = ("Server error (5xx)" if isinstance(st, int) and st >= 500 else
                        "Page not found (404/410)" if st in (404, 410) else
                        "Client error (4xx)" if isinstance(st, int) and 400 <= st < 500 else name)
            per[name].append(r)
    issues, by_name = [], {}
    for name, lst in per.items():
        prio = SPLIT_PRIORITY.get(name) or priority(name)
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

LINK_FIX = {"Broken": "Remove the link, or point it to a working page (or 301-redirect the missing page to its closest match).",
            "Redirect": "Point the link straight to the final URL, so visitors and crawlers skip the redirect.",
            "Links to noindex": "Link to an indexable page instead, or make the target indexable if it should rank.",
            "Links to canonicalised": "Link straight to the canonical URL, the page that ranks."}


def write_report(path, crawler, post, site, started, finished, ai_out=None):
    """The Excel report (11 sheets, see report_sheets.py). Returns the overall health score, as before."""
    from .report_sheets import write_workbook
    return write_workbook(path, crawler, post, site, started, finished, ai_out)
