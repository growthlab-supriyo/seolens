"""JavaScript rendering with the Edge or Chrome already installed on the computer (Playwright), like Google's second wave.

One browser runs in its own thread; pages are rendered a few at a time. Images, fonts and video are not downloaded (faster;
they don't change the HTML). The network requests show which analytics tags really load in a browser.
"""
import asyncio
import re
import threading

from . import __version__
from .htmltools import is_bot_check_page

PARALLEL = 3
NAV_TIMEOUT = 30000
IDLE_WAIT = 5000

# Requests that prove a tag loaded (or sent a hit) in the browser: [name, pattern, counts as analytics].
TAGS = [
    ("GA4", r"googletagmanager\.com/gtag/js\?id=(G-[A-Z0-9]+)|google-analytics\.com/g/collect[^ ]*?[?&]tid=(G-[A-Z0-9]+)|analytics\.google\.com/g/collect[^ ]*?[?&]tid=(G-[A-Z0-9]+)", True),
    ("GTM", r"googletagmanager\.com/gtm\.js\?id=(GTM-[A-Z0-9]+)", True),
    ("Google Ads", r"googletagmanager\.com/gtag/js\?id=(AW-\d+)|googleadservices\.com/pagead", False),
    ("Meta Pixel", r"facebook\.com/tr[/?]|connect\.facebook\.net/[^ ]*fbevents", False),
    ("Microsoft Clarity", r"clarity\.ms/", True),
    ("Hotjar", r"hotjar\.com", False),
    ("LinkedIn Insight", r"snap\.licdn\.com|px\.ads\.linkedin\.com", False),
    ("TikTok Pixel", r"analytics\.tiktok\.com", False),
    ("HubSpot", r"js\.hs-scripts\.com|js\.hs-analytics\.net", True),
    ("Matomo", r"matomo\.(js|php)|piwik\.(js|php)", True),
    ("Plausible", r"plausible\.io/", True),
    ("Segment", r"cdn\.segment\.com|api\.segment\.io", True),
    ("Adobe Analytics", r"assets\.adobedtm\.com|\.omtrdc\.net", True),
    ("Shopify Analytics", r"/api/collect\b|monorail-edge\.shopifysvc\.com|/\.well-known/shopify/monorail", True),
]
_TAGS = [(n, re.compile(p, re.I), a) for n, p, a in TAGS]


def _host(url):
    m = re.match(r"^https?://([^/?#]+)", url, re.I)
    return re.sub(r"^www\.", "", re.sub(r":\d+$", "", m.group(1).split("@")[-1].lower())) if m else ""


def tags_from_requests(urls):
    """{name: [ids]} for the tags whose requests were seen, plus whether any analytics tag loaded and whether GA4 sent a hit."""
    found, analytics, ga_hit = {}, False, False
    for u in urls:
        for name, rx, is_analytics in _TAGS:
            m = rx.search(u)
            if not m:
                continue
            ids = found.setdefault(name, [])
            gid = next((g for g in m.groups() if g), None) if m.groups() else None
            if gid and gid not in ids:
                ids.append(gid)
            analytics = analytics or is_analytics
            if name == "GA4" and "/collect" in u:
                ga_hit = True
    return found, analytics, ga_hit


class Renderer:
    def __init__(self):
        self.user_agent = ""
        self.name = ""
        self.error = ""
        self.count = 0
        self._loop = None
        self._thread = None
        self._pw = self._browser = None
        self.auth_host, self.auth_headers = "", {}   # authorized-access headers, added only for the audited host

    def start(self):
        """Starts Edge (or Chrome) in the background. Returns True, or False with self.error set."""
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.error = "Playwright isn't installed"
            return False
        ready = threading.Event()
        self._loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._launch())
            ready.set()
            if not self.error:
                self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        ready.wait(60)
        return not self.error and self._browser is not None

    async def _launch(self):
        from playwright.async_api import async_playwright
        try:
            self._pw = await async_playwright().start()
        except Exception as e:  # noqa: BLE001
            self.error = "Couldn't start the browser engine: %s" % str(e)[:120]
            return
        for channel, label in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome"), (None, "Chromium")):
            try:
                self._browser = await self._pw.chromium.launch(channel=channel, headless=True) if channel else await self._pw.chromium.launch(headless=True)
                v = self._browser.version.split(".")[0]
                self.name = "%s %s" % (label, v)
                # The browser's own identity plus SEO Lens's name, so the site knows an audit tool is rendering it.
                self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36 SEOLens/%s" % (
                    v, __version__)
                return
            except Exception:  # noqa: BLE001 - try the next browser
                continue
        self.error = "No Edge or Chrome found on this computer"

    def render(self, urls, timeout=180):
        """{url: {"html", "requests", "error"}} for each URL."""
        if not self._browser:
            return {u: {"error": self.error or "browser not started"} for u in urls}
        fut = asyncio.run_coroutine_threadsafe(self._render_many(urls), self._loop)
        try:
            return fut.result(timeout=timeout + 30 * len(urls) / PARALLEL)
        except Exception as e:  # noqa: BLE001
            return {u: {"error": "rendering timed out: %s" % str(e)[:80]} for u in urls}

    async def _render_many(self, urls):
        sem = asyncio.Semaphore(PARALLEL)
        out = {}

        async def one(u):
            async with sem:
                out[u] = await self._render_one(u)

        await asyncio.gather(*(one(u) for u in urls))
        return out

    async def _render_one(self, url):
        ctx = None
        try:
            ctx = await self._browser.new_context(user_agent=self.user_agent, viewport={"width": 1366, "height": 900}, locale="en-US")
            requests = []
            ctx.on("request", lambda r: requests.append(r.url))

            async def block(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                elif self.auth_headers and self.auth_host and _host(route.request.url) == self.auth_host:
                    await route.continue_(headers=dict(route.request.headers, **self.auth_headers))
                else:
                    await route.continue_()

            await ctx.route("**/*", block)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            try:
                await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT)
            except Exception:  # noqa: BLE001 - busy pages never go idle; what has loaded so far is used
                pass
            html = await page.content()
            self.count += 1
            if is_bot_check_page(html):
                return {"error": "the site showed a bot check to the browser", "requests": requests}
            return {"html": html, "requests": requests, "final": page.url}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e).split("\n")[0][:140]}
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self):
        if not self._loop:
            return

        async def shutdown():
            try:
                if self._browser:
                    await self._browser.close()
                if self._pw:
                    await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=20)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
