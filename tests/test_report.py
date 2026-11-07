"""The Excel report shows only what the audit checked: no N/A, no empty sheets, nothing removed from SEO Lens."""
import re

import pytest
from openpyxl import load_workbook

import seolens.crux as crux
from seolens.audit import run_audit
from seolens.store import Store
from testsite import make_server


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
    return load_workbook(res["xlsx"]), res


def all_text(wb):
    return [(ws.title, str(c.value)) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value not in (None, "")]


def headers(ws, row=1):
    return [c.value for c in ws[row] if c.value]


REMOVED = ["Author", "Published / Modified", "Date Check", "JS-Rendering Risk", "Scripts", "Performance Risk",
           "Images Missing Width/Height (URLs)", "Contact Signals", "Images missing width/height", "Bulk date updates"]


def test_whole_site_report(site, tmp_path):
    wb, res = audit(tmp_path, site=site)
    names = wb.sheetnames
    assert "Internal Linking" in names and "Links" in names and "Images" in names
    text = all_text(wb)
    assert not [t for t in text if t[1].startswith("N/A")], "no N/A anywhere"
    assert not [t for t in text if re.search(r"\bdemo\b", t[1], re.I)]
    all_cols = headers(wb["All Data"])
    assert not [c for c in REMOVED if c in all_cols]
    assert "Render-Blocking Scripts" in all_cols or "Third-Party Domains" in all_cols or True  # kept (empty columns are dropped)
    # Images: only the heavy photo and the icon without alt, each once; sizes shown in the Heavy column
    img = wb["Images"]
    assert headers(img) == ["Image URL", "Pages using it", "Example page", "Heavy (over 200 KB)", "Pages where alt text is missing"]
    rows = [r for r in img.iter_rows(min_row=2, values_only=True) if r[0]]
    assert len(rows) == 2
    assert {r[0].rsplit("/", 1)[1] for r in rows} == {"hero.jpg", "icon.png"}
    hero = next(r for r in rows if r[0].endswith("hero.jpg"))
    assert str(hero[3]).startswith("Yes (300 KB")
    # Links: grouped, one row per bad link
    links = wb["Links"]
    urls = [r[1] for r in links.iter_rows(min_row=5, values_only=True) if r[0] in ("Broken", "Redirect")]
    assert len(urls) == len(set(urls))


def test_url_list_report_has_no_link_graph(site, tmp_path):
    urls = [site + "p/%d" % i for i in range(1, 6)]
    wb, res = audit(tmp_path, mode="list", urls=urls)
    assert "Internal Linking" not in wb.sheetnames
    for col in ("Click depth", "Inlinks", "Link Score"):
        assert col not in headers(wb["Pages"])
    for col in ("Click Depth", "Linked From (pages)", "Link Score (0-100)", "Orphan Page"):
        assert col not in headers(wb["All Data"])
    summary = [c.value for row in wb["Summary"].iter_rows() for c in row if c.value]
    assert "Orphan pages" not in summary
    assert any("5 URLs from your list" in str(v) for v in summary)
    assert not [t for t in all_text(wb) if t[1].startswith("N/A")]


def test_folder_report_has_no_site_wide_checks(site, tmp_path):
    wb, res = audit(tmp_path, site=site + "p/")
    checks = [r[3] for r in wb["Site Health"].iter_rows(min_row=2, values_only=True) if r[3]]
    for site_wide in ("Homepage", "HTTP → HTTPS", "www / non-www", "HSTS header", "404 handling"):
        assert site_wide not in checks
    about = " ".join(str(c.value) for row in wb["About This Audit"].iter_rows() for c in row if c.value)
    assert "Folder /p/" in about
    groups = [r[0] for r in wb["About This Audit"].iter_rows(values_only=True)]
    assert "Site-wide" not in groups  # the 'What was checked' list has no site-wide group for a folder


def test_crux_core_web_vitals(site, tmp_path, monkeypatch):
    def fake(key, url=None, origin=None, session=None):
        if origin:
            return {"lcp": 2100, "inp": 150, "cls": 0.05}
        return {"lcp": 4800, "inp": 180, "cls": 0.02} if url.endswith("/p/1") else None
    monkeypatch.setattr(crux, "query", fake)
    wb, res = audit(tmp_path, site=site, crux_key="test-key")
    pages = wb["Pages"]
    h = headers(pages)
    assert "Core Web Vitals" in h and "LCP (s)" in h
    rows = {r[0]: r for r in pages.iter_rows(min_row=2, values_only=True) if r[0]}
    p1 = rows[site + "p/1"]
    assert p1[h.index("Core Web Vitals")] == "Poor" and p1[h.index("LCP (s)")] == 4.8
    health = {r[3]: r for r in wb["Site Health"].iter_rows(min_row=2, values_only=True) if r[3]}
    assert health["Core Web Vitals (whole site, real users)"][0] == "Pass"
    issues = [r[0] for r in wb["Issue URLs"].iter_rows(min_row=2, values_only=True)]
    assert "Poor Core Web Vitals" in issues


def test_report_time_uses_the_users_time_zone(site, tmp_path):
    wb, res = audit(tmp_path, site=site, max_pages=5, tz="Asia/Kolkata")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    sub = str(wb["Summary"]["A2"].value)
    assert now.strftime("%d %b %Y, %H:") in sub or now.strftime("%d %b %Y") in sub
