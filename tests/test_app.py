"""The Streamlit dashboard opens without errors for every kind of audit (whole site, folder, URL list)."""
import pytest

from seolens.audit import run_audit
from seolens.store import Store
from testsite import make_server

pytest.importorskip("pyarrow.compute")  # the dashboard needs it; skipped where the OS blocks it


@pytest.fixture()
def site():
    srv = make_server(pages=15)
    yield "http://127.0.0.1:%d/" % srv.server_address[1]
    srv.shutdown()


@pytest.mark.parametrize("kind", ["site", "folder", "list"])
def test_dashboard_opens(site, tmp_path, monkeypatch, kind):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("SEOLENS_HOME", str(tmp_path))
    import seolens.store as store_mod
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "crawls.db")
    store = Store(tmp_path / "crawls.db")
    settings = {"site": site, "max_pages": 40, "check_external": False, "out_dir": str(tmp_path)}
    if kind == "folder":
        settings["site"] = site + "p/"
    elif kind == "list":
        settings = dict(settings, mode="list", urls=[site + "p/%d" % i for i in range(1, 6)])
    rid = store.create_run(settings)
    store.update_run(rid, status="running")
    result = run_audit(store.run(rid)["settings"], store=store, run_id=rid)
    store.update_run(rid, status="done", result=result)
    import seolens
    from pathlib import Path
    at = AppTest.from_file(str(Path(seolens.__file__).with_name("app.py")), default_timeout=120)
    at.session_state["run"] = rid
    at.run()
    assert not at.exception, at.exception[0].message if at.exception else ""
    assert len(at.tabs) == 8


def test_filters_and_filtered_report(site, tmp_path, monkeypatch):
    """Filtering narrows every table; the filtered report holds only the filtered pages and says so."""
    from streamlit.testing.v1 import AppTest
    from openpyxl import load_workbook
    from seolens.htmltools import norm
    from seolens.report_data import build_model
    from seolens.report_sheets import collect_tables, write_workbook
    from seolens.snapshot import rebuild
    monkeypatch.setenv("SEOLENS_HOME", str(tmp_path))
    import seolens.store as store_mod
    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "crawls.db")
    store = Store(tmp_path / "crawls.db")
    rid = store.create_run({"site": site, "max_pages": 40, "check_external": False, "out_dir": str(tmp_path)})
    store.update_run(rid, status="running")
    store.update_run(rid, status="done", result=run_audit(store.run(rid)["settings"], store=store, run_id=rid))
    assert store.snapshot(rid)["site"]["checks"]  # every site check is kept, passes included

    # The app's tables are the workbook's tables
    full = build_model(*rebuild(store, rid))
    tables = collect_tables(full)
    assert {"ActionPlan", "IssueDetails", "PageAudit", "OnPage", "Technical", "CanonicalClusters", "ParameterURLs",
            "InternalLinks", "ImagesSpeed", "SchemaSocial", "CrawlCoverage", "AuditInfo"} <= set(tables)
    assert len(tables["PageAudit"][1]) == len(full.rows)

    # A filtered report: only /p/1 and /p/2
    keep = {norm(site + "p/1"), norm(site + "p/2")}
    out = tmp_path / "filtered.xlsx"
    write_workbook(str(out), *rebuild(store, rid, keep=keep))
    wb = load_workbook(out)
    urls = [r[0] for r in wb["04 Page Audit"].iter_rows(min_row=7, values_only=True) if r[0]]
    assert sorted(norm(u) for u in urls) == sorted(keep)
    assert "filtered: 2 of" in str(wb["04 Page Audit"]["A4"].value)

    # The app: a URL filter narrows the pages and offers the filtered report
    import seolens
    from pathlib import Path
    at = AppTest.from_file(str(Path(seolens.__file__).with_name("app.py")), default_timeout=120)
    at.session_state["run"] = rid
    at.run()
    box = next(t for t in at.text_input if t.label == "URL contains")
    box.input("/p/1").run()
    assert not at.exception, at.exception[0].message if at.exception else ""
    assert any("Showing" in c.value and "URL contains /p/1" in c.value for c in at.caption)
    assert len(at.tabs) == 8
