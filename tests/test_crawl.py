import threading

import pytest

from simplescope.audit import Paused, run_audit
from simplescope.store import Store
from testsite import make_server


@pytest.fixture()
def site():
    srv = make_server(pages=80, slow_s=1.0, latency_s=0.15)  # slow enough to pause mid-crawl
    yield "http://127.0.0.1:%d/" % srv.server_address[1]
    srv.shutdown()


def settings(url, tmp_path, **kw):
    return dict({"mode": "domain", "site": url, "max_pages": 300, "speed": "Medium", "render_js": "off",
                 "check_external": False, "out_dir": str(tmp_path)}, **kw)


def test_full_audit_labels_blocked_pages_and_writes_reports(site, tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run(settings(site, tmp_path))
    res = run_audit(store.run(rid)["settings"], store=store, run_id=rid)
    rows = {r["URL"].split("/", 3)[3]: r for r in store.page_rows(rid)}
    assert res["pages"] == len(rows) >= 80
    assert rows["challenge"]["Crawl Access"] == "Blocked: Cloudflare challenge"
    assert rows["limited"]["Final Status"] == 200            # 429 was retried after Retry-After
    assert rows["p/9999"]["Final Status"] == 404
    assert rows["p/9999"]["Crawl Access"] == "OK"             # a real 404 is a real answer, not a block
    assert "private/x" not in rows                            # robots.txt respected
    assert rows[""]["Click Depth"] == 0
    assert (tmp_path / res["xlsx"].split("\\")[-1].split("/")[-1]).exists()


def test_pause_then_resume_crawls_every_page_once(site, tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run(settings(site, tmp_path))
    calls = {"n": 0}

    def report(st):
        # Ask for a pause as soon as some pages are crawled (the app does this by setting `control`).
        if st.get("phase") == "crawling" and st.get("done", 0) >= 10 and not calls["n"]:
            calls["n"] = 1
            store.update_run(rid, control="pause")

    with pytest.raises(Paused):
        run_audit(store.run(rid)["settings"], report, store=store, run_id=rid)
    first = len(store.page_rows(rid))
    assert 10 <= first < 80
    store.update_run(rid, control="")
    res = run_audit(store.run(rid)["settings"], store=store, run_id=rid, resume=True)
    urls = [r["URL"] for r in store.page_rows(rid)]
    assert len(urls) == len(set(urls)) == res["pages"] >= 80


def test_finish_now_builds_a_report_from_partial_crawl(site, tmp_path):
    store = Store(tmp_path / "t.db")
    rid = store.create_run(settings(site, tmp_path))

    def report(st):
        if st.get("phase") == "crawling" and st.get("done", 0) >= 10:
            store.update_run(rid, control="finish")

    res = run_audit(store.run(rid)["settings"], report, store=store, run_id=rid)
    assert 10 <= res["pages"] < 80
    assert res["xlsx"]


def test_slow_server_keeps_concurrency_above_minimum(tmp_path):
    """Every page takes 1.2 s. The old crawler fell to 2 pages at a time; the new one stays at or above Medium's minimum (4)."""
    srv = make_server(pages=120, latency_s=1.2)
    try:
        url = "http://127.0.0.1:%d/" % srv.server_address[1]
        store = Store(tmp_path / "t.db")
        rid = store.create_run(settings(url, tmp_path, max_pages=120))
        seen = []
        run_audit(store.run(rid)["settings"], lambda st: seen.append(st["conc"]) if "conc" in st else None,
                  store=store, run_id=rid, stop=threading.Event())
        assert min(seen) >= 4
    finally:
        srv.shutdown()


def test_owner_header_unlocks_protected_page(site, tmp_path):
    store = Store(tmp_path / "t.db")
    for key, expect in (("", 403), ("X-Audit-Key: letmein", 200)):
        rid = store.create_run(settings(site, tmp_path, max_pages=200, auth_headers=key))
        run_audit(store.run(rid)["settings"], store=store, run_id=rid)
        rows = {r["URL"].rsplit("/", 1)[1]: r for r in store.page_rows(rid)}
        assert rows["members"]["Final Status"] == expect
