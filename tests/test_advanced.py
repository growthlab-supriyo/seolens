"""The advanced checks: URL parameters, canonical clusters, main content, indexability conflicts, JavaScript comparison,
raw vs rendered links, pagination, URL structure and anchor distribution."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openpyxl import load_workbook

from seolens.advanced import param_kind, safe_url
from seolens.audit import run_audit
from seolens.crawler import compare_links, compare_render

NAV = "".join('<a href="/menu-%d/">Menu item %d</a> ' % (i, i) for i in range(30))
FOOTER = "Footer text about the clinic, opening hours and address. " * 30
WORDS = "Implants replace missing teeth with titanium posts and crowns that look natural. "
OTHER = "Root canal treatment removes infected pulp, cleans the canal and seals the tooth. "


def page(title, body, canonical=None, robots=None, head=""):
    return ("<!doctype html><html lang='en'><head><title>%s</title><meta name='description' content='%s for the test site, long enough "
            "for the check.'>%s%s%s</head><body><header><nav>%s</nav></header><main><h1>%s</h1>%s</main><footer>%s</footer></body></html>"
            % (title, title, "<link rel='canonical' href='%s'>" % canonical if canonical else "",
               "<meta name='robots' content='%s'>" % robots if robots else "", head, NAV, title, body, FOOTER))


def make_site():
    def routes(port):
        o = "http://127.0.0.1:%d" % port
        home = page("Dental clinic home", "<p>" + WORDS * 20 + "</p>"
                    "<p>Read about <a href='/services/?utm_source=home'>dental implants</a> and "
                    "<a href='/services/'>click here</a>, <a href='/services/'><img src='/i.png'></a>, "
                    "<a href='/services/?sort=asc'>implants sorted</a>, <a href='/root-canal/'>root canal</a>, "
                    "<a href='/blog/'>blog</a>, <a href='/contact/'>contact</a>, <a href='/noindex-page/'>offers</a>, "
                    "<a href='/a/b/c/d/e/f/deep-page/'>deep</a>, <a href='/members-area/?sid=SECRET123'>login</a>.</p>", o + "/")
        return {
            "/": home,
            "/services/": page("Dental implants", "<p>" + WORDS * 60 + "</p>", o + "/services/"),
            "/services/?sort=asc": page("Dental implants", "<p>" + WORDS * 60 + "</p>", o + "/services/?sort=asc"),
            "/services/?utm_source=home": page("Dental implants", "<p>" + WORDS * 60 + "</p>", o + "/services/"),
            "/root-canal/": page("Root canal", "<p>" + OTHER * 60 + "</p>", o + "/services/"),
            "/contact/": page("Contact us", "<p>Call us today.</p>", o + "/contact/"),
            "/noindex-page/": page("Offers", "<p>" + WORDS * 40 + "</p>", o + "/services/", robots="noindex, follow"),
            "/blog/": page("Blog", "<p>" + " ".join("Post number %d about gums and teeth." % i for i in range(80)) + "</p>"
                           "<a href='/blog/page/2/' class='next'>Next</a>", o + "/blog/"),
            "/blog/page/2/": page("Blog page 2", "<p>" + " ".join("Older post %d about braces and aligners." % i for i in range(80)) + "</p>",
                                  o + "/blog/"),
            "/a/b/c/d/e/f/deep-page/": page("Deep page", "<p>" + WORDS * 40 + "</p>", o + "/a/b/c/d/e/f/deep-page/"),
            "/members-area/?sid=SECRET123": page("Login", "<p>Log in.</p>"),
        }

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="text/html; charset=utf-8"):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            port = self.server.server_address[1]
            o = "http://127.0.0.1:%d" % port
            if self.path == "/robots.txt":
                return self.send(200, "User-agent: *\nDisallow: /private/\nSitemap: %s/sitemap.xml\n" % o, "text/plain")
            if self.path == "/sitemap.xml":
                locs = "".join("<url><loc>%s%s</loc></url>" % (o, p) for p in ("/", "/services/", "/noindex-page/", "/root-canal/",
                                                                                 "/private/secret/", "/services/?sort=asc"))
                return self.send(200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % locs,
                                 "application/xml")
            r = routes(port)
            if self.path in r:
                return self.send(200, r[self.path])
            if self.path.startswith("/menu-") or self.path == "/i.png":
                return self.send(200, page("Menu", "<p>" + WORDS * 40 + "</p>"))
            return self.send(404, "not found")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    srv = make_site()
    out = tmp_path_factory.mktemp("adv")
    try:
        res = run_audit({"site": "http://127.0.0.1:%d/" % srv.server_address[1], "max_pages": 80, "check_external": False,
                         "include_query": True, "out_dir": str(out)})
    finally:
        srv.shutdown()
    return res


def rows_of(ws, header_text):
    """{first column: row values} for the table whose header row starts with header_text."""
    rows, on, head = {}, False, None
    for r in ws.iter_rows(values_only=True):
        if r and r[0] == header_text:
            on, head = True, r
            continue
        if on:
            if not r or r[0] is None:
                on = False  # end of this table; a later table may have the same first header
                continue
            rows.setdefault(r[0], []).append(dict(zip(head, r)))
    return rows


def test_parameters_and_privacy():
    assert param_kind("utm_source") == "tracking" and param_kind("sessionid") == "private" and param_kind("sort") == "sorting"
    assert safe_url("https://x.com/a?sessionid=abc&sort=asc") == "https://x.com/a?sessionid=[hidden]&sort=asc"


def test_render_comparison():
    raw_row = {"URL": "https://x.com/", "Meta Title": "Shop", "Meta Description": "Missing", "H1 Text": "Missing",
               "Canonical URL": "https://x.com/", "Meta Robots": "robots: noindex", "Schema Types": "None", "Word Count": 20}
    row = dict(raw_row, **{"Meta Description": "Best shop", "H1 Text": "Shop", "Canonical URL": "https://x.com/home",
                           "Meta Robots": "Not specified", "Word Count": 900})
    diff = {d[0]: d[1] for d in compare_render(raw_row, {"links": []}, row, {"links": []})}
    assert diff == {"Title": "Unchanged", "Meta description": "Added", "H1": "Added", "Canonical": "Modified", "Robots": "Removed",
                    "Structured data": "Unchanged", "Main content words": "Modified", "Internal links": "Unchanged"}
    raw_ex = {"links": [["https://x.com/a", "About", "m"], ["https://x.com/old", "Prices", "c"]]}
    ex = {"links": [["https://x.com/a", "About", "m"], ["https://x.com/new", "Prices", "c"], ["https://x.com/js", "Only JS", "c"]],
          "js_nav": 2}
    links = compare_links(raw_row, raw_ex, row, ex)
    assert links["added"] == [["https://x.com/js", "Only JS"]] and not links["removed"]
    assert links["changed"] == [["Prices", "https://x.com/old", "https://x.com/new"]] and links["js_nav"] == 2


def test_advanced_checks_in_report(audit):
    wb = load_workbook(audit["xlsx"])
    assert len([s for s in wb.sheetnames if not s.startswith("__")]) == 11  # no new sheets
    tech = rows_of(wb["06 Technical SEO"], "URL")
    by_url = {k.rsplit(":", 1)[-1].split("/", 1)[-1]: v[0] for k, v in tech.items()}
    # Indexability conflict with evidence: noindex page in the sitemap, also canonicalised
    assert "Noindex URL in the XML sitemap" in by_url["noindex-page/"]["Indexability Conflicts"]
    assert "canonical to another URL at the same time" in by_url["noindex-page/"]["Indexability Conflicts"]
    # Pagination: page 2 canonicalised to page 1
    assert "canonical points to page 1" in by_url["blog/page/2/"]["Pagination"]
    assert by_url["blog/"]["Pagination"].startswith("Page 1 · next page link")
    # Rendering off: never claimed
    assert by_url["services/"]["JS Rendering Differences"] == "Not measured"
    # Canonical cluster table
    clusters = rows_of(wb["06 Technical SEO"], "Cluster ID")
    kinds = {r["Source URL"].split("/", 3)[3]: r["Conflict Type"] for v in clusters.values() for r in v}
    assert kinds["root-canal/"].startswith("Possible mismatch")
    assert kinds["services/"].startswith("Self-canonical")
    # Parameter table: private values hidden, tracking link flagged, sitemap blocked URL reported
    params = rows_of(wb["06 Technical SEO"], "URL")
    allp = [r for v in params.values() for r in v if r.get("Clean URL")]
    assert any("[hidden]" in r["URL"] for r in allp) and not any("SECRET123" in str(r) for r in allp)
    assert any("tracking parameter" in r["Finding"] for r in allp)
    assert any("Same content as" in r["Finding"] for r in allp if "sort=asc" in r["URL"])
    checks = rows_of(wb["__Raw Checks"], "Status")
    text = str(checks)
    assert "Sitemap URLs blocked by robots.txt" in text and "/private/secret/" in text
    # Main content vs total, page-type threshold
    onpage = {k.rsplit(":", 1)[-1].split("/", 1)[-1]: v[0] for k, v in rows_of(wb["05 On-Page SEO"], "URL").items()}
    svc = onpage["services/"]
    assert svc["Total Words"] > svc["Main Content Words"] > 0 and svc["Boilerplate Words"] > 0
    assert svc["Extraction Method"].startswith("<main> element")
    assert onpage["contact/"]["Content Flag"].startswith("Not judged")
    # URL structure on Page Audit
    pa = {k.rsplit(":", 1)[-1].split("/", 1)[-1]: v[0] for k, v in rows_of(wb["04 Page Audit"], "URL").items()}
    assert pa["a/b/c/d/e/f/deep-page/"]["URL Depth (folders)"] == 7 and pa["services/"]["JS Rendering"] == "Not measured"
    # Anchor distribution and the issue rules
    links = rows_of(wb["07 Internal Links"], "Edge Type")
    assert any("generic anchor" in r["Issue"] for r in links.get("Anchor distribution", []))
    rules = {r["Rule"] for v in rows_of(wb["03 Issue Details"], "Issue ID").values() for r in v}
    assert {"Tracking parameters in internal links", "Canonical to a different page", "Paginated page canonicalised to page 1",
            "Noindex page with a canonical to another URL", "Parameter URL duplicates another page", "Links without anchor text"} <= rules
    # Dashboard cards and the app's counts
    assert audit["advanced"]["param_urls"] >= 3 and audit["advanced"]["canonical_conflicts"] >= 1
    assert audit["advanced"]["render_diff"] is None
    dash = [c.value for row in wb["01 Dashboard"]["B40:S43"] for c in row if c.value is not None]
    assert "Parameter URLs" in dash and "Not measured" in dash
