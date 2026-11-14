"""Everything the Excel report is built from, saved with the crawl, so the app can show the same tables and build a
report for any filtered set of pages long after the crawl process has ended.

capture() runs at the end of an audit; rebuild() turns the saved data (plus the page rows, extras and queue already in
the database) back into the objects report_data.build_model() and report_sheets.write_workbook() take.
"""
from datetime import datetime
from types import SimpleNamespace

from .htmltools import norm

SECRET_SETTINGS = ("auth_pass", "auth_headers", "auth_user", "crux_key")


def capture(crawler, post, site, started, finished):
    sm = site.get("sitemap")
    return {
        "version": 1,
        "started": started.isoformat(), "finished": finished.isoformat(),
        "settings": {k: v for k, v in crawler.s.items() if k not in SECRET_SETTINGS},
        "crawler": {"host": crawler.host, "scope": crawler.scope, "discovery": crawler.discovery, "conc": crawler.conc,
                    "slowdowns": crawler.slowdowns, "slow_reason": crawler.slow_reason, "rendered": getattr(crawler, "rendered", 0),
                    "render_note": getattr(crawler, "render_note", ""), "renderer": bool(getattr(crawler, "renderer", None)),
                    "requests": crawler.fetcher.count},
        "site": {"checks": site["checks"], "host": site.get("host", ""), "site_wide": site["site_wide"],
                 "sitemap_keys": sorted(site.get("sitemap_keys") or []), "has_sitemap": bool(sm)},
        "post": {"checks": post.checks, "broken_rows": [list(b) for b in post.broken_rows], "res": post.res,
                 "skipped": getattr(post, "skipped", {}), "advanced": getattr(post, "advanced", {}) or {}},
    }


def _filter_advanced(adv, keep):
    """The advanced tables limited to the kept pages (site-wide counts stay as they were)."""
    if keep is None:
        return adv
    k = lambda u: norm(str(u or "")) in keep  # noqa: E731
    return dict(adv, params=[r for r in adv.get("params", []) if k(r[0]) or k(r[1])],
                clusters=[r for r in adv.get("clusters", []) if k(r[1])],
                anchors=[r for r in adv.get("anchors", []) if k(r[0])],
                render_links=[r for r in adv.get("render_links", []) if k(r[1])])


def rebuild(store, run_id, snap=None, keep=None):
    """(crawler, post, site, started, finished) for build_model / write_workbook.

    keep: a set of normalised page URLs to limit the pages to (a filtered report), or None for every page.
    Crawls saved before snapshots existed get empty site checks and link data; everything page-based still works."""
    run = store.run(run_id)
    snap = snap if snap is not None else (store.snapshot(run_id) or {})
    pages = store.load_pages(run_id)
    if keep is not None:
        pages = [p for p in pages if norm(p[1].get("URL", "")) in keep]
        # links are listed under the page they were found on, by its final address
        found_on = keep | {norm(p[1].get("Final URL") or p[1].get("URL", "")) for p in pages}
    from .checks import REMOVED_ISSUES
    for p in pages:  # crawls saved before a check was removed still list it
        r = p[1]
        if r.get("Issues"):
            r["Issues"] = "; ".join(i for i in (x.strip() for x in str(r["Issues"]).split(";")) if i and i not in REMOVED_ISSUES)
    c = snap.get("crawler", {})
    meta = run.get("meta") or {}
    settings = dict(run["settings"], **snap.get("settings", {}))
    for k in SECRET_SETTINGS:
        settings.pop(k, None)
    settings.setdefault("mode", "domain")
    crawler = SimpleNamespace(
        rows=[p[1] for p in pages], extras=[p[2] for p in pages], page_idx=[p[0] for p in pages], access=[p[3] for p in pages],
        queue=[(u, s) for _, u, s in store.load_queue(run_id)], s=settings, scope=c.get("scope", meta.get("scope", "")),
        host=c.get("host", meta.get("host", "")), discovery=c.get("discovery", meta.get("discovery", {})) or {},
        conc=c.get("conc", 0), slowdowns=c.get("slowdowns", 0), slow_reason=c.get("slow_reason", ""),
        rendered=c.get("rendered", 0), render_note=c.get("render_note", ""), renderer=c.get("renderer") or None,
        fetcher=SimpleNamespace(count=c.get("requests", 0)), run_id=run_id,
        filter_note=("filtered: %d of %d audited pages" % (len(pages), len(store.page_rows(run_id)))) if keep is not None else "")
    p = snap.get("post", {})
    broken = [tuple(b) for b in p.get("broken_rows", run.get("result", {}).get("broken", []))]
    if keep is not None:
        broken = [b for b in broken if norm(str(b[0])) in found_on]
    post = SimpleNamespace(checks=p.get("checks", []), broken_rows=broken, res=p.get("res", {}), skipped=p.get("skipped", {}),
                           advanced=_filter_advanced(p.get("advanced", {}), keep), graph={})
    s = snap.get("site", {})
    checks = s.get("checks")
    if checks is None:  # older crawls kept only the failed and warning site checks
        checks = [dict(x) for x in run.get("result", {}).get("checks", [])]
    site = {"checks": checks, "host": s.get("host", crawler.host), "site_wide": s.get("site_wide", not crawler.scope),
            "sitemap_keys": set(s.get("sitemap_keys", [])), "sitemap": {"keys": set()} if s.get("has_sitemap") else None}
    started = datetime.fromisoformat(snap["started"]) if snap.get("started") else datetime.fromtimestamp(run["created"])
    finished = datetime.fromisoformat(snap["finished"]) if snap.get("finished") else datetime.fromtimestamp(run["updated"])
    return crawler, post, site, started, finished
