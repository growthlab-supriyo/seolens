"""Command line.

    seolens                     opens the app in your browser (same as `seolens app`)
    seolens crawl example.com --max 2000 --speed Medium --render off
    seolens crawl --list urls.txt
"""
import argparse
import sys
import time
from pathlib import Path


def _app(port, host="localhost", browser=True):
    import threading
    import webbrowser

    from streamlit.web import cli as stcli
    app = str(Path(__file__).with_name("app.py"))
    url = "http://localhost:%d" % port
    # Headless mode skips Streamlit's first-run "enter your email" prompt (which would wait forever);
    # the browser is opened here instead, once the server has had a moment to start.
    print("SEO Lens is starting: %s (keep this window open while you use it; Ctrl+C stops it)" % url, flush=True)
    if host != "localhost":
        print("Listening on %s:%d: other devices on this network can open it (there is no login)." % (host, port), flush=True)
    if browser:
        threading.Timer(3.0, lambda: webbrowser.open(url)).start()
    sys.argv = ["streamlit", "run", app, "--server.port", str(port), "--server.headless", "true",
                "--browser.gatherUsageStats", "false", "--server.address", host]
    sys.exit(stcli.main())


def _crawl(a):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    from .store import Store
    from .worker import main as worker_main
    settings = {"speed": a.speed, "max_pages": a.max, "render_js": a.render, "follow_links": not a.no_follow,
                "respect_robots": not a.ignore_robots, "include_query": a.params,
                "render_max": min(500, max(25, a.render_max)), "content_min": min(1000, max(100, a.content_min)),
                "url_max": min(250, max(75, a.url_max))}
    if a.out:
        settings["out_dir"] = a.out
    if a.list:
        settings.update(mode="list", urls=[u for u in Path(a.list).read_text(encoding="utf-8").splitlines() if u.strip()])
    elif a.site:
        settings.update(mode="domain", site=a.site)
    else:
        sys.exit("Give a website (seolens crawl example.com) or --list urls.txt")
    store = Store()
    run_id = store.create_run(settings)
    print("Run %d: crawling %s (the app shows it too: seolens app)" % (run_id, settings.get("site") or a.list))
    _work(store, run_id, [])


def _resume(a):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    from .store import Store
    store = Store()
    run = store.run(a.run)
    if not run:
        sys.exit("No crawl #%d" % a.run)
    print("Run %d: resuming %s (%d pages saved so far)" % (a.run, run["target"], len(store.page_rows(a.run))))
    _work(store, a.run, ["--resume"] + (["--finish"] if a.finish else []))


def _work(store, run_id, extra):
    from .worker import main as worker_main
    t0 = time.time()
    worker_main(["--run", str(run_id)] + extra)
    run = store.run(run_id)
    for _, msg in store.logs(run_id, 15):
        print("  " + msg)
    print("Status: %s in %.0f s" % (run["status"], time.time() - t0))
    if run["status"] == "done":
        print("Excel report: " + run["result"]["xlsx"])
        if run["result"].get("map"):
            print("Site map:     " + run["result"]["map"])
    elif run["error"]:
        print(run["error"])
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seolens", description="Free, open-source SEO crawler (up to 10,000 URLs).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("app", help="open the app in your browser")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--host", default="localhost", help="0.0.0.0 = reachable from other devices or a container (no login!)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window (servers, containers)")
    c = sub.add_parser("crawl", help="crawl from the command line")
    c.add_argument("site", nargs="?")
    c.add_argument("--list", help="text file with one URL per line")
    c.add_argument("--max", type=int, default=500)
    c.add_argument("--speed", choices=["Polite", "Medium", "Fast"], default="Medium")
    c.add_argument("--render", choices=["off", "auto", "all"], default="off", help="JavaScript comparison: auto = Selective")
    c.add_argument("--render-max", type=int, default=100, help="most pages to render (25-500)")
    c.add_argument("--content-min", type=int, default=300, help="main-content review threshold in words (100-1000)")
    c.add_argument("--url-max", type=int, default=115, help="URL length review threshold in characters (75-250)")
    c.add_argument("--params", action="store_true", help="also crawl URLs with ?parameters")
    c.add_argument("--no-follow", action="store_true", help="don't follow links (sitemap + start URL only)")
    c.add_argument("--ignore-robots", action="store_true", help="for your own site only")
    c.add_argument("--out", help="folder for the reports")
    r = sub.add_parser("resume", help="resume a paused or interrupted crawl")
    r.add_argument("run", type=int, help="crawl number (shown when it started, and in the app)")
    r.add_argument("--finish", action="store_true", help="don't crawl more: build the report from the saved pages")
    a = ap.parse_args(argv)
    if a.cmd == "crawl":
        _crawl(a)
    elif a.cmd == "resume":
        _resume(a)
    else:
        _app(getattr(a, "port", 8501), getattr(a, "host", "localhost"), not getattr(a, "no_browser", False))


if __name__ == "__main__":
    main()
