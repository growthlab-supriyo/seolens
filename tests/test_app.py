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
    assert len(at.tabs) == 7
