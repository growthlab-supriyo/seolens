"""Look of the Excel report: palette, tab colours, title blocks, filterable tables, conditional formatting.

Styling is applied per column or per range (tables, conditional formatting), never cell by cell over the data, so a
10,000-page report stays light.
"""
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# Palette (from the redesign guideline)
DARK_GREEN, GREEN, PALE_GREEN = "075633", "087443", "E8F6EB"
NAVY, INK, LIGHT_BG = "203A60", "172033", "F8FAFC"
PALE_BLUE, PALE_PURPLE, PALE_ORANGE = "E3F2FD", "F1E8FA", "FFF0DC"
MUTED, LINE = "5B6475", "D7DCE3"
SEVERITY = {"Critical": "DC2626", "High": "F97316", "Medium": "EAB308", "Low": "3B82F6"}
SEVERITY_PALE = {"Critical": "FDE2E2", "High": "FFEAD5", "Medium": "FEF6C7", "Low": "DCEBFE"}
PRIORITY_PALE = {"P0": "FDE2E2", "P1": "FFEAD5", "P2": "FEF6C7", "P3": "DCEBFE"}
FONT = "Calibri"

# Tab colours, in sheet order: green / purple / blue / orange / light blue / blue / blue / purple / blue / purple / grey
TAB_COLOURS = ["087443", "7C3AED", "2563EB", "F97316", "60A5FA", "2563EB", "2563EB", "7C3AED", "2563EB", "7C3AED", "6B7280"]

CELL_LIMIT = 32000  # Excel holds 32,767 characters per cell
WRAP_LIMIT = 3000   # tables longer than this skip per-cell text wrapping (it is the slowest part of writing big sheets)
THIN = Side(style="thin", color=LINE)
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)


def fill(hex_colour):
    return PatternFill("solid", fgColor=hex_colour)


def put(ws, row, col, value):
    """Writes a value as data, never as a formula: text from websites can start with = + - @ (formula injection)."""
    if isinstance(value, str):
        if len(value) > CELL_LIMIT:
            value = value[:CELL_LIMIT] + " …"
        value = value.replace("\x00", "")
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value[:1] in "=+-@":
        cell.data_type = "s"  # keep it text
    return cell


def title_block(ws, title, subtitle, meta):
    """Rows 1-2: title and subtitle. Row 4: website, audit date, run and scope. Gridlines off."""
    ws.sheet_view.showGridLines = False
    put(ws, 1, 1, title).font = Font(name=FONT, size=18, bold=True, color=DARK_GREEN)
    put(ws, 2, 1, subtitle).font = Font(name=FONT, size=10, color=MUTED)
    put(ws, 4, 1, meta).font = Font(name=FONT, size=10, bold=True, color=INK)
    ws.row_dimensions[1].height = 26


def add_table(ws, name, headers, rows, widths, top=6, wrap=(), number_formats=None, empty_note="Nothing to list."):
    """A filterable Excel table: header on row `top` (6), records from top+1. Returns (first data row, last data row)."""
    for c, h in enumerate(headers, 1):
        cell = put(ws, top, c, h)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[top].height = 30
    if not rows:
        rows = [[empty_note] + [""] * (len(headers) - 1)]
    if ws.max_row == top:  # fast path: append whole rows (the table is the last thing on the sheet so far)
        text_cells = []
        for i, row in enumerate(rows, top + 1):
            clean = []
            for c, v in enumerate(row, 1):
                if isinstance(v, str):
                    if len(v) > CELL_LIMIT:
                        v = v[:CELL_LIMIT] + " …"
                    if v[:1] in "=+-@":
                        text_cells.append((i, c))
                clean.append(v)
            ws.append(clean)
        for i, c in text_cells:
            ws.cell(row=i, column=c).data_type = "s"  # text from websites, never a formula
    else:
        for i, row in enumerate(rows, top + 1):
            for c, v in enumerate(row, 1):
                put(ws, i, c, v)
    last = top + len(rows)
    t = Table(displayName=name, ref="A%d:%s%d" % (top, get_column_letter(len(headers)), last))
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    ws.add_table(t)
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    for c in range(1, len(headers) + 1):  # one style per column, not per cell
        dim = ws.column_dimensions[get_column_letter(c)]
        dim.alignment = WRAP if c in wrap else TOP
    if len(rows) <= WRAP_LIMIT:  # wrapping is set per cell, so it's skipped on very large tables (the text is all there)
        for c in wrap:
            for r in range(top + 1, last + 1):
                ws.cell(row=r, column=c).alignment = WRAP
    for c, fmt in (number_formats or {}).items():
        for r in range(top + 1, last + 1):
            ws.cell(row=r, column=c).number_format = fmt
    return top + 1, last


def colour_values(ws, col, first, last, colours):
    """Conditional formatting: colour a column's cells by their value (one rule per value, applied to the whole range)."""
    letter = get_column_letter(col)
    rng = "%s%d:%s%d" % (letter, first, letter, last)
    for value, hex_colour in colours.items():
        ws.conditional_formatting.add(rng, FormulaRule(formula=['$%s%d="%s"' % (letter, first, value)], fill=fill(hex_colour),
                                                       stopIfTrue=True))


def colour_prefix(ws, col, first, last, colours):
    """Conditional formatting by how the text starts (e.g. 'Poor', 'Missing')."""
    letter = get_column_letter(col)
    rng = "%s%d:%s%d" % (letter, first, letter, last)
    for prefix, hex_colour in colours.items():
        ws.conditional_formatting.add(rng, FormulaRule(formula=['LEFT($%s%d,%d)="%s"' % (letter, first, len(prefix), prefix)],
                                                       fill=fill(hex_colour), stopIfTrue=True))


def finish_sheet(ws, index, freeze="A7"):
    ws.sheet_properties.tabColor = TAB_COLOURS[index]
    ws.freeze_panes = freeze
