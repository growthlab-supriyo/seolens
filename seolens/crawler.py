"""Discovery and crawling: robots.txt, sitemap, start URL or list, then a continuous pool of fetchers whose size is set by
an AIMD controller (throttle.py). Every queued URL and crawled page is saved to SQLite, so a crawl can pause and resume.

Blocked pages (firewall / Cloudflare challenge, 403, CAPTCHA, rate limiting that never ends) are retried within limits,
then reported as blocked. The crawler never disguises itself to get past a block.
"""
import re
import time
from collections import deque
import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

from . import DEMO_MAX_URLS, MAX_URLS
from .analyze import analyze_page
from .htmltools import (FILE_EXT_RE, NON_PAGE_PATH_RE, bare_host, is_bot_check_page, is_utility_link, parse_url, path_of,
                        queue_key)
from .net import Fetcher, Response, find_robots_group, parse_robots, read_sitemap, robots_allows
from .renderer import Renderer, tags_from_requests
from .throttle import SPEEDS, Throttle

RETRIES = {"rate": 3, "busy": 3, "timeout": 2, "conn": 1, "5xx": 1, "403": 1}
SAVE_EVERY_S = 1.5


def parse_headers(text):
    """'Name: value' lines → {name: value} (blank lines and lines without ':' are ignored)."""
    out = {}
    for line in str(text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() and re.match(r"^[A-Za-z0-9-]+$", k.strip()):
                out[k.strip()] = v.strip()
    return out


class Stopped(Exception):
    """The user stopped the crawl (nothing more is written)."""


class Paused(Exception):
    """The user paused the crawl: everything so far is saved and it can resume."""


def classify(ch, served_before):
    """(access label for the report, retry kind or None). served_before = the site already returned normal pages."""
    if ch["error"]:
        e = ch["error"]
        if e.startswith("Timeout"):
            return "Timeout", "timeout"
        if e.startswith("DNS"):
            return "DNS error", None
        if e.startswith(("Connection", "SSL")):
            return "Connection error", "conn"
        if "redirect" in e.lower():
            return "OK", None  # a redirect loop is a real answer from the site
        return "Fetch error", "conn"
    res = ch["response"]
    code = ch["chain"][-1][0] if ch["chain"] else 0
    cf = res is not None and ("cloudflare" in res.header("server").lower() or res.header("cf-mitigated").lower() == "challenge")
    if res is not None and res.header("cf-mitigated").lower() == "challenge":
        return "Blocked: Cloudflare challenge", None
    if res is not None and code in (200, 403, 503) and is_bot_check_page(res.text):
        return ("Blocked: Cloudflare challenge" if cf else "Blocked: bot check / CAPTCHA"), None
    if code == 429:
        return "Rate limited (429)", "rate"
    if code == 503:
        return "Server busy (503)", "busy"
    if code == 403:
        # A 403 in the middle of a crawl that was going fine is usually rate limiting: retry once, slower.
        return ("Blocked: Cloudflare (403)" if cf else "Blocked (403)"), ("403" if served_before else None)
    if code in (500, 502, 504, 520, 521, 522, 523, 524):
        return "Server error (%d)" % code, "5xx"
    return "OK", None


def merge_rendered(raw_row, raw_ex, row, ex, tags, browser):
    """row/ex = the rendered page (kept). Adds what changed compared with the raw HTML, and the tags seen in the browser."""
    issues = [i for i in row.get("_base_issues", []) if i not in ("Content may depend on JavaScript", "No analytics detected")]
    num = lambda v: v if isinstance(v, int) else 0  # noqa: E731
    raw_w, ren_w = num(raw_row.get("Word Count")), num(row.get("Word Count"))
    raw_l, ren_l = len(raw_ex.get("links", [])), len(ex.get("links", []))
    changed = []
    for col, label in (("Meta Title", "title"), ("Canonical URL", "canonical"), ("Meta Robots", "robots"), ("H1 Text", "H1")):
        a, b = str(raw_row.get(col, "")), str(row.get(col, ""))
        if a != b:
            changed.append("%s: %s → %s" % (label, (a[:60] or "–"), (b[:60] or "–")))
    verdict = []
    if ren_w - raw_w >= 100 and raw_w < ren_w * 0.5:
        issues.append("Content only appears with JavaScript")
        verdict.append("⚠ most content appears only with JavaScript")
    if ren_l >= raw_l + 5 and raw_l < ren_l * 0.8:
        issues.append("Links only appear with JavaScript")
        verdict.append("⚠ %d links appear only with JavaScript" % (ren_l - raw_l))
    if any(c.split(":")[0] in ("title", "canonical", "robots") for c in changed):
        issues.append("JavaScript changes SEO tags")
    row["JS-Rendering Risk"] = "Rendered with %s · raw HTML %d words → rendered %d words · links %d → %d%s%s" % (
        browser, raw_w, ren_w, raw_l, ren_l, (" · changed by JavaScript: " + "; ".join(changed)) if changed else "",
        (" · " + "; ".join(verdict)) if verdict else " · content is in the HTML (no JavaScript needed)")
    found, analytics, ga_hit = tags
    seen = ", ".join("%s%s" % (n, " (%s)" % ", ".join(ids) if ids else "") for n, ids in found.items())
    html_tags = str(raw_row.get("Tracking Detected", ""))
    row["Tracking Detected"] = ("Loaded in the browser: " + seen + (" · GA4 sent a page view" if ga_hit else "")) if seen else \
        "Nothing loaded in the browser (before any cookie consent)" + (" · in the HTML: " + html_tags if html_tags and html_tags != "None found" else "")
    if not analytics and "webPixelsConfigList" not in html_tags and "Shopify web pixels" not in html_tags:
        issues.append("No analytics detected")
    row["_base_issues"] = issues
    # Links found only after rendering are still followed by the crawl.
    ex["discovered"] = list(dict.fromkeys(raw_ex.get("discovered", []) + ex.get("discovered", [])))



class Crawler:
    """settings: mode ('domain' | 'list'), site, urls, speed, max_pages, follow_links, include_query, respect_robots, render_js."""

    def __init__(self, settings, report, stop_event, store=None, run_id=None):
        self.s = settings
        self.s["max_pages"] = max(1, min(int(self.s.get("max_pages") or 500), MAX_URLS))
        if os.environ.get("SEOLENS_DEMO") == "1":
            # Public demo: enforced here too, not only in the form, so no request can get around it.
            self.s.update(max_pages=min(self.s["max_pages"], DEMO_MAX_URLS), render_js="off", respect_robots=True,
                          auth_headers="", auth_user="", auth_pass="")
        self.report = report            # report(dict) → progress
        self.stop = stop_event
        self.store, self.run_id = store, run_id
        self.fetcher = Fetcher()
        site = parse_url(settings.get("site", "") if re.match(r"^https?://", settings.get("site", ""), re.I) else "https://" + settings.get("site", ""))
        if not site and settings.get("urls"):
            site = parse_url(settings["urls"][0] if re.match(r"^https?://", settings["urls"][0], re.I) else "https://" + settings["urls"][0])
        self.fetcher.set_access(site["host"] if site else "", parse_headers(settings.get("auth_headers", "")),
                                (settings.get("auth_user", ""), settings.get("auth_pass", "")))
        start, lo, hi = SPEEDS.get(settings.get("speed"), SPEEDS["Medium"])
        self.throttle = Throttle(start, lo, hi)
        self.delay = 0.0                # only set by a robots.txt Crawl-delay
        self.browser = False            # kept for the report: SEO Lens never switches to a browser identity
        self.ok_pages = 0
        self.robots_group = None
        self.robots_raw = None
        self.sitemap = None
        self.scope = ""
        self.start_path = ""         # set when a page address became the start of a whole-site audit
        self.host = ""
        self.protocol = "https"
        self.discovery = {"start": 0, "sitemap": 0, "crawl": 0, "listed": 0, "blocked": 0}
        self.queue = []                 # [(url, source)]
        self._new_queue = []            # queue items not yet saved
        self.rows, self.extras, self.page_idx, self.access = [], [], [], []
        self._new_pages = []
        self.done = set()               # queue indexes that have a page row
        self.renderer, self.rendered, self.render_note, self.home_tags = None, 0, "", None
        self.started = time.time()

    @property
    def conc(self):
        return self.throttle.current

    @property
    def slowdowns(self):
        return self.throttle.slowdowns

    @property
    def slow_reason(self):
        return self.throttle.reason

    def _enqueue(self, url, source):
        self._new_queue.append((len(self.queue), url, source))
        self.queue.append((url, source))

    # ---------------------------------------------------------------- discovery

    def log(self, msg, **kw):
        self.report(dict(kw, log=msg))

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped()

    def discover(self):
        s = self.s
        if s["mode"] == "list":
            seen = set()
            for u in s["urls"]:
                u = u.strip()
                if not u:
                    continue
                if not re.match(r"^https?://", u, re.I):
                    u = "https://" + u
                k = queue_key(u)
                if k not in seen:
                    seen.add(k)
                    self._enqueue(u, "Your list")
            self.discovery["listed"] = len(self.queue)
            first = parse_url(self.queue[0][0]) if self.queue else None
            self.host = first["host"] if first else ""
            self.protocol = first["protocol"] if first else "https"
            self.log("Discovery: %d URLs from your list" % len(self.queue), phase="discovering")
            return
        raw = s["site"].strip()
        if not re.match(r"^https?://", raw, re.I):
            raw = "https://" + raw
        p = parse_url(raw)
        if not p:
            raise ValueError("That doesn't look like a website address: " + s["site"])
        self.report({"phase": "discovering", "step": "Reading robots.txt and the sitemap", "log": "Starting " + raw})
        # The real homepage address (http/https, www or not) after redirects.
        home = self.fetcher.chain(p["protocol"] + "://" + p["host"] + "/")
        if home["error"]:
            raise ValueError("The site doesn't load: " + home["error"])
        hp = parse_url(home["final"]) or p
        self.host, self.protocol = hp["host"], hp["protocol"]
        path = p["path"].rstrip("/")
        if re.search(r"/[^/]+\.[a-z0-9]{2,5}$", path, re.I):
            path = re.sub(r"/[^/]*$", "", path)
        self.scope = path.lower()
        origin = self.protocol + "://" + self.host
        res, err = self.fetcher.get(origin + "/robots.txt", follow=True)
        if not err and res.status == 200 and not re.match(r"^\s*(<!doctype|<html|<head|<body)", res.text[:500], re.I):
            self.robots_raw = parse_robots(res.text)
            self.robots_group = find_robots_group(self.robots_raw["groups"], "googlebot")
            if s.get("respect_robots") and self.robots_group and self.robots_group.get("crawl_delay"):
                self.delay = min(10.0, self.robots_group["crawl_delay"])
                self.throttle.fixed(1)
                self.log("robots.txt asks for a Crawl-delay of %ss: one page at a time" % self.robots_group["crawl_delay"])
        cands = (self.robots_raw["sitemaps"] if self.robots_raw else []) + [origin + "/sitemap.xml", origin + "/sitemap_index.xml", origin + "/wp-sitemap.xml"]
        self.check_stop()
        self.sitemap = read_sitemap(self.fetcher, list(dict.fromkeys(cands))[:50])
        start = origin + (p["path"] if self.scope else "/")
        self.add(start, "Start URL" if self.scope else "Homepage")
        if self.sitemap:
            for u in self.sitemap["urls"]:
                if self.add(u, "Sitemap"):
                    self.discovery["sitemap"] += 1
        self.discovery["start"] = 1
        self.log("Discovery: %d URLs (%d from the sitemap)%s" % (len(self.queue), self.discovery["sitemap"], ", inside " + self.scope + "/" if self.scope else ""),
                 discovered=len(self.queue))

    def to_queue_url(self, u):
        p = parse_url(re.sub(r"#.*$", "", u))
        if not p or bare_host(p["host"]) != bare_host(self.host):
            return ""
        if self.scope:
            pl = p["path"].lower()
            if pl != self.scope and pl != self.scope + "/" and not pl.startswith(self.scope + "/"):
                return ""
        if p["query"] and not self.s.get("include_query"):
            return ""
        if FILE_EXT_RE.search(p["path"]) or NON_PAGE_PATH_RE.search(p["path"]) or is_utility_link(u):
            return ""
        return self.protocol + "://" + self.host + p["path"] + (p["query"] if self.s.get("include_query") else "")

    def add(self, u, source):
        q = self.to_queue_url(u)
        if not q:
            return False
        k = queue_key(q)
        if k in self._seen:
            return False
        self._seen.add(k)
        if self.s.get("respect_robots") and self.robots_group and not robots_allows(self.robots_group, path_of(q)):
            self.discovery["blocked"] += 1
            return False
        self._enqueue(q, source)
        return True


    # ---------------------------------------------------------------- saving and resuming

    def meta(self):
        sm = dict(self.sitemap, keys=sorted(self.sitemap["keys"])) if self.sitemap else None
        return {"host": self.host, "protocol": self.protocol, "scope": self.scope, "start_path": self.start_path,
                "discovery": self.discovery, "robots_raw": self.robots_raw, "robots_group": self.robots_group, "sitemap": sm,
                "delay": self.delay, "home_tags": self.home_tags}

    def save(self, force=False):
        if not self.store:
            return
        now = time.time()
        if not force and now - getattr(self, "_saved", 0) < SAVE_EVERY_S:
            return
        self._saved = now
        if self._new_queue:
            self.store.add_queue(self.run_id, self._new_queue)
            self._new_queue = []
        if self._new_pages:
            self.store.add_pages(self.run_id, self._new_pages)
            self._new_pages = []
        self.store.update_run(self.run_id, meta=self.meta())  # commits

    def restore(self):
        m = self.store.run(self.run_id)["meta"]
        self.host, self.protocol, self.scope = m["host"], m["protocol"], m["scope"]
        self.start_path, self.discovery, self.delay = m.get("start_path", ""), m["discovery"], m.get("delay", 0.0)
        self.robots_raw, self.robots_group, self.home_tags = m.get("robots_raw"), m.get("robots_group"), m.get("home_tags")
        if m.get("sitemap"):
            self.sitemap = dict(m["sitemap"], keys=set(m["sitemap"]["keys"]))
        if self.delay:
            self.throttle.fixed(1)
        self.queue = [(u, s) for _, u, s in self.store.load_queue(self.run_id)]
        self._seen = {queue_key(u) for u, _ in self.queue}
        for idx, row, ex, access in self.store.load_pages(self.run_id):
            self.rows.append(row)
            self.extras.append(ex)
            self.page_idx.append(idx)
            self.access.append(access)
            self.done.add(idx)
            if access == "OK" and row.get("Final Status") == 200:
                self.ok_pages += 1
        self.log("Resuming: %d of %d discovered URLs were already crawled" % (len(self.done), len(self.queue)),
                 discovered=len(self.queue))

    # ---------------------------------------------------------------- crawl

    def run(self, resume=False):
        self._seen = set()
        if resume and self.store:
            self.restore()
        else:
            self.discover()
            self.save(force=True)
        if self.host:
            self.fetcher.set_access(self.host, self.fetcher.auth_headers, self.fetcher.basic_auth)
        self.start_renderer()
        self.start_cpu_pool()
        try:
            self.crawl()
        finally:
            if self.renderer:
                self.renderer.close()
            if self.cpu_pool:
                self.cpu_pool.shutdown(wait=False, cancel_futures=True)
            self.save(force=True)
        # Back in queue order (pages finish out of order), which is what the checks and the report expect.
        order = sorted(range(len(self.rows)), key=lambda k: self.page_idx[k])
        self.rows = [self.rows[k] for k in order]
        self.extras = [self.extras[k] for k in order]
        self.page_idx = [self.page_idx[k] for k in order]
        self.access = [self.access[k] for k in order]
        return self

    # ---------------------------------------------------------------- JavaScript rendering

    def start_renderer(self):
        mode = self.s.get("render_js", "auto")
        if mode == "off":
            self.render_note = "Off (raw HTML only)"
            return
        self.report({"step": "Starting the browser for JavaScript rendering", "log": "Starting the browser for JavaScript rendering"})
        r = Renderer()
        r.auth_host, r.auth_headers = self.fetcher.auth_host, self.fetcher.auth_headers
        if r.start():
            self.renderer = r
            self.log("🖥️ JavaScript rendering with %s (%s)" % (r.name, "every page" if mode == "all" else "start page + pages that look empty without JavaScript"))
        else:
            r.close()
            self.render_note = "Not available: %s (raw HTML only)" % r.error
            self.log("JavaScript rendering not available (%s): the audit reads the raw HTML only" % r.error)

    def needs_render(self, idx, ch, row, ex):
        if row.get("Final Status") != 200 or ex.get("non_html") or str(row.get("Meta Title", "")).startswith("N/A"):
            return False
        risky = not str(row.get("JS-Rendering Risk", "Low")).startswith("Low")
        return self.s.get("render_js") == "all" or idx == 0 or risky

    def render_group(self, items):
        """items: [(idx, url, ch, row, ex)] that need rendering → the same list with the rendered rows."""
        self.report({"log": "🖥️ Rendering %d page/s with JavaScript" % len(items)})
        results = self.renderer.render([ch["final"] for _, _, ch, _, _ in items])
        out = []
        for idx, url, ch, raw_row, raw_ex in items:
            r = results.get(ch["final"]) or {}
            if r.get("error") or not r.get("html"):
                raw_row["JS-Rendering Risk"] = str(raw_row.get("JS-Rendering Risk", "")) + " · Not rendered: " + (r.get("error") or "no HTML")
                out.append((idx, url, ch, raw_row, raw_ex))
                continue
            raw = ch["response"]
            rendered = Response(raw.url, raw.status, raw.headers, raw.content, r["html"])
            try:
                row, ex = analyze_page(url, dict(ch, response=rendered))
            except Exception:  # noqa: BLE001 - keep the raw analysis
                out.append((idx, url, ch, raw_row, raw_ex))
                continue
            merge_rendered(raw_row, raw_ex, row, ex, tags_from_requests(r.get("requests", [])), self.renderer.name)
            self.rendered += 1
            if idx == 0:
                self.home_tags = row.get("Tracking Detected")
            out.append((idx, url, ch, row, ex))
        return out

    # ---------------------------------------------------------------- the fetch loop

    def start_cpu_pool(self):
        """Page analysis is CPU work (about 1-2 s for a 500 KB page) and Python threads can't share CPU cores, so the
        analysis runs in separate processes, one per spare core. Fetching stays in threads (it only waits on the network)."""
        self.cpu_pool = None
        n = max(1, min(8, (os.cpu_count() or 2) - 1))
        if n < 2 or os.environ.get("SEOLENS_NO_PROCESSES") == "1":
            return
        try:
            self.cpu_pool = ProcessPoolExecutor(max_workers=n)
            self.cpu_pool.submit(int, 0).result(timeout=60)  # start-up check
            self.log("⚙️ Page analysis runs on %d CPU cores" % n)
        except Exception:  # noqa: BLE001 - fall back to analysing in this process
            self.cpu_pool = None

    def analyse(self, url, ch):
        if self.cpu_pool:
            try:
                return self.cpu_pool.submit(analyze_page, url, ch).result()
            except BrokenProcessPool:
                self.cpu_pool = None
        return analyze_page(url, ch)

    def fetch_one(self, idx, url):
        """Runs in a worker thread: fetch (following redirects hop by hop), then analyse in the process pool."""
        t0 = time.time()
        ch = self.fetcher.chain(url)
        took = time.time() - t0
        try:
            row, ex = self.analyse(url, ch)
        except Exception as e:  # noqa: BLE001 - one odd page must not stop the audit
            row, ex = ({"URL": url, "Initial Status": "Error", "Final Status": "Error", "Indexable": "No – fetch failed",
                        "Redirect Chain": "Script error: %s" % e, "_base_issues": ["Page fetch failed"]},
                       {"links": [], "discovered": [], "images": [], "hreflang": [], "minhash": None, "source": url})
        return idx, url, ch, row, ex, took

    def keep(self, idx, url, ch, row, ex, access, follow):
        row["Crawl Access"] = access
        self.rows.append(row)
        self.extras.append(ex)
        self.page_idx.append(idx)
        self.access.append(access)
        self.done.add(idx)
        self._new_pages.append((idx, row, ex, access))
        if access == "OK" and row.get("Final Status") == 200:
            self.ok_pages += 1
        if follow:
            for link in ex["discovered"]:
                if self.add(link, "Crawl"):
                    self.discovery["crawl"] += 1
        if idx == 0 and len(self.queue) == 1 and self.scope and follow:
            self.widen_scope(ex)

    def crawl(self):
        limit = lambda: min(len(self.queue), self.s["max_pages"])  # noqa: E731
        follow = self.s["mode"] == "domain" and self.s.get("follow_links", True)
        retry = deque(i for i in range(min(len(self.queue), self.s["max_pages"])) if i not in self.done)
        cursor = len(self.queue) if self.done else 0  # on resume every known URL not crawled yet is in `retry`
        if not self.done:
            retry.clear()
        tries = {}
        inflight = {}
        to_render = []
        recent_5xx = deque()
        finishing = None
        last_dispatch = last_progress = last_control = 0.0
        blocked = sum(1 for a in self.access if a != "OK")
        t_start, done_start = time.time(), len(self.done)

        def next_idx():
            nonlocal cursor
            if retry:
                return retry.popleft()
            if cursor < limit():
                cursor += 1
                return cursor - 1
            return None

        with ThreadPoolExecutor(max_workers=self.throttle.maximum) as pool:
            while True:
                if self.stop.is_set():
                    for f in inflight:
                        f.cancel()
                    raise Stopped()
                now = time.time()
                if self.store and now - last_control > 1.0:
                    last_control = now
                    was = finishing
                    finishing = finishing or self.store.control(self.run_id) or None
                    if finishing and not was:
                        self.log("⏸ Pausing: finishing the pages in progress" if finishing == "pause" else
                                 "⏹ Finishing early: the report is built from the pages crawled so far")
                # Keep `current` pages in flight (unless paused by Retry-After or a Crawl-delay).
                while not finishing and len(inflight) < self.throttle.current and self.throttle.wait_s() == 0:
                    if self.delay and time.time() - last_dispatch < self.delay:
                        break
                    idx = next_idx()
                    if idx is None:
                        break
                    last_dispatch = time.time()
                    inflight[pool.submit(self.fetch_one, idx, self.queue[idx][0])] = idx
                if not inflight:
                    if to_render:
                        for item in self.render_group(to_render):
                            self.keep(*item, "OK", follow)
                        to_render = []
                        continue
                    if finishing or not (retry or cursor < limit()):
                        break
                    # More to crawl, but waiting for a Retry-After pause or a Crawl-delay.
                    time.sleep(min(0.25, max(0.05, self.throttle.wait_s())))
                    continue
                finished, _ = wait(list(inflight), timeout=0.5, return_when=FIRST_COMPLETED)
                start_page = False
                for fut in finished:
                    inflight.pop(fut)
                    idx, url, ch, row, ex, took = fut.result()
                    access, kind = classify(ch, self.ok_pages > 0)
                    if kind and tries.get(idx, 0) < RETRIES[kind] and not finishing:
                        tries[idx] = tries.get(idx, 0) + 1
                        if kind in ("rate", "busy", "403"):
                            ra = 0.0
                            try:
                                ra = float(ch["response"].header("retry-after") or 0) if ch["response"] else 0.0
                            except ValueError:
                                pass
                            self.throttle.overload(ra * tries[idx] if ra else 3.0 * tries[idx], access)
                            self.report({"slow": {"conc": self.conc, "reason": self.slow_reason},
                                         "log": "🐢 %s: %d pages at a time, retrying %s" % (access, self.conc, url)})
                        else:
                            self.throttle.record(took, ok=False)
                            if kind == "5xx":
                                # Several quick 5xx in a row = the server failing under load, even when it answers fast.
                                recent_5xx.append(time.time())
                                while recent_5xx and recent_5xx[0] < time.time() - 20:
                                    recent_5xx.popleft()
                                if len(recent_5xx) >= 3:
                                    self.throttle.overload(3.0, "HTTP %s errors" % ch["chain"][-1][0])
                                    recent_5xx.clear()
                                    self.report({"slow": {"conc": self.conc, "reason": self.slow_reason},
                                                 "log": "🐢 Server errors: slowed down to %d pages at a time" % self.conc})
                        retry.append(idx)
                        continue
                    change = self.throttle.record(took, ok=(access == "OK"))
                    if kind in ("5xx", "busy"):
                        # Still a server error after the retries: a real answer (a broken page), not a block.
                        access = "OK"
                    if change == "down":
                        self.report({"slow": {"conc": self.conc, "reason": self.slow_reason},
                                     "log": "🐢 Slowed down to %d pages at a time: %s" % (self.conc, self.slow_reason)})
                    elif change == "up" and self.conc % 5 == 0:
                        self.log("⚡ Speeding up: %d pages at a time" % self.conc)
                    if access != "OK":
                        blocked += 1
                    if self.renderer and access == "OK" and self.needs_render(idx, ch, row, ex):
                        to_render.append((idx, url, ch, row, ex))
                        start_page = start_page or idx == 0
                    else:
                        self.keep(idx, url, ch, row, ex, access, follow)
                if to_render and (len(to_render) >= 3 or start_page or not inflight):
                    for item in self.render_group(to_render):
                        self.keep(*item, "OK", follow)
                    to_render = []
                now = time.time()
                if now - last_progress > 0.5:
                    last_progress = now
                    elapsed = now - t_start
                    self.report({"phase": "crawling", "done": len(self.done), "total": limit(), "discovered": len(self.queue),
                                 "conc": self.conc, "inflight": len(inflight), "blocked": blocked, "median_s": round(self.throttle.median(), 2),
                                 "rate": (len(self.done) - done_start) / elapsed if elapsed else 0})
                self.save()
        self.report({"phase": "crawling", "done": len(self.done), "total": limit(), "discovered": len(self.queue), "conc": self.conc,
                     "blocked": blocked, "inflight": 0})
        if finishing == "pause":
            self.save(force=True)
            raise Paused()
        self.recheck_server_errors()
        return self

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            time.sleep(min(0.25, max(0.0, end - time.time())))

    def recheck_server_errors(self):
        """A 5xx during a fast crawl can be the server failing under load, not a broken page. Every 5xx page is fetched
        again slowly, one at a time, after a short pause. Pages that now load get their real data and are reported as
        'Intermittent server error (5xx)'; the rest stay 5xx."""
        todo = [k for k, r in enumerate(self.rows) if isinstance(r.get("Final Status"), int) and r["Final Status"] >= 500]
        if not todo:
            return
        self.log("Rechecking %d page/s that answered with a server error, one at a time" % len(todo))
        self.sleep(5)
        fixed = 0
        for n, k in enumerate(todo):
            self.check_stop()
            idx, url, ch, row, ex, took = self.fetch_one(self.page_idx[k], self.rows[k]["URL"])
            if ch["chain"] and ch["chain"][-1][0] < 500 and not ch["error"]:
                first = self.rows[k]["Final Status"]
                row["Crawl Access"] = "OK"
                row["_base_issues"] = list(row.get("_base_issues", [])) + ["Intermittent server error (5xx)"]
                row["Server Errors"] = "HTTP %s during the crawl, %s on a slow recheck" % (first, ch["chain"][-1][0])
                self.rows[k], self.extras[k] = row, ex
                self._new_pages.append((idx, row, ex, "OK"))
                fixed += 1
            self.report({"phase": "crawling", "step": "Rechecking server errors (%d of %d)" % (n + 1, len(todo))})
            self.sleep(1.0)
        self.log("Recheck: %d of %d page/s load fine when fetched slowly (intermittent server errors); %d still fail" % (
            fixed, len(todo), len(todo) - fixed))
        self.save(force=True)

    def widen_scope(self, ex):
        """The start URL links to nothing inside its own folder (e.g. a landing page like /teaching whose links go to /course/...):
        a URL typed without a trailing slash then becomes the start page of a whole-site audit instead of a one-page audit."""
        if self.s["site"].strip().endswith("/"):
            self.log("Nothing else was found inside %s/. To audit the pages this page links to, enter the address without the trailing slash "
                     "or audit the whole domain." % self.scope)
            return
        folder, self.scope = self.scope, ""
        self.start_path = folder  # click depth is still counted from the page that was entered
        for link in ex["discovered"]:
            if self.add(link, "Crawl"):
                self.discovery["crawl"] += 1
        for u in (self.sitemap or {}).get("urls", []):
            if self.add(u, "Sitemap"):
                self.discovery["sitemap"] += 1
        self.log("No pages inside %s/ were linked from the start URL, so the audit follows its links across the whole site "
                 "(start URL = click depth 0). Add a trailing slash to audit only that folder." % folder, discovered=len(self.queue))

