"""A small local website for tests: N linked pages, a sitemap, a 404, a redirect, a slow page, a rate-limited page
and a page that answers with a Cloudflare-style challenge."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html lang="en"><head><title>Test page {n} about gardening tools</title>
<meta name="description" content="Page {n} of the SEO Lens test site, a description long enough to pass the length check for tests.">
<link rel="canonical" href="http://127.0.0.1:{port}{path}"></head>
<body><h1>Gardening page {n}</h1><p>{text}</p>{links}</body></html>"""


def make_server(pages=60, slow_s=0.0, latency_s=0.0, every_7th_s=0.0):
    hits = {"rate": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            port = self.server.server_address[1]
            p = self.path
            if latency_s:
                time.sleep(latency_s)
            if p == "/robots.txt":
                return self.send(200, "User-agent: *\nDisallow: /private\nSitemap: http://127.0.0.1:%d/sitemap.xml\n" % port, "text/plain")
            if p == "/sitemap.xml":
                locs = "".join("<url><loc>http://127.0.0.1:%d/p/%d</loc></url>" % (port, i) for i in range(1, 6))
                return self.send(200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % locs, "application/xml")
            if p == "/old":
                return self.send(301, headers={"Location": "/p/1"})
            if p == "/slow":
                time.sleep(slow_s)
                return self.send(200, PAGE.format(n="slow", port=port, path=p, text="slow " * 150, links=""))
            if p == "/limited":
                hits["rate"] += 1
                if hits["rate"] <= 1:
                    return self.send(429, "slow down", headers={"Retry-After": "1"})
                return self.send(200, PAGE.format(n="limited", port=port, path=p, text="rate " * 150, links=""))
            if p == "/members":
                if self.headers.get("X-Audit-Key") != "letmein":
                    return self.send(403, "<html><head><title>Forbidden</title></head><body>no</body></html>")
                return self.send(200, PAGE.format(n="members", port=port, path=p, text="members " * 150, links=""))
            if p == "/challenge":
                return self.send(403, "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>",
                                 headers={"Server": "cloudflare", "cf-mitigated": "challenge"})
            if p == "/" or p.startswith("/p/"):
                n = 0 if p == "/" else int(p.rsplit("/", 1)[1] or 0)
                if every_7th_s and n % 7 == 6:
                    time.sleep(every_7th_s)
                if n > pages:
                    return self.send(404, "not found")
                nxt = [(n * 3 + k) % (pages + 1) for k in (1, 2, 3)]
                links = "".join('<a href="/p/%d">Gardening page %d</a> ' % (i, i) for i in nxt)
                if n == 0:
                    links += '<a href="/old">old</a> <a href="/slow">slow</a> <a href="/limited">limited</a> ' \
                             '<a href="/members">members</a> <a href="/challenge">challenge</a> <a href="/p/9999">missing</a> <a href="/private/x">private</a>'
                text = ("Gardening tools page %d. " % n) + "Soil spade rake hoe pruner watering can " * 25
                # Every page: one large photo (with alt) and one small icon without alt text.
                text += ' <img src="/img/hero.jpg" alt="Garden tools" width="800" height="400"> <img src="/img/icon.png">'
                return self.send(200, PAGE.format(n=n, port=port, path=p, text=text, links=links))
            if p == "/img/hero.jpg":
                return self.send(200, "x" * 300 * 1024, "image/jpeg")
            if p == "/img/icon.png":
                return self.send(200, "x" * 2048, "image/png")
            return self.send(404, "not found")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
