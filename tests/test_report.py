"""The 11-sheet Excel report (redesign guideline): structure, data parity, matching counts, links between sheets."""
import re
from collections import Counter

import pytest
from openpyxl import Workbook, load_workbook

import seolens.crux as crux
from seolens.audit import run_audit
from seolens.report_style import put
from seolens.store import Store
from testsite import make_server

SHEETS = ["01 Dashboard", "02 Action Plan", "03 Issue Details", "04 Page Audit", "05 On-Page SEO", "06 Technical SEO",
          "07 Internal Links", "08 Images & Speed", "09 Schema & Social", "10 Crawl Coverage", "11 Raw & Audit Info"]
HIDDEN = ["__Raw Pages", "__Raw Links", "__Raw Checks", "__Raw Discovery"]
REMOVED = ["Author", "Published / Modified", "Date Check", "JS-Rendering Risk", "Scripts", "Performance Risk",
           "Images Missing Width/Height (URLs)", "Contact Signals"]


@pytest.fixture()
def site():
    srv = make_server(pages=30)
    yield "http://127.0.0.1:%d/" % srv.server_address[1]
    srv.shutdown()


def audit(tmp_path, **settings):
    store = Store(tmp_path / "t.db")
    s = dict({"mode": "domain", "max_pages": 100, "speed": "Medium", "render_js": "off", "check_external": False,
              "out_dir": str(tmp_path)}, **settings)
    rid = store.create_run(s)
    res = run_audit(store.run(rid)["settings"], store=store, run_id=rid)
    return load_workbook(res["xlsx"]), res, store.page_rows(rid)


def table_rows(ws, top=6):
    """Records of the sheet's table (header on row `top`), as dicts."""
    headers = [c.value for c in ws[top]]
    out = []
    for row in ws.iter_rows(min_row=top + 1, values_only=True):
        if any(v not in (None, "") for v in row):
            out.append(dict(zip(headers, row)))
    return out


def test_sheets_order_tables_and_charts(site, tmp_path):
    wb, res, rows = audit(tmp_path, site=site)
    visible = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
    hidden = [ws.title for ws in wb.worksheets if ws.sheet_state == "hidden"]
    assert visible == SHEETS
    assert hidden == HIDDEN
    assert len(wb["01 Dashboard"]._charts) == 2  # native Excel charts, not pictures
    for name in SHEETS[1:]:
        ws = wb[name]
        assert len(ws.tables) == 1, name
        ref = list(ws.tables.values())[0].ref
        assert ref.startswith("A6:"), (name, ref)  # filter header on row 6
        assert ws.freeze_panes in ("A7", "B7"), name
        assert ws.sheet_properties.tabColor is not None
    dv = wb["02 Action Plan"].data_validations.dataValidation
    assert dv and "In Progress" in dv[0].formula1
    text = [str(c.value) for ws in wb.worksheets for r in ws.iter_rows() for c in r if c.value not in (None, "")]
    assert not [t for t in text if t.startswith("N/A")]
    assert not [t for t in text if re.search(r"\bdemo\b|google sheets", t, re.I)]
    assert not [c for c in REMOVED if c in [x.value for x in wb["__Raw Pages"][1]]]


def test_counts_and_links_agree(site, tmp_path):
    wb, res, rows = audit(tmp_path, site=site)
    tasks = table_rows(wb["02 Action Plan"])
    issues = table_rows(wb["03 Issue Details"])
    task_ids = {t["Task ID"] for t in tasks}
    assert all(i["Task ID"] in task_ids for i in issues)  # every issue resolves to a task
    per_task = Counter(i["Task ID"] for i in issues if i["Scope"] == "Page")
    for t in tasks:
        if t["Affected URLs"] != "Site-wide":
            assert per_task[t["Task ID"]] == t["Affected URLs"]  # task URL count = its issue records
    assert all(t["Owner"] in (None, "") and t["Status"] == "To Do" and t["Verification"] == "Not checked" for t in tasks)
    page_issues = [i for i in issues if i["Scope"] == "Page"]
    assert all(i["URL"] for i in page_issues) and all(not i["URL"] for i in issues if i["Scope"] == "Site")
    assert len({(i["URL"], i["Rule"]) for i in page_issues}) == len(page_issues)  # one record per URL per rule
    dash = wb["01 Dashboard"]
    kpi = {dash.cell(row=5, column=2 + 3 * k).value: dash.cell(row=6, column=2 + 3 * k).value for k in range(6)}
    assert kpi["Issue–URL Records"] == len(page_issues)
    assert kpi["Pages Crawled"] == len(rows) == len(table_rows(wb["04 Page Audit"]))
    assert kpi["Health Score"] == res["overall"]
    severity = {dash.cell(row=r, column=25).value: dash.cell(row=r, column=26).value for r in range(12, 16)}
    assert severity == {k: Counter(i["Severity"] for i in page_issues).get(k, 0) for k in ("Critical", "High", "Medium", "Low")}
    assert sum(severity.values()) == len(page_issues)  # site-wide findings are not mixed in
    info = {r["Setting"]: r["Value"] for r in table_rows(wb["11 Raw & Audit Info"])}
    assert info["Integrity checks"] == "Passed"
    assert len(table_rows(wb["__Raw Pages"], top=1)) == len(rows)
    assert {r["Check"] for r in table_rows(wb["__Raw Checks"], top=1)}  # every site check kept, Pass included


def test_specialist_sheets(site, tmp_path):
    wb, res, rows = audit(tmp_path, site=site)
    imgs = table_rows(wb["08 Images & Speed"])
    assert {r["Image URL"].rsplit("/", 1)[1] for r in imgs if r["Image URL"]} == {"hero.jpg", "icon.png"}
    hero = next(r for r in imgs if str(r["Image URL"]).endswith("hero.jpg"))
    assert hero["Image Size (KB)"] == 300 and hero["Measured?"] == "Yes" and hero["Issue"].startswith("Heavy")
    icon = next(r for r in imgs if str(r["Image URL"]).endswith("icon.png"))
    assert icon["Alt Text"] == "Missing" and icon["CWV Source"] == "Not measured"
    links = table_rows(wb["07 Internal Links"])
    assert any(r["Edge Type"].startswith("Problem link") and r["Issue"] == "Broken" for r in links)
    assert any(r["Edge Type"] == "Page link summary" for r in links)
    cov = {r["Discovery Source"]: r for r in table_rows(wb["10 Crawl Coverage"])}
    assert cov["All sources (unique URLs)"]["Audited"] == len(rows)
    assert cov["External links"]["Reason"].startswith("Not measured")
    assert cov["Core Web Vitals (CrUX)"]["Reason"].startswith("Not measured")
    tech = table_rows(wb["06 Technical SEO"])
    assert any(r["HTTP Status"] == 404 for r in tech)
    onpage = table_rows(wb["05 On-Page SEO"])
    assert "Title Length (characters)" in onpage[0]


def test_url_list_audit_marks_link_graph_not_measured(site, tmp_path):
    urls = [site + "p/%d" % i for i in range(1, 6)]
    wb, res, rows = audit(tmp_path, mode="list", urls=urls)
    assert [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"] == SHEETS
    pages = table_rows(wb["04 Page Audit"])
    assert len(pages) == 5 and all(p["Click Depth"] == "Not measured" and p["Inlinks"] == "Not measured" for p in pages)
    cov = {r["Discovery Source"]: r for r in table_rows(wb["10 Crawl Coverage"])}
    assert cov["Click depth, inlinks, orphan pages"]["Reason"].startswith("Not measured")


def test_folder_audit_has_no_site_wide_checks(site, tmp_path):
    wb, res, rows = audit(tmp_path, site=site + "p/")
    checks = {r["Check"] for r in table_rows(wb["__Raw Checks"], top=1)}
    for site_wide in ("Homepage", "HTTP → HTTPS redirect", "www / non-www", "HSTS header", "404 handling"):
        assert site_wide not in checks
    info = {r["Setting"]: r["Value"] for r in table_rows(wb["11 Raw & Audit Info"])}
    assert info["Audit type"].startswith("Folder /p/")


def test_crux_core_web_vitals(site, tmp_path, monkeypatch):
    def fake(key, url=None, origin=None, session=None):
        if origin:
            return {"lcp": 2100, "inp": 150, "cls": 0.05}
        return {"lcp": 4800, "inp": 180, "cls": 0.02} if url.endswith("/p/1") else None
    monkeypatch.setattr(crux, "query", fake)
    wb, res, rows = audit(tmp_path, site=site, crux_key="test-key")
    speed = table_rows(wb["08 Images & Speed"])
    p1 = next(r for r in speed if r["Page URL"] == site + "p/1" and str(r["Issue"]).startswith("Core Web Vitals"))
    assert p1["CWV Source"].startswith("CrUX") and "Poor" in p1["Issue"] and "LCP 4.8 s" in p1["Issue"]
    checks = {r["Check"]: r for r in table_rows(wb["__Raw Checks"], top=1)}
    assert checks["Core Web Vitals (whole site, real users)"]["Status"] == "Pass"
    assert "Poor Core Web Vitals" in {r["Rule"] for r in table_rows(wb["03 Issue Details"])}


def test_report_time_uses_the_users_time_zone(site, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    wb, res, rows = audit(tmp_path, site=site, max_pages=5, tz="Asia/Kolkata")
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    assert now.strftime("%d %b %Y") in str(wb["01 Dashboard"]["J2"].value)


def test_text_from_websites_is_never_a_formula():
    wb = Workbook()
    ws = wb.active
    for k, v in enumerate(["=HYPERLINK(\"http://x\")", "+1+1", "-cmd", "@SUM(A1)", 42], 1):
        put(ws, k, 1, v)
    assert [ws.cell(row=k, column=1).data_type for k in range(1, 5)] == ["s"] * 4
    assert ws.cell(row=5, column=1).data_type == "n"
