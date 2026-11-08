"""Writes the audit to an Excel workbook (same tabs as the Google Sheets version) plus the Site Architecture Map (HTML)."""
import json
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .analyze import _RULES
from .checks import PRIORITY_RANK, fix_text, priority
from .htmltools import norm, parse_url, unique

COLUMNS = [
    "URL", "Initial Status", "Final Status", "Crawl Access", "Redirect Hops", "Redirect Chain", "Final URL", "URL Issues", "Indexable", "HTML Size (KB)",
    "Meta Title", "Title Length", "Meta Description", "Description Length", "Canonical URL", "Canonical Target Status", "Meta Robots",
    "HTML Lang", "OG Locale", "Hreflang Tags", "Hreflang Issues", "Viewport", "Open Graph", "Twitter Card",
    "H1 Count", "H1 Text", "Title vs H1", "H2 Tags (body)", "H3 Tags (body)", "Word Count", "Archive Page",
    "Images Missing Alt (URLs)", "Heavy Images (200 KB+)",
    "Linked From (pages)", "Internal Links on Page (content)", "Internal Link Anchors (content)", "External Links on Page (content)", "External Link Anchors (content)",
    "Link URL Types", "Broken Links (on page)", "Orphan Page", "Click Depth", "Link Score (0-100)",
    "Render-Blocking Scripts", "Third-Party Domains", "Core Web Vitals", "LCP (s)", "INP (ms)", "CLS",
    "CMS / Platform", "Tracking Detected",
    "Schema Types", "Schema Parse Errors", "Schema Missing Properties", "Breadcrumbs",
    "Placeholder Text",
    "Duplicate Title", "Duplicate Description", "Duplicate H1", "Duplicate Content", "Near Duplicate",
    "Critical", "High", "Medium", "Low", "Page Score", "Issues",
]
# Still collected (JavaScript-risk drives Auto rendering) but never shown in a report or the app.
HIDDEN_COLUMNS = ["Author", "Published / Modified", "Date Check", "JS-Rendering Risk", "Scripts", "Performance Risk",
                  "Images Missing Width/Height (URLs)", "Contact Signals"]
NAVY = PatternFill("solid", fgColor="1F3864")
FILLS = {k: PatternFill("solid", fgColor=v) for k, v in {"red": "F4CCCC", "amber": "FFF2CC", "green": "D9EAD3", "dgreen": "B7E1CD", "peach": "FCE5CD",
                                                        "blue": "E8F0FE", "grey": "EEEEEE"}.items()}
STATUS_FILL = {"Pass": "green", "Warn": "amber", "Fail": "red", "Info": "grey"}
PRIO_FILL = {"Critical": "red", "High": "peach", "Medium": "amber", "Low": "blue"}
CELL_LIMIT = 32000  # Excel allows 32,767 characters per cell
KEY_PAGE_RE = re.compile(_RULES["KEY_PAGE_RE"]["source"], re.I)
AUTHOR_URL = "https://www.linkedin.com/in/s-mallick"


def _v(v):
    if v is None:
        return ""
    if isinstance(v, str):
        v = v if len(v) <= CELL_LIMIT else v[:CELL_LIMIT] + " …[truncated]"
        return "'" + v if v.startswith("=") else v  # only "=" makes Excel read text as a formula
    return v


def _header(ws, row, values, widths=None):
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = NAVY
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    if widths:
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = w


def overall_score(rows, checks):
    scores = [r["Page Score"] for r in rows if isinstance(r.get("Page Score"), (int, float))]
    avg = round(sum(scores) / len(scores)) if scores else 0
    penalty = min(25, sum(4 if c["status"] == "Fail" else 1 if c["status"] == "Warn" else 0 for c in checks))
    return avg, penalty, max(0, avg - penalty)


def write_report(path, crawler, post, site, started, finished, ai_out=None):
    rows, s = crawler.rows, crawler.s
    checks = site["checks"] + post.checks
    wb = Workbook()
    wb.remove(wb.active)
    avg, penalty, overall = overall_score(rows, checks)

    # ---- Summary ----
    ws = wb.create_sheet("Summary")
    _summary(ws, crawler, rows, checks, site, avg, penalty, overall, started, finished, (ai_out or {}).get("summary"))
    # ---- Page Wise Audit ----
    ws = wb.create_sheet("Page Wise Audit")
    _header(ws, 1, COLUMNS)
    for i, r in enumerate(rows, 2):
        for c, col in enumerate(COLUMNS, 1):
            ws.cell(row=i, column=c, value=_v(r.get(col, "")))
    ws.freeze_panes = "B2"
    for c, col in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(c)].width = 45 if col in ("URL", "Final URL", "Meta Title", "Issues") else 22
    _colour_pages(ws, rows)
    # ---- AI Suggestions (local AI assistant) ----
    if ai_out is not None:
        ws = wb.create_sheet("AI Suggestions")
        _header(ws, 1, ["Page", "What", "Now", "AI Suggestion", "Note"], [50, 16, 50, 60, 14])
        for i, r in enumerate(ai_out["rows"], 2):
            for c, v in enumerate(r, 1):
                ws.cell(row=i, column=c, value=_v(v)).alignment = Alignment(wrap_text=True, vertical="top")
        if not ai_out["rows"]:
            ws.cell(row=2, column=1, value="No page needed a new title, description, H1 or alt text (or the AI couldn't write one).")
        ws.freeze_panes = "B2"
    # ---- Internal Linking ----
    _internal_linking(wb.create_sheet("Internal Linking"), crawler, post)
    # ---- Broken Links ----
    ws = wb.create_sheet("Broken Links")
    _header(ws, 1, ["Found On Page", "Pages", "Link URL", "Anchor Text", "Where", "Type", "Status", "Result", "How to Fix"], [45, 8, 50, 25, 22, 10, 22, 20, 60])
    if post.broken_rows:
        for i, b in enumerate(post.broken_rows, 2):
            for c, v in enumerate(b, 1):
                ws.cell(row=i, column=c, value=_v(v))
            ws.cell(row=i, column=8).fill = FILLS["red" if b[7] == "Broken" else "amber" if b[7] in ("Redirect", "Links to noindex", "Links to canonicalised") else "grey"]
    else:
        ws.cell(row=2, column=1, value="No broken, redirecting or unverified links found. 🎉")
    ws.freeze_panes = "A2"
    # ---- Site Health ----
    ws = wb.create_sheet("Site Health")
    basis = _RULES.get("SITE_BASIS", {})
    _header(ws, 1, ["Category", "Check", "Status", "Result", "Decision Basis", "Priority", "Recommendation"], [16, 34, 9, 80, 60, 10, 60])
    for i, c in enumerate(checks, 2):
        for k, v in enumerate([c["category"], c["check"], c["status"], c["result"], basis.get(c["check"], ""), c["priority"], c["fix"]], 1):
            ws.cell(row=i, column=k, value=_v(v))
        ws.cell(row=i, column=3).fill = FILLS[STATUS_FILL.get(c["status"], "grey")]
    ws.freeze_panes = "A2"
    # ---- Discovered URLs ----
    ws = wb.create_sheet("Discovered URLs")
    _header(ws, 1, ["URL", "Source", "Status", "In Sitemap"], [70, 14, 14, 30])
    access = dict(zip(getattr(crawler, "page_idx", range(len(rows))), getattr(crawler, "access", ["OK"] * len(rows))))
    keys = site.get("sitemap_keys") or set()
    for i, (u, src) in enumerate(crawler.queue, 2):
        ws.cell(row=i, column=1, value=_v(u))
        ws.cell(row=i, column=2, value=src)
        a = access.get(i - 2)
        ws.cell(row=i, column=3, value="Audited" if a == "OK" else ("Not readable: " + a) if a else
                "Not audited (over the page limit)" if i - 2 >= s["max_pages"] else "Not audited (crawl ended early)")
        ws.cell(row=i, column=4, value="Yes" if src == "Sitemap" or norm(u) in keys else "No" if site.get("sitemap") else "No (no XML sitemap found)")
    ws.freeze_panes = "A2"
    wb.save(path)
    return overall


def _summary(ws, crawler, rows, checks, site, avg, penalty, overall, started, finished, ai_summary=None):
    s = crawler.s
    out, fmt = [], []

    def push(row, kind=None):
        out.append(row)
        if kind:
            fmt.append((len(out), kind))

    host = site.get("host") or ""
    scope_note = ("These %d URLs (%s)" % (len(rows), host)) if s["mode"] == "list" else (host + crawler.scope + "/ (%d pages)" % len(rows) if crawler.scope else host)
    push(["SEO Audit Summary", scope_note], "title")
    push(["Audit date", finished.strftime("%d %b %Y, %I:%M %p")])
    push(["Made with ❤️ by", "Supriya Mallick"], "credit")
    push([])
    if ai_summary:
        push(["AI SUMMARY", "Written by the AI assistant on this computer from the numbers below; check before sharing"], "section")
        push(["In short", ai_summary["summary"]])
        for n, step in enumerate(ai_summary["plan"], 1):
            push(["Fix plan, step %d" % n, step])
        push(["AI suggestions", "New titles, meta descriptions, H1s and alt text: see the AI Suggestions tab"])
        push([])
    push(["PARAMETERS CHECKED", "✅ = analysed on every page or site-wide in this audit"], "section")
    site_type = _RULES["SITE_TYPES"].get(s.get("site_type", "auto"), _RULES["SITE_TYPES"]["auto"])
    push(["Website type", site_type["label"]])
    drop = re.compile(r"Search Console|GA4|search traffic|Core Web Vitals|CrUX|Homepage response time|Text-to-HTML|only work with JavaScript|'Load more'|Products|Expected schema|Low product", re.I)
    for group, items in _RULES["AUDIT_PARAMETERS"]:
        if group in ("Optional APIs", "E-commerce"):
            continue
        if group == "Site-wide" and not site["site_wide"]:
            items, group = ["Pages blocked by robots.txt", "Pages in the XML sitemap", "Bulk date updates"], "Across these pages"
        items = [x for x in items if not drop.search(x)]
        if items:
            push([group, "    ".join("✅ " + x for x in items)])
    push([])
    grade = "Good" if overall >= 80 else "Needs improvement" if overall >= 60 else "Poor"
    push(["Overall Health Score", "%d / 100" % overall, grade], "score")
    push(["Method", "Average page score (%d) minus %s penalties (%d)" % (avg, "site-level" if site["site_wide"] else "Site Health", penalty)])
    push([])
    push(["KEY NUMBERS"], "section")
    push(["Pages audited", len(rows)])
    push(["URLs discovered", len(crawler.queue)])
    d = crawler.discovery
    push(["Discovery sources", ("Start URL: 1 · " if crawler.scope else "Homepage: 1 · " if s["mode"] == "domain" else "") +
          "Sitemap: %d · Crawl (links on pages): %d · Your list: %d · Blocked by robots.txt: %d" % (d["sitemap"], d["crawl"], d["listed"], d["blocked"])])
    push(["Crawl mode", s.get("speed", "Medium") + (" · 🐢 slowed down %d× (ended at %d pages at a time): %s" % (
        crawler.slowdowns, crawler.conc, crawler.slow_reason or "the server got slow") if crawler.slowdowns else " · ended at %d pages at a time" % crawler.conc)])
    push(["JavaScript rendering", crawler.render_note or ("%s: %d page/s rendered with %s (%s)" % (
        "Every page" if s.get("render_js") == "all" else "Auto", crawler.rendered, crawler.renderer.name if crawler.renderer else "a browser",
        "compared with the raw HTML" if s.get("render_js") == "all" else "start page + pages that looked empty without JavaScript"))])
    if s["mode"] == "list":
        push(["Audit scope", "Only the URLs in your list (whole-domain checks such as homepage and HTTPS are skipped)"])
    elif crawler.scope:
        push(["Audit scope", "Only pages inside %s/%s" % (crawler.scope, " (Site Health also checks the whole domain)" if site["site_wide"] else " (whole-domain checks skipped: tick 'Folder audits: also check site-wide health' to add them)")])
    push(["Time taken", "%d min %d s · %d requests" % ((finished - started).seconds // 60, (finished - started).seconds % 60, crawler.fetcher.count)])
    status = [r.get("Final Status") for r in rows]
    push(["200 OK", sum(1 for x in status if x == 200)])
    push(["Redirected URLs", sum(1 for r in rows if r.get("Redirect Hops"))])
    push(["4xx errors", sum(1 for x in status if isinstance(x, int) and 400 <= x < 500)])
    push(["5xx errors", sum(1 for x in status if isinstance(x, int) and x >= 500)])
    push(["Unreachable", sum(1 for x in status if x == "Error")])
    not_ok = [str(r.get("Crawl Access")) for r in rows if r.get("Crawl Access", "OK") != "OK"]
    push(["Blocked or not readable", "%d%s" % (len(not_ok), (" (" + ", ".join("%s: %d" % (k, not_ok.count(k)) for k in sorted(set(not_ok))) + ")") if not_ok else "")])
    push(["Indexable pages", sum(1 for r in rows if r.get("Indexable") == "Yes")])
    wc = [r["Word Count"] for r in rows if isinstance(r.get("Word Count"), int)]
    push(["Average word count (approximate)", round(sum(wc) / len(wc)) if wc else 0])
    push(["Orphan pages (no internal links)", sum(1 for r in rows if str(r.get("Orphan Page", "")).startswith("Yes"))])
    push([])
    agg = {}
    for r in rows:
        for n in [x for x in str(r.get("Issues", "")).split("; ") if x]:
            agg[n] = agg.get(n, 0) + 1
    issue_rows = [{"p": priority(n), "name": n, "pages": c, "w": c, "fix": fix_text(n)} for n, c in agg.items()]
    for c in checks:
        if c["status"] in ("Fail", "Warn"):
            res = re.sub(r":\s+https?://[\s\S]*$", " (URLs listed in the Site Health tab)", c["result"])
            issue_rows.append({"p": c["priority"] or "Medium", "name": c["check"] + ": " + res, "pages": "Site-wide" if site["site_wide"] else "Across these pages",
                               "w": len(rows) + 1, "fix": c["fix"]})
    issue_rows.sort(key=lambda x: (PRIORITY_RANK.get(x["p"], 2), -x["w"]))
    push(["ISSUES BY PRIORITY", "Issue types", "Affected pages (total)"], "section")
    for p in ("Critical", "High", "Medium", "Low"):
        lst = [x for x in issue_rows if x["p"] == p]
        push([p, len(lst), sum(x["pages"] for x in lst if isinstance(x["pages"], int))], "prio:" + p)
    push([])
    push(["TOP 5 QUICK WINS"], "section")
    for k, x in enumerate(issue_rows[:5], 1):
        where = "%d of %d pages" % (x["pages"], len(rows)) if isinstance(x["pages"], int) else x["pages"].lower()
        push(["%d. %s" % (k, x["p"]), "%s (%s). %s" % (x["name"], where, x["fix"])], "prio:" + x["p"])
    push([])
    push(["ALL ISSUES"], "section")
    push(["Priority", "Issue", "Pages Affected", "% of Pages", "How to Fix"], "header")
    for x in issue_rows:
        push([x["p"], x["name"], x["pages"], round(x["pages"] / len(rows), 3) if isinstance(x["pages"], int) and rows else "", x["fix"]], "prio:" + x["p"])
    for i, row in enumerate(out, 1):
        for c, v in enumerate(row, 1):
            ws.cell(row=i, column=c, value=_v(v))
    for i, kind in fmt:
        if kind == "title":
            ws.cell(row=i, column=1).font = Font(bold=True, size=16)
        elif kind == "credit":
            ws.cell(row=i, column=2).hyperlink = AUTHOR_URL
            ws.cell(row=i, column=2).font = Font(color="1A73E8", underline="single")
        elif kind == "section":
            for c in range(1, 6):
                ws.cell(row=i, column=c).fill = NAVY
                ws.cell(row=i, column=c).font = Font(bold=True, color="FFFFFF")
        elif kind == "header":
            for c in range(1, 6):
                ws.cell(row=i, column=c).font = Font(bold=True)
                ws.cell(row=i, column=c).fill = FILLS["grey"]
        elif kind == "score":
            ws.cell(row=i, column=1).font = Font(bold=True, size=13)
            ws.cell(row=i, column=2).font = Font(bold=True, size=13)
            ws.cell(row=i, column=2).fill = FILLS["green" if overall >= 80 else "amber" if overall >= 60 else "red"]
        elif kind.startswith("prio:"):
            ws.cell(row=i, column=1).fill = FILLS[PRIO_FILL.get(kind[5:], "grey")]
    for c, w in enumerate([34, 90, 16, 12, 70], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=cell.column in (2, 5), vertical="top")
    for row in ws.iter_rows(min_row=1):
        if isinstance(row[3].value, float):
            row[3].number_format = "0%"


def _colour_pages(ws, rows, columns=None):
    idx = {c: i + 1 for i, c in enumerate(columns or COLUMNS)}
    for n, r in enumerate(rows, 2):
        def paint(col, fill):
            if col in idx:
                ws.cell(row=n, column=idx[col]).fill = FILLS[fill]
        st = r.get("Final Status")
        if st not in (200, "", None):
            paint("Final Status", "red")
        ix = str(r.get("Indexable", ""))
        paint("Indexable", "green" if ix == "Yes" else "red" if ix.startswith("No") else "grey")
        sc = r.get("Page Score")
        if isinstance(sc, int):
            paint("Page Score", "green" if sc >= 80 else "amber" if sc >= 60 else "red")
        tv = str(r.get("Title vs H1", ""))
        if tv == "Same":
            paint("Title vs H1", "dgreen")
        elif tv.startswith("Similar"):
            paint("Title vs H1", "green")
        elif tv.startswith("Partly"):
            paint("Title vs H1", "amber")
        elif tv.startswith("Different"):
            paint("Title vs H1", "red")
        for col in ("OG Locale", "Archive Page", "Canonical Target Status", "Hreflang Issues"):
            if "⚠" in str(r.get(col, "")):
                paint(col, "amber")
        hv = str(r.get("Heavy Images (200 KB+)", ""))
        if " MB)" in hv:
            paint("Heavy Images (200 KB+)", "red")
        elif " KB)" in hv:
            paint("Heavy Images (200 KB+)", "amber")
        for col in ("Duplicate Title", "Duplicate Description", "Duplicate H1", "Duplicate Content", "Near Duplicate"):
            if str(r.get(col, "")).startswith("Yes"):
                paint(col, "peach")
        if str(r.get("Orphan Page", "")).startswith("Yes"):
            paint("Orphan Page", "amber")
        if isinstance(r.get("Click Depth"), int) and r["Click Depth"] >= 4:
            paint("Click Depth", "amber")
        if isinstance(r.get("Broken Links (on page)"), int) and r["Broken Links (on page)"] > 0:
            paint("Broken Links (on page)", "red")
        if r.get("Critical"):
            paint("Critical", "red")
        if r.get("High"):
            paint("High", "peach")
        for col in ("Meta Title", "Meta Description", "H1 Text", "Canonical URL", "Viewport"):
            if r.get(col) == "Missing":
                paint(col, "amber")


def _internal_linking(ws, crawler, post):
    rows = crawler.rows
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
    lf = lambda v: (num(v) if num(v) is not None else (int(m.group(1)) if (m := re.match(r"^(\d+)", str(v))) else None))  # noqa: E731
    pages = []
    for r in rows:
        if r.get("Final Status") != 200 or r.get("Meta Title") == "N/A (non-HTML)" or r.get("Redirect Hops"):
            continue
        u = r.get("Final URL") or r["URL"]
        p = parse_url(u) or {"path": "/"}
        segs = [x for x in p["path"].split("/") if x]
        cd = r.get("Click Depth")
        pages.append({"u": u, "k": norm(u), "segs": segs, "top": segs[0] if len(segs) > 1 else "", "file": not p["path"].endswith("/") or not segs,
                      "idx": r.get("Indexable") == "Yes", "depth": num(cd), "unreach": str(cd).startswith("Not reachable"),
                      "inl": lf(r.get("Linked From (pages)")), "score": num(r.get("Link Score (0-100)")), "orphan": bool(re.match(r"^(Yes|Possible)", str(r.get("Orphan Page", ""))))})
    has_depth = crawler.s["mode"] == "domain" and crawler.s.get("follow_links", True)
    indexable = [p for p in pages if p["idx"]]
    out, fmt = [], []

    def push(row, kind=None):
        out.append(row)
        if kind:
            fmt.append((len(out), kind))

    def avg(arr):
        v = [x for x in arr if x is not None]
        return round(sum(v) / len(v), 1) if v else ""

    adjacent = post.graph.get("adjacent", {})
    wasted = []
    for r in rows:
        n = lf(r.get("Linked From (pages)"))
        if r.get("Indexable") == "Yes" or not n:
            continue
        idx = str(r.get("Indexable", ""))
        canon = str(r.get("Canonical URL", "")).split(", ")[0]
        better = str(r.get("Final URL")) if r.get("Redirect Hops") else (canon if "canonicalised elsewhere" in idx and canon not in ("", "Missing") else "")
        why = ("Redirects (%s) to %s" % (r.get("Initial Status"), r.get("Final URL"))) if r.get("Redirect Hops") else \
            ("Returns %s" % r.get("Final Status")) if r.get("Final Status") != 200 else re.sub(r"^No\s*[–-]\s*", "", idx)
        frm = [x for x in str(r.get("Linked From (pages)", "")).split(", ") if x.startswith("http")][:3]
        wasted.append([r["URL"], n, why, "", ", ".join(frm), ("Point these links straight to " + better) if better else
                       "Remove these links, or make the page indexable if it should rank" if "noindex" in idx else "Fix the page or remove these links"])
    wasted.sort(key=lambda x: -x[1])
    weak = sorted([p for p in indexable if p["inl"] is not None and p["inl"] <= 2 and p["depth"] != 0], key=lambda p: (p["inl"], p["score"] or 0))
    push(["Internal Linking", "%d pages" % len(pages)], "title")
    push(["What to fix in the internal links, most useful first. Open the Site Architecture Map (HTML file) to see who links to whom."])
    push([])
    push(["OVERVIEW", "Value", "", "", "", "What it means"], "section")
    push(["Indexable pages", "%d of %d" % (len(indexable), len(pages)), "", "", "", "Pages that can rank; internal links should point to these."])
    push(["Average pages linking to an indexable page", avg([p["inl"] for p in indexable]), "", "", "", "Higher = Google finds and values the pages more easily."])
    push(["Indexable pages linked from 2 pages or fewer", len(weak), "", "", "", "Listed under WEAK PAGES below." if weak else "Good: every indexable page has at least 3 internal links."], "warn" if weak else None)
    push(["URLs that get internal links but can't rank", len(wasted), "", "", "", "Listed under LINKS TO URLs THAT CAN'T RANK below." if wasted else "Good: internal links only point to indexable pages."], "warn" if wasted else None)
    deep = [p for p in indexable if p["unreach"] or (p["depth"] is not None and p["depth"] >= 4) or p["orphan"]]
    if has_depth:
        push(["Deepest page (clicks from the start page)", max([p["depth"] or 0 for p in pages] or [0]), "", "", "", "Keep important pages within 3 clicks."])
        push(["Indexable pages 4+ clicks deep, orphan or not reachable", len(deep), "", "", "", "Listed under DEEP & ORPHAN PAGES below." if deep else "Good: every indexable page is close to the start page."], "warn" if deep else None)
    push([])
    push(["WEAK PAGES (indexable, fewest internal links)", "Linked from", "Link Score", "Click depth", "", "What to do"], "section")
    if not weak:
        push(["None: every indexable page is linked from at least 3 pages. 🎉"])
    hubs_by_top = {}
    for p in sorted(indexable, key=lambda p: (p["depth"] if p["depth"] is not None else 99, -(p["score"] or 0))):
        hubs_by_top.setdefault(p["top"], []).append(p)
    for p in weak[:50]:
        hub = next((h for h in hubs_by_top.get(p["top"], []) + hubs_by_top.get("", []) if h["k"] != p["k"] and p["k"] not in adjacent.get(h["k"], [])), None)
        push([p["u"], p["inl"], p["score"], ("Not reachable" if p["unreach"] else p["depth"]) if has_depth else "", "",
              "Add links from related pages" + (", e.g. from " + hub["u"] if hub else "")], "warn")
    push([])
    push(["LINKS TO URLs THAT CAN'T RANK", "Linked from", "Why", "", "Example pages with the link", "What to do"], "section")
    if not wasted:
        push(["None: no internal link points to a redirect, an error, a noindex or a canonicalised page. 🎉"])
    for w in wasted[:50]:
        push(w, "warn")
    push(["Broken links (404, server errors) are listed with every page they're on in the Broken Links tab."])
    push([])
    push(["HUB PAGES (most internal link equity)", "Link Score", "Linked from", "Click depth", "", "What to do"], "section")
    hubs = sorted([p for p in indexable if (p["score"] or 0) >= 25], key=lambda p: (-(p["score"] or 0), -(p["inl"] or 0)))[:10]
    if not hubs:
        push(["No page stands out: every Link Score is under 25 (internal links are spread evenly)."])
    for p in hubs:
        push([p["u"], p["score"], p["inl"], p["depth"] if has_depth else "", "", "Strong page: add links from here to the weak pages of the same topic"])
    push([])
    if has_depth:
        push(["DEEP & ORPHAN PAGES (indexable)", "Click depth", "Linked from", "Orphan", "", "What to do"], "section")
        if not deep:
            push(["None: every indexable page is within 3 clicks and linked from another page. 🎉"])
        for p in sorted(deep, key=lambda p: (not p["unreach"], -(p["depth"] or 0)))[:50]:
            push([p["u"], "Not reachable" if p["unreach"] else p["depth"], p["inl"], "Yes" if p["orphan"] else "", "",
                  "Link it from a category, the menu or a related page" if p["orphan"] or p["unreach"] else "Link it from a page 1–2 clicks from the start page"], "warn")
        push([])
    keys = [p for p in pages if KEY_PAGE_RE.search("/" + "/".join(p["segs"]))]
    if keys:
        push(["KEY PAGES (contact, pricing, booking, sign-up…)", "Click depth", "Linked from", "Link Score", "", "What to do"], "section")
        for p in keys:
            buried = has_depth and (p["unreach"] or (p["depth"] is not None and p["depth"] >= 3))
            push([p["u"], p["depth"] if p["depth"] is not None else "Not reachable" if p["unreach"] else "", p["inl"], p["score"], "",
                  "⚠ Buried: link it from the menu, homepage or top pages (1–2 clicks)" if buried else "Few links: add links from related pages" if (p["inl"] or 0) < 3 else "OK"], "warn" if buried else None)
        push([])
    # A page lives in the folder above it: /blog/post/ is in /blog/, and /about/ is a page at the top level, not a folder.
    groups = {}
    for p in pages:
        dirs = p["segs"][:-1]
        ks = ["/"] + (["/" + dirs[0] + "/"] if dirs else []) + (["/" + dirs[0] + "/" + dirs[1] + "/"] if len(dirs) > 1 else [])
        groups.setdefault(ks[-1], []).append(p)
        if len(ks) == 3:
            groups.setdefault(ks[1], []).append(p)
    # Only folders that hold several pages; in a folder audit, only the sub-folders inside it (not the folder itself or its parents).
    inside = (lambda fo: fo.lower().startswith(crawler.scope + "/") and fo.rstrip("/").lower() != crawler.scope) if crawler.scope else (lambda fo: True)
    real = sorted(fo for fo, g in groups.items() if len(g) >= 2 and fo != "/" and inside(fo))
    if real:
        push(["FOLDERS", "Pages", "Indexable", "Avg click depth", "Avg linked from", "Avg Link Score"], "section")
        if len(groups.get("/", [])) >= 2 and not crawler.scope:
            g = groups["/"]
            push(["/ (pages at the top level)", len(g), sum(1 for p in g if p["idx"]), avg([p["depth"] for p in g]), avg([p["inl"] for p in g]),
                  avg([p["score"] for p in g])], "folder")
        for fo in real:
            g = groups[fo]
            level = len([x for x in fo.split("/") if x])
            push([("   └ " if level == 2 else "") + fo, len(g), sum(1 for p in g if p["idx"]), avg([p["depth"] for p in g]), avg([p["inl"] for p in g]), avg([p["score"] for p in g])],
                 None if level == 2 else "folder")
    for i, row in enumerate(out, 1):
        for c, v in enumerate(row, 1):
            ws.cell(row=i, column=c, value=_v(v))
    for i, kind in fmt:
        if kind == "title":
            ws.cell(row=i, column=1).font = Font(bold=True, size=16)
        elif kind == "section":
            for c in range(1, 7):
                ws.cell(row=i, column=c).fill = NAVY
                ws.cell(row=i, column=c).font = Font(bold=True, color="FFFFFF")
        elif kind == "folder":
            ws.cell(row=i, column=1).font = Font(bold=True)
        elif kind == "warn":
            for c in range(1, 7):
                ws.cell(row=i, column=c).fill = FILLS["amber"]
    for c, w in enumerate([60, 14, 30, 14, 40, 60], 1):
        ws.column_dimensions[get_column_letter(c)].width = w


# ---------------------------------------------------------------- Site Architecture Map

def write_site_map(path, crawler, post):
    rows = crawler.rows
    lf = lambda v: v if isinstance(v, int) else (int(m.group(1)) if (m := re.match(r"^(\d+)", str(v))) else 0)  # noqa: E731
    redirect_to, all_ = {}, []
    for r in rows:
        f = r.get("Final URL") or r["URL"]
        if r.get("Redirect Hops"):
            redirect_to[norm(r["URL"])] = norm(f)
            continue
        if r.get("Final Status") != 200 or r.get("Meta Title") == "N/A (non-HTML)":
            continue
        p = parse_url(f) or {"path": "/"}
        cd = r.get("Click Depth")
        all_.append({"k": norm(f), "u": f, "path": p["path"], "d": cd if isinstance(cd, int) else -1, "inl": lf(r.get("Linked From (pages)")),
                     "sc": r.get("Link Score (0-100)") if isinstance(r.get("Link Score (0-100)"), int) else 0,
                     "o": bool(re.match(r"^(Yes|Possible)", str(r.get("Orphan Page", "")))), "ix": r.get("Indexable") == "Yes", "c": 0})
    if not all_:
        return False
    keep = sorted(all_, key=lambda x: (-x["o"], -(x["d"] >= 4), -x["sc"]))[:400]
    at = {x["k"]: i for i, x in enumerate(keep)}
    links, seen = [], set()
    for src, targets in post.graph.get("adjacent", {}).items():
        s = at.get(src)
        if s is None:
            continue
        for t in targets:
            t = at.get(redirect_to.get(t, t))
            if t is None or t == s or (s, t) in seen or len(links) >= 6000:
                continue
            seen.add((s, t))
            links.append([s, t])
    root = {"name": "/", "n": 0, "ds": 0, "dn": 0, "kids": {}}
    for x in all_:
        node = root
        segs = [s for s in x["path"].split("/") if s]

        def addn(nd):
            nd["n"] += 1
            if x["d"] >= 0:
                nd["ds"] += x["d"]
                nd["dn"] += 1
        addn(node)
        for seg in segs[:6]:
            node = node["kids"].setdefault(seg, {"name": seg, "n": 0, "ds": 0, "dn": 0, "kids": {}})
            addn(node)
        if len(segs) <= 6 and ("s" not in node or (x["d"] >= 0 and (node["s"]["d"] < 0 or x["d"] < node["s"]["d"]))):
            node["s"] = {"u": x["u"], "d": x["d"], "sc": x["sc"], "inl": x["inl"], "o": x["o"], "ix": x["ix"]}

    def tree(n):
        kids = sorted([tree(k) for k in n["kids"].values()], key=lambda k: (-k["n"], k["name"]))
        return {"name": n["name"], "n": n["n"], "d": round(n["ds"] / n["dn"], 1) if n["dn"] else -1, "s": n.get("s"), "more": max(0, len(kids) - 60), "children": kids[:60]}

    data = {"nodes": [{"u": x["u"], "p": x["path"], "d": x["d"], "inl": x["inl"], "sc": x["sc"], "o": x["o"], "ix": x["ix"], "c": 0} for x in keep],
            "links": links, "tree": tree(root), "total": len(all_), "host": (parse_url(all_[0]["u"]) or {}).get("host", "")}
    tpl = open(os.path.join(os.path.dirname(__file__), "data", "site_map.html"), encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/null", json.dumps(data)))
    return True
