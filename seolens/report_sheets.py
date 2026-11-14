"""The Excel report: 11 visible sheets built from one normalised model (report_data.py), plus hidden raw-data sheets.

    01 Dashboard · 02 Action Plan · 03 Issue Details · 04 Page Audit · 05 On-Page SEO · 06 Technical SEO ·
    07 Internal Links · 08 Images & Speed · 09 Schema & Social · 10 Crawl Coverage · 11 Raw & Audit Info
    hidden: __Raw Pages · __Raw Links · __Raw Checks · __Raw Discovery (every issue record is on sheet 03)

Every sheet: title (row 1), subtitle (2), website · date · run · scope (4), filterable table header (6), records from 7.
"""
import contextvars
import re
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation

from . import __version__
from .checks import MAX_EXTERNAL_CHECKS, MAX_IMAGE_CHECKS, MAX_INTERNAL_CHECKS, PRIORITY_RANK
from .htmltools import norm
from .report import COLUMNS, HIDDEN_COLUMNS
from .report_dashboard import add_dashboard
from .report_data import (PRIORITY_RULES, SEVERITIES, TECHNICAL_THEMES, build_model, evidence_urls, inlinks, page_issues,
                          page_type)
from .report_style import (FONT, MUTED, PRIORITY_PALE, SEVERITY_PALE, add_table, colour_prefix, colour_values, finish_sheet,
                           put, title_block)
from .themes import theme_of
from .workbook import LINK_FIX, _na

TASK_STATUSES = ["To Do", "In Progress", "Blocked", "Fixed", "Verified", "Not Applicable"]
LINK_LIMIT = 3000  # clickable URL cells per sheet: more slows Excel down; the URLs are still there as text
NOT_MEASURED = "Not measured"


def _meta(m):
    return "Website: %s   ·   Audit: %s   ·   Run %s   ·   %s" % (m.host or "URL list", m.finished.strftime("%d %b %Y, %H:%M"), m.run_id,
                                                              m.audit_type)


def _clean(v):
    """Values as people read them: no 'N/A', no 'None' placeholders."""
    v = _na(v)
    return "" if v in ("None", None) else v


def _sev_rank(rule):
    from .checks import priority
    return PRIORITY_RANK.get(priority(rule), 2)


def _adv(m):
    return getattr(m.post, "advanced", None) or {}


def _render_status(r):
    """Measured | Failed | Not measured | Not applicable (never blank: rendering off means 'Not measured')."""
    v = str(r.get("Rendering Check", "") or "")
    if v:
        return v.split(" (")[0].split(":")[0]
    if r.get("Final Status") != 200 or str(r.get("Meta Title", "")).startswith("N/A"):
        return "Not applicable"
    return NOT_MEASURED


def _int(v):
    return v if isinstance(v, int) else ""


def _top_issue(row, keep=lambda rule: True):
    from .checks import priority
    lst = [i for i in page_issues(row) if keep(i)]
    if not lst:
        return "", ""
    best = sorted(lst, key=_sev_rank)[0]
    return best, priority(best)


# The app shows the same tables as the workbook: collect_tables() runs the sheet code with _table() recording each table's
# headers and rows instead of writing them (a context variable, so app sessions in other threads are not affected).
_SINK = contextvars.ContextVar("seolens_table_sink", default=None)


def _table(ws, name, headers, rows, widths, top=6, **kw):
    sink = _SINK.get()
    if sink is None:
        return add_table(ws, name, headers, rows, widths, top=top, **kw)
    sink[name] = (list(headers), [list(r) for r in rows])
    for c, h in enumerate(headers, 1):
        ws.cell(row=top, column=c, value=h)
    return top + 1, top + max(1, len(rows))


def collect_tables(m):
    """{table name: (headers, rows)} for every table of sheets 02-11, from a model (report_data.build_model)."""
    sink = {}
    token = _SINK.set(sink)
    try:
        wb = Workbook()
        plan_rows = _action_plan(wb, m)
        _issue_details(wb, m, plan_rows)
        for f in (_page_audit, _on_page, _technical, _internal_links, _images_speed, _schema_social, _crawl_coverage):
            f(wb, m)
        _raw_and_info(wb, m, validate(m), None, raw=False)
    finally:
        _SINK.reset(token)
    return sink


def write_workbook(path, crawler, post, site, started, finished, ai_out=None):
    m = build_model(crawler, post, site, started, finished, ai_out)
    wb = Workbook()
    wb.remove(wb.active)
    add_dashboard(wb, m)
    plan_rows = _action_plan(wb, m)
    _issue_details(wb, m, plan_rows)
    _page_audit(wb, m)
    _on_page(wb, m)
    _technical(wb, m)
    _internal_links(wb, m)
    _images_speed(wb, m)
    _schema_social(wb, m)
    _crawl_coverage(wb, m)
    problems = validate(m)
    _raw_and_info(wb, m, problems, ai_out)
    wb.active = 0
    wb.save(path)
    return m.score


# ---------------------------------------------------------------- 02 Action Plan

def _action_plan(wb, m):
    ws = wb.create_sheet("02 Action Plan")
    title_block(ws, "Action Plan", "One task per fix. Assign an owner, set a status and a date; the URLs behind each task are on "
                                   "sheet 03 (click the Task ID).", _meta(m))
    data = [[t.task_id, t.priority, t.severity, t.rule, t.fix_scope, t.affected if t.affected else "Site-wide", t.owner, t.status,
             t.fix, t.verification, t.effort, "", ""] for t in m.tasks]
    first, last = _table(ws, "ActionPlan", ["Task ID", "Priority", "Severity", "Issue", "Fix Scope", "Affected URLs", "Owner", "Status",
                                               "Recommended Fix", "Verification", "Effort", "Target Date", "Notes"],
                            data, [12, 9, 10, 34, 26, 10, 14, 13, 55, 13, 9, 12, 30], wrap=(4, 5, 9, 13),
                            empty_note="No issues found: nothing to fix.")
    if m.tasks:
        dv = DataValidation(type="list", formula1='"%s"' % ",".join(TASK_STATUSES), allow_blank=False,
                            error="Pick a status from the list.", errorTitle="Status")
        ws.add_data_validation(dv)
        dv.add("H%d:H%d" % (first, last))
        for r in range(first, last + 1):
            ws.cell(row=r, column=12).number_format = "dd mmm yyyy"
        colour_values(ws, 2, first, last, PRIORITY_PALE)
        colour_values(ws, 3, first, last, SEVERITY_PALE)
    finish_sheet(ws, 1)
    return {t.task_id: first + k for k, t in enumerate(m.tasks)}


# ---------------------------------------------------------------- 03 Issue Details

def _issue_details(wb, m, plan_rows):
    ws = wb.create_sheet("03 Issue Details")
    title_block(ws, "Issue Details", "One row per affected URL per rule (site-wide findings have Scope 'Site' and no URL). "
                                     "Click a Task ID to go to its task.", _meta(m))
    order = {t.task_id: k for k, t in enumerate(m.tasks)}
    issues = sorted(m.issues, key=lambda i: (order.get(i.task_id, 0), i.url))
    data = [[i.issue_id, i.rule, i.severity, i.scope, i.url, _clean(i.evidence), i.expected, i.fix, i.task_id, i.verification,
             i.category] for i in issues]
    first, last = _table(ws, "IssueDetails", ["Issue ID", "Rule", "Severity", "Scope", "URL", "Evidence", "Expected Result",
                                                 "Suggested Fix", "Task ID", "Verification Status", "Category"],
                            data, [13, 32, 10, 8, 55, 55, 34, 50, 11, 14, 20], wrap=(6, 7, 8), empty_note="No issues found.")
    first_issue = {}
    for k, i in enumerate(issues):
        r = first + k
        first_issue.setdefault(i.task_id, r)
        if i.task_id in plan_rows:
            ws.cell(row=r, column=9).hyperlink = "#'02 Action Plan'!A%d" % plan_rows[i.task_id]
            ws.cell(row=r, column=9).font = Font(name=FONT, color="1D4ED8", underline="single", size=10)
        if i.url and len(issues) <= LINK_LIMIT:
            ws.cell(row=r, column=5).hyperlink = i.url
    if issues:
        colour_values(ws, 3, first, last, SEVERITY_PALE)
    # Action Plan → first record of each task
    plan = wb["02 Action Plan"]
    for tid, pr in plan_rows.items():
        if tid in first_issue:
            plan.cell(row=pr, column=1).hyperlink = "#'03 Issue Details'!A%d" % first_issue[tid]
            plan.cell(row=pr, column=1).font = Font(name=FONT, color="1D4ED8", underline="single", size=10)
    finish_sheet(ws, 2)


# ---------------------------------------------------------------- 04 Page Audit

def _page_audit(wb, m):
    ws = wb.create_sheet("04 Page Audit")
    title_block(ws, "Page Audit", "One row per audited page: its status, key SEO fields and the most important thing to fix.", _meta(m))
    data = []
    for r in m.rows:
        issue, _sev = _top_issue(r)
        crit = (r.get("Critical") or 0) + (r.get("High") or 0)
        data.append([r["URL"], r.get("Content Type") or page_type(r), r.get("Final Status", ""), _clean(r.get("Indexable", "")), r.get("Page Score", ""),
                     _clean(r.get("Meta Title", "")), _clean(r.get("Meta Description", "")), _clean(r.get("H1 Text", "")),
                     r.get("Word Count", "") if isinstance(r.get("Word Count"), int) else "", _clean(r.get("Canonical URL", "")),
                     issue or "Nothing to fix", crit, r.get("Medium") or 0, r.get("Low") or 0,
                     (r.get("Click Depth") if isinstance(r.get("Click Depth"), int) else _clean(r.get("Click Depth", ""))) if m.link_graph else NOT_MEASURED,
                     inlinks(r) if m.link_graph else NOT_MEASURED, _int(r.get("URL Depth")), _clean(r.get("URL Parameters", "")) or "None",
                     _render_status(r)])
    first, last = _table(ws, "PageAudit", ["URL", "Page Type", "HTTP Status", "Indexable", "Page Score", "Title", "Meta Description", "H1",
                                              "Main Content Words", "Canonical", "Action Needed", "Critical + High", "Medium", "Low",
                                              "Click Depth", "Inlinks", "URL Depth (folders)", "URL Parameters", "JS Rendering"],
                            data, [55, 16, 8, 22, 8, 45, 50, 35, 9, 45, 34, 9, 8, 8, 9, 8, 9, 22, 13], wrap=(6, 7, 11))
    _url_links(ws, first, len(m.rows), m.rows)
    from openpyxl.formatting.rule import CellIsRule
    from .report_style import fill
    ws.conditional_formatting.add("E%d:E%d" % (first, last), CellIsRule(operator="lessThan", formula=["60"], fill=fill("FDE2E2")))
    ws.conditional_formatting.add("E%d:E%d" % (first, last), CellIsRule(operator="between", formula=["60", "79"], fill=fill("FEF6C7")))
    ws.conditional_formatting.add("E%d:E%d" % (first, last), CellIsRule(operator="greaterThanOrEqual", formula=["80"], fill=fill("E8F6EB")))
    colour_prefix(ws, 4, first, last, {"No": "FDE2E2"})
    finish_sheet(ws, 3, freeze="B7")


def _url_links(ws, first, n, rows, col=1):
    if n <= LINK_LIMIT and _SINK.get() is None:
        for k, r in enumerate(rows):
            ws.cell(row=first + k, column=col).hyperlink = r["URL"]


# ---------------------------------------------------------------- 05 On-Page SEO

ONPAGE = {"On-page SEO", "Content"}


def _on_page(wb, m):
    ws = wb.create_sheet("05 On-Page SEO")
    title_block(ws, "On-Page SEO", "Titles, descriptions, headings and content, with duplicate groups. Length is in characters "
                                   "(pixel width is not measured). Main content words leave out menus, header, footer and sidebars; "
                                   "the review threshold depends on the page type.", _meta(m))
    groups, gid = defaultdict(list), {}
    for r in m.rows:
        for col, label in (("Meta Title", "Title"), ("Meta Description", "Description"), ("H1 Text", "H1")):
            v = str(r.get(col, "")).strip().lower()
            if v and v not in ("missing", "none") and not v.startswith("n/a"):
                groups[(label, v)].append(r["URL"])
    n = Counter()
    for key, urls in groups.items():
        if len(urls) > 1:
            n[key[0]] += 1
            gid[key] = "%s %d (%d pages)" % (key[0], n[key[0]], len(urls))
    data = []
    for r in m.rows:
        if str(r.get("Meta Title", "")).startswith("N/A"):
            continue
        dup = [gid[(label, str(r.get(col, "")).strip().lower())] for col, label in (("Meta Title", "Title"), ("Meta Description", "Description"),
               ("H1 Text", "H1")) if (label, str(r.get(col, "")).strip().lower()) in gid]
        issue, _ = _top_issue(r, lambda rule: theme_of(rule) in ONPAGE or "alt text" in rule)
        from .checks import fix_text
        data.append([r["URL"], r.get("Content Type") or page_type(r), _clean(r.get("Meta Title", "")), r.get("Title Length", ""),
                     _clean(r.get("Meta Description", "")), r.get("Description Length", ""), _clean(r.get("H1 Text", "")),
                     _int(r.get("Total Words")), _int(r.get("Word Count")), _int(r.get("Boilerplate Words")),
                     _clean(r.get("Content Extraction", "")), _clean(r.get("Content Flag", "")), "; ".join(dup),
                     ("%s: %s" % (issue, fix_text(issue))) if issue else "Nothing to change"])
    first, last = _table(ws, "OnPage", ["URL", "Page Type", "Title", "Title Length (characters)", "Meta Description",
                                           "Meta Length (characters)", "H1", "Total Words", "Main Content Words", "Boilerplate Words",
                                           "Extraction Method", "Content Flag", "Duplicate Group", "Suggested Edit"],
                            data, [55, 18, 45, 11, 55, 11, 38, 9, 9, 10, 30, 34, 30, 60], wrap=(3, 5, 7, 11, 12, 14),
                            empty_note="No HTML pages were audited.")
    colour_prefix(ws, 12, first, last, {"Below": "FEF6C7"})
    finish_sheet(ws, 4, freeze="B7")


# ---------------------------------------------------------------- 06 Technical SEO

def _technical(wb, m):
    ws = wb.create_sheet("06 Technical SEO")
    title_block(ws, "Technical SEO", "Status, indexability, robots, canonical, redirects, sitemap, parameters, pagination and "
                                     "JavaScript rendering, one row per page; then the canonical clusters and the parameter URLs. "
                                     "Canonicals and sitemaps are signals: conflicts are for review, not proof of what Google does.", _meta(m))
    from .checks import fix_text, priority
    data = []
    for r in m.rows:
        issue = _top_issue(r, lambda rule: theme_of(rule) in TECHNICAL_THEMES)[0]
        first_st, final_st = r.get("Initial Status", ""), r.get("Final Status", "")
        status = final_st if str(first_st) in ("", str(final_st)) else "%s → %s" % (first_st, final_st)
        hops = r.get("Redirect Hops") or 0
        chain = _clean(r.get("Redirect Chain", ""))
        canon = _clean(r.get("Canonical URL", ""))
        cstat = _clean(r.get("Canonical Target Status", ""))
        if not m.has_sitemap:
            in_sm = "No sitemap found"
        else:
            in_sm = "Yes" if norm(r.get("Final URL") or r["URL"]) in m.sitemap_keys or norm(r["URL"]) in m.sitemap_keys else "No"
        rend = _render_status(r)
        diffs = _clean(r.get("Rendering Differences", "")) if rend == "Measured" else rend
        data.append([r["URL"], status, _clean(r.get("Indexable", "")), _clean(r.get("Meta Robots", "")) or "Not set",
                     ("%s (%s)" % (canon, cstat)) if canon and cstat else canon or "Missing",
                     ("%d: %s" % (hops, chain)) if hops else "None", in_sm, _clean(r.get("URL Parameters", "")) or "None",
                     _clean(r.get("Canonical Cluster", "")), _clean(r.get("Indexability Conflicts", "")) or "None",
                     _clean(r.get("Pagination", "")), diffs, issue or "None",
                     priority(issue) if issue else "", fix_text(issue) if issue else ""])
    first, last = _table(ws, "Technical", ["URL", "HTTP Status", "Indexable", "Meta Robots", "Canonical", "Redirect Hops / Chain",
                                              "In Sitemap", "URL Parameters", "Canonical Cluster", "Indexability Conflicts", "Pagination",
                                              "JS Rendering Differences", "Technical Issue", "Severity", "Suggested Fix"],
                            data, [55, 11, 24, 30, 50, 40, 12, 22, 34, 50, 40, 50, 32, 10, 55], wrap=(4, 5, 6, 9, 10, 11, 12, 15))
    _url_links(ws, first, len(m.rows), m.rows)
    colour_values(ws, 14, first, last, SEVERITY_PALE)
    colour_prefix(ws, 3, first, last, {"No": "FDE2E2"})
    colour_prefix(ws, 10, first, last, {"None": "FFFFFF", "": "FEF6C7"})
    adv = _adv(m)

    top = last + 3
    put(ws, top - 1, 1, "Canonical clusters: pages that declare another page as canonical, grouped by the canonical they resolve to. "
                        "Pages that only point to themselves are left out.").font = Font(name=FONT, bold=True, size=11)
    first, last = _table(ws, "CanonicalClusters", ["Cluster ID", "Source URL", "Declared Canonical", "Resolved Canonical", "Cluster Size",
                                                      "HTTP Status", "Indexability", "Content Similarity", "Conflict Type"],
                            adv.get("clusters", []), [], top=top, wrap=(9,),
                            empty_note="No canonical clusters: every page with a canonical points to itself.")
    colour_prefix(ws, 9, first, last, {"Possible mismatch": "FDE2E2", "Conflicting": "FDE2E2", "Canonical target problem": "FDE2E2",
                                       "Expected": "E8F6EB", "Self": "E8F6EB"})

    top = last + 3
    crawled = m.settings.get("include_query") or m.mode == "list"
    put(ws, top - 1, 1, "URL parameters: URLs with ?parameters found in internal links, the sitemap or the crawl. Private-looking values "
                        "(sessions, tokens, emails) are hidden." + ("" if crawled else " Parameter URLs were not crawled in this audit, so "
                                                                   "their content is not measured.")).font = Font(name=FONT, bold=True, size=11)
    _table(ws, "ParameterURLs", ["URL", "Clean URL", "Parameters", "Parameter Count", "Found Via", "Crawl Status", "Canonical",
                                    "Indexability", "Finding"],
              adv.get("params", []), [], top=top, wrap=(1, 5, 6, 9), empty_note="No URLs with ?parameters were found.")
    finish_sheet(ws, 5, freeze="B7")


# ---------------------------------------------------------------- 07 Internal Links

def _internal_links(wb, m):
    ws = wb.create_sheet("07 Internal Links")
    title_block(ws, "Internal Links", "Problem links (broken, redirecting, noindex, canonicalised, internal and external), one link "
                                      "summary per page, anchor-text distribution per linked page and, when JavaScript rendering ran, links "
                                      "that differ between the raw HTML and the rendered page. 'Edge Type' tells them apart.", _meta(m))
    by_url = {}
    for r in m.rows:
        by_url[norm(r["URL"])] = r
        if r.get("Final URL"):
            by_url.setdefault(norm(r["Final URL"]), r)
    order = {"Broken": 0, "Redirect": 1, "Links to noindex": 2, "Links to canonicalised": 3}
    data = []
    for b in sorted(m.broken_rows, key=lambda b: (order.get(b[7], 9), -(b[1] if isinstance(b[1], int) else 0))):
        found, pages, url, anchor, where, typ, status, result = b[:8]
        target = by_url.get(norm(url))
        depth = target.get("Click Depth") if target and m.link_graph else ""
        data.append(["Problem link (%s%s)" % (typ.lower(), ", on %d pages" % pages if isinstance(pages, int) and pages > 1 else ""),
                     found, url, anchor, NOT_MEASURED, status,
                     depth if isinstance(depth, int) else (_clean(depth) if depth else ""),
                     inlinks(target) if target and m.link_graph else "", result, LINK_FIX.get(result, b[8] if len(b) > 8 else "")])
    for r in m.rows:
        if str(r.get("Meta Title", "")).startswith("N/A") or r.get("Final Status") != 200:
            continue
        n_in = inlinks(r)
        flags, action = [], ""
        if m.link_graph:
            if str(r.get("Orphan Page", "")).startswith("Yes"):
                flags.append("Orphan page")
            d = r.get("Click Depth")
            if isinstance(d, int) and d >= 4:
                flags.append("Deep page (%d clicks)" % d)
            elif isinstance(d, str) and d.startswith("Not reachable"):
                flags.append("Not reachable from the start page")
            if r.get("Indexable") == "Yes" and n_in <= 2 and not flags:
                flags.append("Weak: linked from %d page%s" % (n_in, "" if n_in == 1 else "s"))
            if flags:
                action = "Link it from the menu, a category or related pages, within 3 clicks of the start page"
        data.append(["Page link summary", r["URL"], "", "", "", r.get("Final Status", ""),
                     (r.get("Click Depth") if isinstance(r.get("Click Depth"), int) else _clean(r.get("Click Depth", ""))) if m.link_graph else NOT_MEASURED,
                     n_in if m.link_graph else NOT_MEASURED, "; ".join(flags), action])
    adv = _adv(m)

    def target_info(url):
        t = by_url.get(norm(url))
        if not t:
            return "", "", ""
        d = t.get("Click Depth") if m.link_graph else ""
        return t.get("Final Status", ""), d if isinstance(d, int) else (_clean(d) if d else ""), inlinks(t) if m.link_graph else ""

    for dest, n_src, n_c, n_m, n_var, top_anchors, finding in adv.get("anchors", []):
        st, depth, n_in = target_info(dest)
        data.append(["Anchor distribution", "%d linking page%s: %d contextual, %d menu/footer link%s" % (
            n_src, "" if n_src == 1 else "s", n_c, n_m, "" if n_m == 1 else "s"), dest, top_anchors, "", st, depth, n_in,
            "Anchors: " + finding, "In body text, link with words that describe this page; menus and footers are counted apart"])
    actions = {"Rendered-only link": ("Only in the rendered page: found once JavaScript runs",
                                      "If it matters, put it in the server HTML as a normal <a href> link (Google renders most pages, "
                                      "but later)"),
               "Removed by JavaScript": ("In the raw HTML, removed when JavaScript runs", "Check that the removal is intended"),
               "Destination changed by JavaScript": ("JavaScript changes where this link goes",
                                                     "Make the href in the HTML point to the final destination")}
    for label, src, dest, anchor in adv.get("render_links", []):
        st, depth, n_in = target_info(dest)
        data.append([label, src, dest, anchor, "", st, depth, n_in, actions[label][0], actions[label][1]])
    first, last = _table(ws, "InternalLinks", ["Edge Type", "Source URL", "Destination URL", "Anchor Text", "Follow", "Target Status",
                                                  "Click Depth", "Inlinks", "Issue", "Recommended Action"],
                            data, [26, 50, 50, 28, 12, 20, 10, 8, 30, 55], wrap=(9, 10), empty_note="No links were found.")
    colour_prefix(ws, 9, first, last, {"Broken": "FDE2E2", "Redirect": "FEF6C7", "Orphan": "FEF6C7", "Deep": "FEF6C7",
                                       "Not reachable": "FDE2E2", "Links to": "FEF6C7", "Weak": "DCEBFE", "Couldn't verify": "EEF0F3",
                                       "Anchors": "DCEBFE", "Only in the rendered": "FEF6C7", "JavaScript changes": "FEF6C7"})
    finish_sheet(ws, 6)


# ---------------------------------------------------------------- 08 Images & Speed

def _images_speed(wb, m):
    ws = wb.create_sheet("08 Images & Speed")
    title_block(ws, "Images & Speed", "Images over 200 KB or without alt text, page by page, and real-user Core Web Vitals when a "
                                      "CrUX key was given. 'Not measured' is never zero.", _meta(m))
    res = getattr(m.post, "res", {}) or {}
    measured = {}
    for key, v in res.items():
        if key.startswith("img|") and v.get("s"):
            measured[key[4:]] = round(v["s"] / 1024)
    data = []
    for r in m.rows:
        heavy = dict(re.findall(r"(https?://\S+?) \(([\d.]+ (?:KB|MB))\)", str(r.get("Heavy Images (200 KB+)", "") or "")))
        alt_missing = evidence_urls(r.get("Images Missing Alt (URLs)"))
        cwv = str(r.get("Core Web Vitals", "") or "")
        cwv_src = "CrUX (real users, phone)" if cwv in ("Good", "Needs improvement", "Poor") else NOT_MEASURED
        for u in list(dict.fromkeys(list(heavy) + alt_missing)):
            size_txt = heavy.get(u)
            kb = None
            if size_txt:
                num, unit = size_txt.split()
                kb = round(float(num) * (1024 if unit == "MB" else 1))
            elif norm(u) in measured:
                kb = measured[norm(u)]
            problems, fixes = [], []
            if u in heavy:
                problems.append("Heavy (%s)" % heavy[u])
                fixes.append("Compress it and serve WebP/AVIF at the displayed size")
            if u in alt_missing:
                problems.append("Missing alt text")
                fixes.append("Add alt text that describes the image (empty alt for decoration)")
            ext = re.search(r"\.([a-z0-9]{3,4})(?:\?|$)", u.lower())
            data.append([r["URL"], u, "Missing" if u in alt_missing else "Present", kb if kb is not None else NOT_MEASURED,
                         "Yes" if kb is not None else "No", ext.group(1).upper() if ext else "", NOT_MEASURED, cwv_src,
                         "; ".join(problems), "; ".join(fixes)])
        if cwv_src != NOT_MEASURED:
            vals = ", ".join(x for x in ("LCP %s s" % r["LCP (s)"] if r.get("LCP (s)") not in (None, "") else "",
                                         "INP %s ms" % r["INP (ms)"] if r.get("INP (ms)") not in (None, "") else "",
                                         "CLS %s" % r["CLS"] if r.get("CLS") not in (None, "") else "") if x)
            data.append([r["URL"], "", "", "", "", "", "", cwv_src, "Core Web Vitals: %s (%s)" % (cwv, vals),
                         "" if cwv == "Good" else "Improve the worst metric: LCP (hero image, server), INP (less JavaScript), CLS (reserve space)"])
    first, last = _table(ws, "ImagesSpeed", ["Page URL", "Image URL", "Alt Text", "Image Size (KB)", "Measured?", "Format", "Loading",
                                                "CWV Source", "Issue", "Fix"],
                            data, [50, 60, 10, 11, 10, 8, 12, 22, 36, 55], wrap=(9, 10),
                            empty_note="No images over 200 KB or without alt text were found.")
    colour_prefix(ws, 9, first, last, {"Heavy": "FDE2E2", "Missing alt": "FEF6C7", "Core Web Vitals: Poor": "FDE2E2",
                                       "Core Web Vitals: Needs": "FEF6C7", "Core Web Vitals: Good": "E8F6EB"})
    finish_sheet(ws, 7, freeze="B7")


# ---------------------------------------------------------------- 09 Schema & Social

SCHEMA_SOCIAL = {"No structured data", "Structured data has errors", "Structured data missing key properties", "Incomplete Open Graph tags"}


def _schema_social(wb, m):
    ws = wb.create_sheet("09 Schema & Social")
    title_block(ws, "Schema & Social", "Structured data and social sharing tags. 'Parse Status' means the JSON-LD could be read; it is "
                                       "not a Google rich-result validation.", _meta(m))
    template = {t.rule for t in m.tasks if t.template}
    from .checks import fix_text
    data = []
    for r in m.rows:
        if str(r.get("Meta Title", "")).startswith("N/A") or r.get("Final Status") != 200:
            continue
        og = str(r.get("Open Graph", "") or "")
        missing_og = [x.strip() for x in og.split(":", 1)[1].split(",")] if og.startswith("Missing") and ":" in og else []
        no_og = og in ("", "None", "Missing")
        errs = str(r.get("Schema Parse Errors", "") or "0")
        types = _clean(r.get("Schema Types", ""))
        rules = [i for i in page_issues(r) if i in SCHEMA_SOCIAL]
        data.append([r["URL"], types or "None found",
                     "No JSON-LD" if not types else ("Parsed" if errs in ("0", "") else "%s parse error%s" % (errs, "" if errs == "1" else "s")),
                     _clean(r.get("Schema Missing Properties", "")) or "None",
                     "Missing" if no_og or "og:title" in missing_og else "Present",
                     "Missing" if no_og or "og:image" in missing_og else "Present",
                     _clean(r.get("Twitter Card", "")) or "Missing",
                     ("Template" if any(x in template for x in rules) else "Page") if rules else "",
                     "; ".join("%s: %s" % (x, fix_text(x)) for x in rules)])
    first, last = _table(ws, "SchemaSocial", ["URL", "Schema Types", "Parse Status", "Missing Properties", "OG Title", "OG Image",
                                                 "Twitter Card", "Fix Scope", "Recommendation"],
                            data, [55, 40, 14, 40, 10, 10, 18, 10, 60], wrap=(2, 4, 9), empty_note="No HTML pages were audited.")
    colour_values(ws, 5, first, last, {"Missing": "FEF6C7"})
    colour_values(ws, 6, first, last, {"Missing": "FEF6C7"})
    colour_prefix(ws, 3, first, last, {"No JSON-LD": "FEF6C7", "1 ": "FDE2E2", "2": "FDE2E2"})
    finish_sheet(ws, 8, freeze="B7")


# ---------------------------------------------------------------- 10 Crawl Coverage

def _crawl_coverage(wb, m):
    ws = wb.create_sheet("10 Crawl Coverage")
    title_block(ws, "Crawl Coverage", "What was discovered, audited and checked, and what wasn't. Each URL is counted once, under the "
                                      "source that found it first; percentages only where the total is known.", _meta(m))
    data = [[c[0]] + [NOT_MEASURED if v is None else v for v in c[1:4]] + [NOT_MEASURED if c[4] is None else c[4], c[5], c[6]]
            for c in m.coverage]
    first, last = _table(ws, "CrawlCoverage", ["Discovery Source", "Discovered", "Audited", "Not Audited", "Coverage %", "Reason",
                                                  "Next Step"],
                            data, [34, 12, 12, 12, 11, 60, 45], wrap=(6, 7))
    for r in range(first, last + 1):
        if isinstance(ws.cell(row=r, column=5).value, float):
            ws.cell(row=r, column=5).number_format = "0%"
    finish_sheet(ws, 9)


# ---------------------------------------------------------------- 11 Raw & Audit Info + hidden raw sheets

def _raw_and_info(wb, m, problems, ai_out, raw=True):
    ws = wb.create_sheet("11 Raw & Audit Info")
    title_block(ws, "Raw & Audit Info", "How this audit was run, the rules behind the numbers, and where the full raw data is.", _meta(m))
    s, c = m.settings, m.crawler
    secs = int((m.finished - m.started).total_seconds())
    from .net import BOT_UA
    info = [
        ("Website", m.host, "", "Crawl"), ("Audit type", m.audit_type, "", "Crawl settings"), ("Run ID", m.run_id, "", "SEO Lens"),
        ("Started", m.started.strftime("%d %b %Y, %H:%M:%S"), "In the time zone of the person who started the audit", "Crawl"),
        ("Finished", m.finished.strftime("%d %b %Y, %H:%M:%S"), "", "Crawl"),
        ("Duration", "%d min %d s" % (secs // 60, secs % 60), "%d HTTP requests" % c.fetcher.count, "Crawl"),
        ("Pages audited", len(m.rows), "of %d discovered URLs" % len(m.queue), "Crawl"),
        ("Maximum URLs", s.get("max_pages"), "", "Crawl settings"),
        ("Speed", s.get("speed", "Medium"), "Ended at %d pages at a time%s" % (c.conc, "; slowed down %d times: %s" % (c.slowdowns, c.slow_reason)
                                                                           if c.slowdowns else ""), "Crawl"),
        ("JavaScript comparison", {"off": "Off (HTML only)", "auto": "Selective", "all": "All"}.get(s.get("render_js", "off"), s.get("render_js")),
         (getattr(c, "render_note", "") or ("%d pages rendered" % getattr(c, "rendered", 0))) +
         ("" if s.get("render_js", "off") == "off" else " · limit %s pages" % s.get("render_max", 100)), "Crawl settings"),
        ("Main-content review threshold", "%s words" % s.get("content_min", 300),
         "Half for the homepage, product, shop and about pages; contact, gallery, legal and utility pages and archives are not judged",
         "Crawl settings"),
        ("URL length review threshold", "%s characters" % s.get("url_max", 115), "A review signal, not an error", "Crawl settings"),
        ("URLs with ?parameters", "Crawled" if s.get("include_query") or m.mode == "list" else "Not crawled (analysed from links and the sitemap)",
         "Parameters are never stripped: a parameter URL is a separate URL", "Crawl settings"),
        ("robots.txt", "Respected" if s.get("respect_robots", True) else "Ignored (site owner's choice)", "", "Crawl settings"),
        ("External links", "Checked" if s.get("check_external", True) else "Not checked", "Up to %d URLs" % MAX_EXTERNAL_CHECKS, "Crawl settings"),
        ("Core Web Vitals", "CrUX (real users)" if any(r.get("Core Web Vitals") for r in m.rows) else NOT_MEASURED,
         "Needs a CrUX API key" if not any(r.get("Core Web Vitals") for r in m.rows) else "", "Crawl settings"),
        ("User-Agent", BOT_UA, "SEO Lens always identifies itself", "SEO Lens"),
        ("SEO Lens version", __version__, "", "SEO Lens"),
        ("Health score", m.score, "Average page score %d minus site-wide penalties %d (4 per failed, 1 per warning site check, at most 25)"
         % (m.score_avg, m.score_penalty), "Computed"),
        ("Severity", "Critical · High · Medium · Low", "Fixed per rule (how bad one finding is)", "SEO Lens rules"),
    ] + [("Priority %s" % p, text, "Task priority (what to do first), separate from severity", "SEO Lens rules") for p, text in PRIORITY_RULES] + [
        ("Template fix", "80% or more of HTML pages (and 20+ pages)", "Then one fix usually repairs every page", "SEO Lens rules"),
        ("Thresholds", "Thin content: below the main-content threshold for the page type · Heavy image: 200 KB+ · Deep page: 4+ clicks · "
                       "Title 30–60 and description 70–160 characters · Deep URL path: more than 5 folders", "", "SEO Lens rules"),
        ("Check limits", "%d internal link targets · %d external URLs · %d image sizes" % (MAX_INTERNAL_CHECKS, MAX_EXTERNAL_CHECKS, MAX_IMAGE_CHECKS),
         "The most-linked or most-used are checked first", "SEO Lens rules"),
        ("Owner and Status", "Owner is blank and Status is 'To Do' on every new report", "SEO Lens never assigns owners; edits in an old "
                                                                                          "workbook are not carried over", "SEO Lens"),
        ("Integrity checks", "Passed" if not problems else "Failed: " + "; ".join(problems),
         "Every issue links to a task, every task to its issues, the dashboard counts match the tables", "Computed"),
        ("Raw data", "Hidden sheets: __Raw Pages, __Raw Links, __Raw Checks, __Raw Discovery",
         "Right-click any sheet tab → Unhide to see them: every collected page field, link check, site check and discovered "
         "URL. Every issue record is on sheet 03.", "SEO Lens"),
    ]
    if ai_out:
        info.append(("AI suggestions", "%d rows" % len(ai_out.get("rows", [])), "", "AI assistant"))
    first, last = _table(ws, "AuditInfo", ["Setting", "Value", "Notes", "Source"], info, [24, 60, 70, 16], wrap=(2, 3))
    finish_sheet(ws, 10)

    if not raw:
        return
    _raw_sheet(wb, "__Raw Pages", "RawPages", [c_ for c_ in COLUMNS if c_ not in HIDDEN_COLUMNS],
               [[_clean(r.get(c_, "")) for c_ in COLUMNS if c_ not in HIDDEN_COLUMNS] for r in m.rows])
    _raw_sheet(wb, "__Raw Links", "RawLinks", ["Found On Page", "Pages", "Link URL", "Anchor Text", "Where", "Type", "Status", "Result",
                                               "How to Fix"], [list(b)[:9] for b in m.broken_rows])
    _raw_sheet(wb, "__Raw Checks", "RawChecks", ["Status", "Priority", "Category", "Check", "Result", "Recommendation"],
               [[c_["status"], c_["priority"], c_["category"], c_["check"], c_["result"], c_["fix"]] for c_ in m.site_checks])
    _raw_sheet(wb, "__Raw Discovery", "RawDiscovery", ["URL", "Found Via", "Status", "In Sitemap"],
               [[u, src, ("Audited" if m.access.get(i) == "OK" else ("Refused: " + m.access[i]) if i in m.access
                          else "Not audited (over the URL limit)" if i >= (s.get("max_pages") or 0) else "Not audited (crawl ended early)"),
                 ("Yes" if src == "Sitemap" or norm(u) in m.sitemap_keys else "No") if m.has_sitemap else "No sitemap found"]
                for i, (u, src) in enumerate(m.queue)])


def _raw_sheet(wb, title, table, headers, rows):
    ws = wb.create_sheet(title)
    add_table(ws, table, headers, rows, [40] * len(headers), top=1, empty_note="No records.")
    ws.freeze_panes = "A2"
    ws.sheet_state = "hidden"


# ---------------------------------------------------------------- integrity

def validate(m):
    """Cross-checks between the sheets. Returns a list of problems (empty = all good)."""
    problems = []
    task_ids = {t.task_id for t in m.tasks}
    issue_ids = {i.issue_id for i in m.issues}
    if any(i.task_id not in task_ids for i in m.issues):
        problems.append("an issue points to a missing task")
    if any(not set(t.issue_ids) <= issue_ids for t in m.tasks):
        problems.append("a task points to a missing issue")
    for t in m.tasks:
        n = sum(1 for i in m.issues if i.task_id == t.task_id and i.scope == "Page")
        if t.affected and n != t.affected:
            problems.append("task %s counts %d URLs but has %d records" % (t.task_id, t.affected, n))
    if len(issue_ids) != len(m.issues):
        problems.append("duplicate issue IDs")
    sev = Counter(i.severity for i in m.issues if i.scope == "Page")
    if sum(sev.values()) != sum(1 for i in m.issues if i.scope == "Page") or any(k not in SEVERITIES for k in sev):
        problems.append("severity counts don't add up")
    return problems
