"""Sheet 01 Dashboard: KPI cards, two native Excel charts (issue distribution, severity), top priorities, audit
completeness and the score method. Layout B:S (six 3-column cards); chart data in cells to the right (U:Z)."""
from collections import Counter

from openpyxl.chart import BarChart, DoughnutChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.series import DataPoint
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from .report_style import (BOX, DARK_GREEN, FONT, GREEN, INK, LIGHT_BG, MUTED, NAVY, PALE_BLUE, PALE_GREEN, PALE_ORANGE,
                           PALE_PURPLE, PRIORITY_PALE, SEVERITY, fill, finish_sheet, put)

SCHEMA_RULES = {"No structured data", "Structured data has errors", "Structured data missing key properties"}
CARD_FILLS = [PALE_GREEN, PALE_BLUE, PALE_PURPLE, PALE_ORANGE, PALE_BLUE, PALE_GREEN]


def _value_labels():
    """Data labels that show the number only (not the series and category names)."""
    lab = DataLabelList()
    lab.showVal, lab.showSerName, lab.showCatName, lab.showLegendKey, lab.showPercent = True, False, False, False, False
    return lab


def _merge_text(ws, rng, value, font=None, fill_hex=None, align=None):
    ws.merge_cells(rng)
    first = rng.split(":")[0]
    cell = ws[first]
    cell.value = value
    if isinstance(value, str) and value[:1] in "=+-@":
        cell.data_type = "s"
    if font:
        cell.font = font
    if fill_hex:
        cell.fill = fill(fill_hex)
    cell.alignment = align or Alignment(wrap_text=True, vertical="top")
    return cell


def add_dashboard(wb, m):
    ws = wb.create_sheet("01 Dashboard")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 2
    for c in range(2, 20):  # B:S
        ws.column_dimensions[get_column_letter(c)].width = 9.5
    ws.column_dimensions["T"].width = 3

    # Title and run details
    _merge_text(ws, "B2:I3", "SEO Lens  |  SEO Audit Dashboard", Font(name=FONT, size=20, bold=True, color=DARK_GREEN),
                align=Alignment(vertical="center"))
    when = m.finished.strftime("%d %b %Y, %H:%M")
    _merge_text(ws, "J2:S3", "%s\n%s · %s · run %s" % (m.host or "URL list", when, m.audit_type, m.run_id),
                Font(name=FONT, size=10, color=INK), align=Alignment(wrap_text=True, vertical="center", horizontal="right"))

    # KPI cards (six, 3 columns each)
    rows, page_issues = m.rows, [i for i in m.issues if i.scope == "Page"]
    site_findings = [i for i in m.issues if i.scope == "Site"]
    indexable = sum(1 for r in rows if r.get("Indexable") == "Yes")
    schema_pages = len({i.url for i in page_issues if i.rule in SCHEMA_RULES})
    thin_pages = len({i.url for i in page_issues if i.rule == "Thin content"})
    grade = "Good" if m.score >= 80 else "Needs improvement" if m.score >= 60 else "Poor"
    cards = [("Health Score", m.score, '0" / 100"', grade),
             ("Pages Crawled", len(rows), "#,##0", "of %s discovered URLs" % format(len(m.queue), ",")),
             ("Indexable Pages", indexable, "#,##0", "%d%% of pages crawled" % round(100 * indexable / len(rows)) if rows else ""),
             ("Issue–URL Records", len(page_issues), "#,##0",
              "%d rules · %d site-wide finding%s" % (len({i.rule for i in page_issues}), len(site_findings), "" if len(site_findings) == 1 else "s")),
             ("Pages With Schema Issues", schema_pages, "#,##0", "of %d HTML pages" % m.eligible),
             ("Pages Flagged for Thin Content", thin_pages, "#,##0", "below the main-content threshold for the page type")]
    for k, (label, value, fmt, note) in enumerate(cards):
        c1, c3 = get_column_letter(2 + 3 * k), get_column_letter(4 + 3 * k)
        card_fill = CARD_FILLS[k]
        _merge_text(ws, "%s5:%s5" % (c1, c3), label, Font(name=FONT, size=10, bold=True, color=MUTED), card_fill,
                    Alignment(vertical="center", horizontal="left", indent=1))
        cell = _merge_text(ws, "%s6:%s8" % (c1, c3), value, Font(name=FONT, size=26, bold=True, color=DARK_GREEN), card_fill,
                           Alignment(vertical="center", horizontal="left", indent=1))
        cell.number_format = fmt
        _merge_text(ws, "%s9:%s9" % (c1, c3), note, Font(name=FONT, size=9, color=MUTED), card_fill,
                    Alignment(vertical="center", horizontal="left", indent=1, wrap_text=True))
    if m.score < 60:
        ws["B6"].font = Font(name=FONT, size=26, bold=True, color=SEVERITY["Critical"])
    elif m.score < 80:
        ws["B6"].font = Font(name=FONT, size=26, bold=True, color="B45309")
    ws.row_dimensions[9].height = 24

    # Chart data (cells, so the charts stay live and checkable): U:W issue distribution, Y:Z severity
    data_font = Font(name=FONT, size=9, color=MUTED)
    put(ws, 11, 21, "Rule").font = data_font
    put(ws, 11, 22, "Affected URLs").font = data_font
    put(ws, 11, 23, "%% of %d HTML pages" % m.eligible).font = data_font
    per_rule = Counter()
    for i in page_issues:
        per_rule[i.rule] += 1  # one record per URL per rule, so this is the unique URLs per rule
    top = per_rule.most_common(10)
    for k, (rule, n) in enumerate(reversed(top)):  # reversed: the biggest bar ends up on top
        put(ws, 12 + k, 21, rule).font = data_font
        put(ws, 12 + k, 22, n).font = data_font
        cell = put(ws, 12 + k, 23, (n / m.eligible) if m.eligible else None)
        cell.font, cell.number_format = data_font, "0%"
    put(ws, 11, 25, "Severity").font = data_font
    put(ws, 11, 26, "Issue–URL records").font = data_font
    sev = Counter(i.severity for i in page_issues)
    for k, level in enumerate(["Critical", "High", "Medium", "Low"]):
        put(ws, 12 + k, 25, level).font = data_font
        put(ws, 12 + k, 26, sev.get(level, 0)).font = data_font
    put(ws, 17, 25, "Site-wide findings (separate)").font = data_font
    put(ws, 17, 26, len(site_findings)).font = data_font
    for col, w in (("U", 34), ("V", 11), ("W", 11), ("X", 3), ("Y", 22), ("Z", 12)):
        ws.column_dimensions[col].width = w

    _merge_text(ws, "B11:J11", "Issue distribution", Font(name=FONT, size=12, bold=True, color=NAVY), align=Alignment(vertical="center"))
    if top:
        bar = BarChart()
        bar.type, bar.style, bar.legend = "bar", 10, None
        bar.y_axis.title = None
        bar.x_axis.delete = False
        bar.y_axis.delete = True
        bar.y_axis.majorGridlines = None
        bar.add_data(Reference(ws, min_col=22, min_row=11, max_row=11 + len(top)), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=21, min_row=12, max_row=11 + len(top)))
        bar.series[0].graphicalProperties.solidFill = GREEN
        bar.series[0].graphicalProperties.line.solidFill = GREEN
        bar.dataLabels = _value_labels()
        bar.gapWidth = 60
        bar.height, bar.width = 7.6, 15.6
        ws.add_chart(bar, "B12")
    else:
        put(ws, 12, 2, "No page-level issues found.").font = Font(name=FONT, size=10, color=MUTED)
    put(ws, 28, 2, "Bars: unique URLs per rule (top %d). Share = of %d HTML pages that returned 200." % (len(top), m.eligible)).font = \
        Font(name=FONT, size=9, italic=True, color=MUTED)

    _merge_text(ws, "K11:O11", "Severity", Font(name=FONT, size=12, bold=True, color=NAVY), align=Alignment(vertical="center"))
    if page_issues:
        dn = DoughnutChart()
        dn.add_data(Reference(ws, min_col=26, min_row=11, max_row=15), titles_from_data=True)
        dn.set_categories(Reference(ws, min_col=25, min_row=12, max_row=15))
        for k, level in enumerate(["Critical", "High", "Medium", "Low"]):
            pt = DataPoint(idx=k)
            pt.graphicalProperties.solidFill = SEVERITY[level]
            pt.graphicalProperties.line.solidFill = "FFFFFF"
            dn.series[0].dPt.append(pt)
        dn.dataLabels = _value_labels()
        dn.holeSize = 55
        dn.legend.position = "b"
        dn.height, dn.width = 7.6, 8.4
        ws.add_chart(dn, "K12")
    put(ws, 28, 11, "Issue–URL records by severity. %d site-wide finding%s counted separately." % (
        len(site_findings), "" if len(site_findings) == 1 else "s")).font = Font(name=FONT, size=9, italic=True, color=MUTED)

    # Top priorities: the five highest-ranked tasks
    _merge_text(ws, "P11:S11", "Top priorities", Font(name=FONT, size=12, bold=True, color=NAVY), align=Alignment(vertical="center"))
    for k, t in enumerate(m.tasks[:5]):
        r = 12 + 3 * k
        put(ws, r, 16, t.priority).font = Font(name=FONT, size=11, bold=True, color=INK)
        ws.cell(row=r, column=16).fill = fill(PRIORITY_PALE[t.priority])
        ws.cell(row=r, column=16).alignment = Alignment(horizontal="center", vertical="center")
        _merge_text(ws, "Q%d:S%d" % (r, r), t.rule, Font(name=FONT, size=10, bold=True, color=INK), align=Alignment(vertical="center", wrap_text=True))
        what = ("%d URL%s · %s" % (t.affected, "" if t.affected == 1 else "s", t.fix_scope)) if t.affected else t.fix_scope
        fix_short = t.fix if len(t.fix) <= 95 else t.fix[:95].rsplit(" ", 1)[0] + "…"
        _merge_text(ws, "P%d:S%d" % (r + 1, r + 2), "%s\n%s" % (what, fix_short), Font(name=FONT, size=9, color=MUTED))
        for rr in (r, r + 1, r + 2):
            ws.row_dimensions[rr].height = 17
    if not m.tasks:
        put(ws, 12, 16, "Nothing to fix: no issues found.").font = Font(name=FONT, size=10, color=MUTED)

    # Audit completeness and score method
    notes = m.notices or ["Every planned check ran: no limits reached, nothing refused."]
    _merge_text(ws, "B30:S33", "Audit completeness\n" + "\n".join("• " + n for n in notes),
                Font(name=FONT, size=10, color=INK), LIGHT_BG)
    for r in range(30, 34):
        ws.row_dimensions[r].height = max(15, 15 * (len(notes) + 1) / 4)
    _merge_text(ws, "B35:S37",
                "Health score: average page score (%d) minus site-wide penalties (%d; 4 per failed and 1 per warning site check, "
                "at most 25) = %d / 100.  Severity is per rule (Critical, High, Medium, Low). Priority P0–P3 ranks the tasks: the rules "
                "are on sheet 11." % (m.score_avg, m.score_penalty, m.score), Font(name=FONT, size=9, color=MUTED), LIGHT_BG)
    # Advanced technical checks (six cards, same grid as the KPI cards)
    adv = (getattr(m.post, "advanced", None) or {}).get("counts", {})
    _merge_text(ws, "B39:S39", "Advanced technical checks", Font(name=FONT, size=12, bold=True, color=NAVY), align=Alignment(vertical="center"))
    rendered_on = m.settings.get("render_js", "off") != "off"
    acards = [("Parameter URLs", adv.get("param_urls", 0), "found in links, sitemap or crawl (sheet 06)"),
              ("Canonical Conflicts", adv.get("canonical_conflicts", 0), "in %d canonical cluster%s (sheet 06)" % (
                  adv.get("canonical_clusters", 0), "" if adv.get("canonical_clusters", 0) == 1 else "s")),
              ("Indexability Conflicts", adv.get("indexability_conflicts", 0), "pages and sitemap URLs with mixed signals"),
              ("JS Rendering Differences", adv.get("render_diff") if rendered_on and adv.get("render_diff") is not None else "Not measured",
               ("of %d pages rendered" % adv.get("rendered", 0)) if rendered_on else "rendering was off (installed version only)"),
              ("Pagination Series", adv.get("pagination_series", 0), "paginated lists found"),
              ("Anchor Text Findings", adv.get("anchor_findings", 0), "linked pages with weak contextual anchors")]
    for k, (label, value, note) in enumerate(acards):
        c1, c3 = get_column_letter(2 + 3 * k), get_column_letter(4 + 3 * k)
        _merge_text(ws, "%s40:%s40" % (c1, c3), label, Font(name=FONT, size=10, bold=True, color=MUTED), LIGHT_BG,
                    Alignment(vertical="center", horizontal="left", indent=1))
        cell = _merge_text(ws, "%s41:%s42" % (c1, c3), value,
                           Font(name=FONT, size=20 if isinstance(value, int) else 12, bold=True, color=DARK_GREEN), LIGHT_BG,
                           Alignment(vertical="center", horizontal="left", indent=1))
        cell.number_format = "#,##0"
        _merge_text(ws, "%s43:%s43" % (c1, c3), note, Font(name=FONT, size=9, color=MUTED), LIGHT_BG,
                    Alignment(vertical="center", horizontal="left", indent=1, wrap_text=True))
    ws.row_dimensions[43].height = 24
    for rng in ("B30:S33", "B35:S37"):
        for row in ws[rng]:
            for cell in row:
                cell.border = BOX
    finish_sheet(ws, 0, freeze=None)
    return ws
