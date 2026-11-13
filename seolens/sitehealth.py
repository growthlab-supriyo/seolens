"""Site Health: whole-domain checks (full domain audits, or folders when asked) plus the sitemap checks about the audited pages."""
import re
import uuid
from datetime import datetime, timezone

from .analyze import detect_platform, detect_tracking, parse_date
from .htmltools import bare_host, get_tags, norm, parse_attributes, parse_url, rel_has, resolve_url, unique
from .net import read_sitemap, robots_allows

CDN_NAMES = {"cf-cache-status": "Cloudflare", "x-hcdn-cache-status": "Hostinger CDN", "x-vercel-cache": "Vercel", "x-nextjs-cache": "Next.js",
             "x-litespeed-cache": "LiteSpeed", "x-fastly-cache": "Fastly"}


REMOVED_SITE_CHECKS = {"Bulk date updates"}


def run_site_health(crawler, report):
    out = _run_site_health(crawler, report)
    out["checks"] = [c for c in out["checks"] if c["check"] not in REMOVED_SITE_CHECKS]
    return out


def _run_site_health(crawler, report):
    s, f, rows = crawler.s, crawler.fetcher, crawler.rows
    checks = []

    def add(category, check, status, result, prio="", fix=""):
        problem = status in ("Fail", "Warn")
        checks.append({"category": category, "check": check, "status": status, "result": str(result), "priority": prio if problem else "", "fix": fix if problem else ""})

    site_add = add
    site_wide = s["mode"] == "domain" and (not crawler.scope or s.get("site_wide_health"))
    if not site_wide:
        site_add = lambda *a, **k: None  # noqa: E731

    host_count = {}
    for r in rows:
        if r.get("Final Status") == 200:
            p = parse_url(r.get("Final URL") or r["URL"])
            if p:
                host_count[p["host"]] = host_count.get(p["host"], 0) + 1
    if not host_count:
        for r in rows:
            p = parse_url(r["URL"])
            if p:
                host_count[p["host"]] = host_count.get(p["host"], 0) + 1
    if not host_count:
        add("Site", "Domain", "Fail", "Could not determine the domain", "Critical", "Check the URLs you entered.")
        return {"checks": checks, "host": "", "site_wide": site_wide, "sitemap_keys": set()}
    host = max(host_count, key=host_count.get)
    on_site = [r for r in rows if bare_host((parse_url(r.get("Final URL") or r["URL"]) or {}).get("host", "")) == bare_host(host)]
    domains = unique([bare_host((parse_url(r["URL"]) or {}).get("host", "")) for r in rows if parse_url(r["URL"])])
    if len(domains) > 1:
        add("Site", "Domains in this audit", "Warn", "%d domains (%s). Site Health and the sitemap checks cover only %s (%d of %d pages)." % (
            len(domains), ", ".join(domains[:5]), host, len(on_site), len(rows)), "Medium", "Audit one domain at a time so Site Health and the Summary describe a single site.")
    origin = "https://" + host
    report({"phase": "site", "step": "Site Health" + ("" if site_wide else " (checks about these pages)")})
    home_res, home_html, home_final = None, "", origin + "/"

    if site_wide:
        home = f.chain(origin + "/")
        if home["error"]:
            add("Availability", "Homepage", "Fail", "Unreachable: " + home["error"], "Critical", "Check hosting, DNS and SSL.")
            if re.search(r"ssl|certificate|handshake", home["error"], re.I):
                add("Security", "SSL certificate", "Fail", home["error"], "Critical", "Fix or renew the SSL certificate (it must cover %s)." % host)
            h2 = f.chain("http://" + host + "/")
            if not h2["error"] and h2["final"].startswith("http:") and h2["response"].status == 200:
                origin, home_res, home_final = "http://" + host, h2["response"], h2["final"]
                home_html = re.sub(r"<!--[\s\S]*?-->", "", home_res.text)
                add("Availability", "Homepage over http://", "Warn", "200 %s (the site works only without HTTPS; the checks below use http://)" % home_final,
                    "Critical", "Install an SSL certificate, then 301 redirect http:// to https://.")
        else:
            home_res, home_final = home["response"], home["final"]
            code = home_res.status
            if code == 200:
                home_html = re.sub(r"<!--[\s\S]*?-->", "", home_res.text)
            add("Availability", "Homepage", "Pass" if code == 200 else "Fail",
                "%d %s%s" % (code, home_final, " (after %d redirect/s)" % (len(home["chain"]) - 1) if len(home["chain"]) > 1 else ""), "Critical", "The homepage must return 200.")
            fixed = [h for h, p in f.chain_fixed.items() if p]
            if fixed:
                add("Security", "SSL certificate chain", "Warn",
                    "Valid certificate, but the server doesn't send its intermediate certificate (%s). Browsers usually download it "
                    "themselves; crawlers, apps and some phones can't, and fail with a certificate error." % ", ".join(fixed),
                    "High", "Install the full certificate chain on the server (for Let's Encrypt: fullchain.pem, not cert.pem), "
                            "then check it at ssllabs.com/ssltest.")
            else:
                add("Security", "SSL certificate", "Pass", "HTTPS loads with a valid certificate")
        home_host = (parse_url(home_final) or {}).get("host", host)
        http = f.chain("http://" + host + "/")
        if http["error"]:
            add("Security", "HTTP → HTTPS redirect", "Info", "http:// version not reachable: " + http["error"])
        else:
            first = http["chain"][0][0]
            if not http["final"].startswith("https:"):
                add("Security", "HTTP → HTTPS redirect", "Fail", "http:// loads without redirecting to https://", "High", "Add a sitewide 301 redirect from http:// to https://.")
            elif first not in (301, 308):
                add("Security", "HTTP → HTTPS redirect", "Warn", "Uses a %d (temporary) redirect" % first, "Medium", "Use a permanent 301 redirect.")
            else:
                add("Security", "HTTP → HTTPS redirect", "Pass", "%d → %s (%d hop/s)" % (first, http["final"], len(http["chain"]) - 1))
        alt_host = host[4:] if host.startswith("www.") else "www." + host
        alt = f.chain("https://" + alt_host + "/")
        if alt["error"]:
            add("Duplicates", "www / non-www", "Warn", "%s does not load over HTTPS: %s" % (alt_host, alt["error"]), "Low",
                "Point %s to the site (with a valid certificate) and 301 redirect it to %s." % (alt_host, home_host))
        else:
            alt_final_host = (parse_url(alt["final"]) or {}).get("host")
            if len(alt["chain"]) > 1 and alt_final_host == home_host:
                c = alt["chain"][0][0]
                if c in (301, 308):
                    add("Duplicates", "www / non-www", "Pass", "%s → %d → %s" % (alt_host, c, home_host))
                else:
                    add("Duplicates", "www / non-www", "Warn", "%s uses a %d (temporary) redirect" % (alt_host, c), "Medium", "Use a permanent 301 redirect.")
            elif len(alt["chain"]) == 1 and alt["response"].status == 200:
                add("Duplicates", "www / non-www", "Fail", "Both %s and %s return 200 (duplicate site)" % (host, alt_host), "High",
                    "301 redirect one version to the other and keep canonicals consistent.")
            else:
                add("Duplicates", "www / non-www", "Warn", "%s ends at %s" % (alt_host, alt["final"]), "Medium", "Make %s 301 redirect to %s." % (alt_host, home_host))

    # robots.txt and the sitemap (also for lists and folders: are these pages blocked / in the sitemap?)
    robots = crawler.robots_raw
    if robots is None:
        res, err = f.get(origin + "/robots.txt", follow=True)
        if not err and res.status == 200 and not re.match(r"^\s*(<!doctype|<html|<head|<body)", res.text[:500], re.I):
            from .net import find_robots_group, parse_robots
            robots = parse_robots(res.text)
            crawler.robots_group = find_robots_group(robots["groups"], "googlebot")
    group = crawler.robots_group
    if robots is not None:
        site_add("Crawling", "robots.txt", "Pass", "Found (%d user-agent group/s)" % len(robots["groups"]))
        if not robots_allows(group, "/") and not robots_allows(group, "/seo-audit-random-page"):
            site_add("Crawling", "robots.txt sitewide block", "Fail", "robots.txt blocks Googlebot from the whole site", "Critical", "Remove 'Disallow: /' unless the site is meant to be hidden.")
        else:
            site_add("Crawling", "robots.txt sitewide block", "Pass", "No sitewide block")
        blocked = [r for r in on_site if parse_url(r["URL"]) and not robots_allows(group, parse_url(r["URL"])["path"] + parse_url(r["URL"])["query"])]
        add("Crawling", "Audited URLs blocked by robots.txt", "Fail" if blocked else "Pass",
            "%d URL/s, e.g. %s" % (len(blocked), ", ".join(r["URL"] for r in blocked[:3])) if blocked else "None", "High", "Unblock pages that should rank (blocked pages can't be crawled).")
        site_add("Crawling", "Sitemap declared in robots.txt", "Pass" if robots["sitemaps"] else "Warn", ", ".join(robots["sitemaps"]) if robots["sitemaps"] else "No 'Sitemap:' line",
                 "Low", "Add 'Sitemap: https://.../sitemap.xml' to robots.txt.")
    else:
        site_add("Crawling", "robots.txt", "Fail", "Missing or invalid", "Medium", "Create a robots.txt that lists your sitemap.")
    sm = crawler.sitemap
    if sm is None:
        cands = (robots["sitemaps"] if robots else []) + [origin + "/sitemap.xml", origin + "/sitemap_index.xml", origin + "/wp-sitemap.xml"]
        sm = read_sitemap(f, list(dict.fromkeys(cands))[:50])
    if not sm:
        if site_wide:
            add("Crawling", "XML sitemap", "Fail", "No XML sitemap found", "High", "Create an XML sitemap and submit it in Google Search Console.")
        else:
            add("Crawling", "Pages in the XML sitemap", "Info", "No XML sitemap found for %s, so it couldn't be checked whether these pages are in it" % host)
    else:
        # Sitemap URLs on the other www / non-www host (e.g. www.example.com in the sitemap, the site at example.com):
        # say so once, then compare pages by path so every page isn't reported as missing from the sitemap.
        crawl_host = str(crawler.host or "").lower()
        other = [u for u in sm["urls"] if (parse_url(u) or {}).get("host", "").lower() not in ("", crawl_host)
                 and bare_host((parse_url(u) or {}).get("host", "")) == bare_host(crawl_host)]
        if crawl_host and other:
            sample = other[0]
            site_add("Crawling", "Sitemap uses the same host as the site", "Warn",
                     "%d of %d sitemap URL/s use %s, but the site is crawled at %s (e.g. %s)" % (
                         len(other), len(sm["urls"]), parse_url(sample)["host"], crawl_host, sample),
                     "Medium", "List the URLs exactly as they should be indexed: same protocol and the same www / non-www host as the canonical URLs.")

            def same_host(u):
                p = parse_url(u)
                if p and p["host"].lower() != crawl_host and bare_host(p["host"]) == bare_host(crawl_host):
                    return p["protocol"] + "://" + crawl_host + p["path"] + p["query"]
                return u
            sm = dict(sm, keys={norm(same_host(u)) for u in sm["urls"]})
        partial = sm["is_index"] and sm["child_read"] < sm["child_total"]
        site_add("Crawling", "XML sitemap", "Pass", sm["url"] + (" (index: read %d of %d child sitemaps)" % (sm["child_read"], sm["child_total"]) if sm["is_index"] else ""))
        site_add("Crawling", "Sitemap URL count", "Info", "%d URLs%s" % (len(sm["keys"]), " (partial)" if partial else ""))
        if sm["latest"]:
            t = parse_date(sm["latest"])
            age = round((datetime.now(timezone.utc).timestamp() - t) / 86400) if t else None
            site_add("Crawling", "Sitemap freshness", "Pass" if age is None or age <= 180 else "Warn",
                     "Latest lastmod: %s%s" % (sm["latest"], " (%d days ago)" % age if age is not None else ""), "Low", "Keep lastmod dates accurate so Google re-crawls updated pages.")
        else:
            site_add("Crawling", "Sitemap freshness", "Info", "No lastmod dates in sitemap")
        indexable = [r for r in on_site if r.get("Indexable") == "Yes"]
        missing = [r for r in indexable if norm(r.get("Final URL") or r["URL"]) not in sm["keys"]]
        note = " (sitemap read partially, may be overstated)" if partial else ""
        lst = lambda arr, fmt: ", ".join(fmt(x) for x in arr[:50]) + (" … and %d more (see the Page Wise Audit tab)" % (len(arr) - 50) if len(arr) > 50 else "")  # noqa: E731
        if indexable:
            add("Crawling", "Indexable pages missing from sitemap", "Warn" if len(missing) / len(indexable) > 0.1 else "Pass",
                "%d of %d%s missing%s" % (len(missing), len(indexable), note, ": " + lst(missing, lambda r: r.get("Final URL") or r["URL"]) if missing else " (all in the sitemap)"),
                "Medium", "Add every indexable page to the XML sitemap.")
        junk = [r for r in on_site if r.get("Indexable") != "Yes" and norm(r["URL"]) in sm["keys"]]
        why = lambda r: ("redirects to " + str(r.get("Final URL"))) if r.get("Redirect Hops") else re.sub(r"^No\s*[–-]\s*", "", str(r.get("Indexable", "")))  # noqa: E731
        add("Crawling", "Non-indexable URLs in sitemap", "Warn" if junk else "Pass", "%d URL/s: %s" % (len(junk), lst(junk, lambda r: r["URL"] + " (" + why(r) + ")")) if junk else "None",
            "Medium", "List only 200, indexable, canonical URLs in the sitemap: replace redirecting URLs with their final URL and remove error or noindex pages.")

    if site_wide:
        fake = origin + "/seo-audit-check-" + uuid.uuid4().hex[:8]
        res, err = f.get(fake)
        if err:
            add("Crawling", "404 handling", "Info", "Could not test: " + err)
        else:
            fc = res.status
            if fc in (404, 410):
                add("Crawling", "404 handling", "Pass", "Missing pages return %d" % fc)
            elif 300 <= fc < 400 and res.header("location"):
                add("Crawling", "404 handling", "Warn", "Missing pages redirect (%d) to %s" % (fc, resolve_url(res.header("location"), fake)), "Medium", "Return a real 404 status for missing pages instead of redirecting.")
            else:
                add("Crawling", "404 handling", "Fail" if fc == 200 else "Warn", "Missing pages return %d%s" % (fc, " (soft 404)" if fc == 200 else ""), "High", "Configure the server to return a 404 status for missing pages.")

    if site_wide and home_res is not None:
        hh = home_res.headers
        css = re.search(r"<link\b[^>]*rel=[\"']?stylesheet[\"']?[^>]*>", home_html, re.I)
        href = (re.search(r"href=[\"']([^\"']+)[\"']", css.group(0), re.I).group(1) if css and re.search(r"href=[\"']([^\"']+)[\"']", css.group(0), re.I) else None) or \
               (re.search(r"<script\b[^>]*\ssrc=[\"']([^\"']+\.js(?:\?[^\"']*)?)[\"']", home_html, re.I) or [None, None])[1]
        asset = resolve_url(href, home_final) if href else ""
        max_age = lambda v: (lambda m: int(m.group(1)) if m else None)(re.search(r"(?:^|[,\s])(?:s-)?max-age\s*=\s*(\d+)", str(v or ""), re.I))  # noqa: E731

        def explain(cc):
            v = str(cc or "").lower()
            age = max_age(v)
            if not v or v == "none":
                return "no caching rule (browser guesses; may download again each visit)"
            if "no-store" in v:
                return "not saved (no-store: downloaded fresh on every visit)"
            if "no-cache" in v or age == 0:
                return "re-checked every visit (no-cache: browser asks the server if it changed)"
            if age is not None:
                return "saved for %s (max-age=%d%s" % (_span(age), age, ", immutable: repeat visits reuse the saved copy without re-checking)" if "immutable" in v else ": repeat visits reuse the saved copy)")
            return "rule set but no duration (%s)" % cc

        asset_cc = ""
        if asset:
            ar, aerr = f.get(asset, follow=True)
            if not aerr and ar.status == 200:
                asset_cc = ar.header("cache-control") or "none"
        cdn = [(h, hh.get(h)) for h in ("cf-cache-status", "x-cache", "x-vercel-cache", "x-nextjs-cache", "cdn-cache-status", "x-hcdn-cache-status",
                                       "x-cache-status", "x-litespeed-cache", "x-proxy-cache", "x-fastly-cache") if hh.get(h)]
        html_part = "Pages (HTML): " + explain(hh.get("cache-control")) + ("; change check available (ETag/Last-Modified)" if hh.get("etag") or hh.get("last-modified") else "")
        cdn_part = " · CDN: " + ", ".join("%s %s" % (CDN_NAMES.get(h, "CDN"), v) for h, v in cdn) if cdn else " · CDN: none detected (no CDN cache header)"
        if asset_cc:
            long_cache = (max_age(asset_cc) or 0) >= 7 * 86400 or "immutable" in asset_cc.lower()
            add("Performance", "Caching (browser & CDN)", "Pass" if long_cache else "Warn",
                "CSS/JS files: %s · %s%s" % (explain(asset_cc), html_part, cdn_part), "Medium",
                "Serve CSS, JS, fonts and images with Cache-Control: public, max-age=31536000, immutable (with versioned file names), and let a CDN cache what it can.")
        else:
            add("Performance", "Caching (browser & CDN)", "Info", html_part + cdn_part)
        for header, label, prio in (("strict-transport-security", "HSTS", "Medium"), ("content-security-policy", "Content-Security-Policy", "Low")):
            v = hh.get(header)
            add("Security", label + " header", "Pass" if v else "Warn", v[:120] if v else "Missing", prio, "Add the %s response header." % header.title())
        if home_final.startswith("https:") and home_html:
            mixed = len(re.findall(r"\s(?:src|srcset)=[\"']http://", home_html, re.I)) + \
                sum(1 for a in (parse_attributes(t) for t in get_tags(home_html, "link")) if rel_has(a, "stylesheet") and (a.get("href") or "").lower().startswith("http://"))
            add("Security", "Mixed content (homepage)", "Warn" if mixed else "Pass", "%d http:// resource/s" % mixed if mixed else "None", "Medium", "Load every image, script and stylesheet over https://.")
        server = hh.get("server", "")
        cdns = [n for n, test in (("Cloudflare", lambda: hh.get("cf-ray") or "cloudflare" in server.lower()), ("CloudFront", lambda: hh.get("x-amz-cf-id")),
                                  ("Fastly", lambda: "fastly" in (hh.get("x-served-by", "") + hh.get("via", "")).lower()), ("Vercel", lambda: hh.get("x-vercel-id")),
                                  ("Netlify", lambda: hh.get("x-nf-request-id")), ("Akamai", lambda: "akamai" in server.lower())) if test()]
        add("Technology", "Server / CDN", "Info", (server or "Server header hidden") + (" | CDN: " + ", ".join(cdns) if cdns else " | No CDN detected"))
        if home_html:
            gen = re.search(r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']([^\"']+)", home_html, re.I)
            add("Technology", "CMS / Platform", "Info", detect_platform(home_html, gen.group(1) if gen else "", hh))
            if crawler.home_tags:
                # Measured in a real browser (JavaScript rendering): what actually loads, not only what the HTML mentions.
                loaded = not crawler.home_tags.startswith("Nothing loaded")
                add("Technology", "Analytics & tags (homepage)", "Pass" if loaded else "Warn", crawler.home_tags, "Medium",
                    "No analytics tag loaded on the homepage in a browser (before cookie consent). Check that GA4 / GTM is installed and not blocked.")
            else:
                tr = detect_tracking(home_html)
                add("Technology", "Analytics & tags (homepage)", "Info", ", ".join(tr["list"]) if tr["list"] else "None found in the HTML (tags loaded by GTM, Shopify pixels or consent tools may still run)")
            icon = any("icon" in (a.get("rel") or "").lower() for a in (parse_attributes(t) for t in get_tags(home_html, "link")))
            if not icon:
                fr, ferr = f.get(origin + "/favicon.ico", follow=True)
                icon = not ferr and fr.status == 200 and "image" in fr.header("content-type").lower()
            add("Technology", "Favicon", "Pass" if icon else "Warn", "Found" if icon else "Missing", "Low", "Add a favicon (shown in Google mobile results and browser tabs).")

    # Bulk date updates (no requests)
    dated = []
    for r in rows:
        if r.get("Redirect Hops") or r.get("Final Status") != 200:
            continue
        v = str(r.get("Published / Modified", ""))
        pub, mod = re.search(r"Pub: (\d{4}-\d{2}-\d{2})", v), re.search(r"Mod: (\d{4}-\d{2}-\d{2})", v)
        if pub and mod:
            dated.append((pub.group(1), mod.group(1)))
    if len(dated) < 5:
        add("Content", "Bulk date updates", "Info", ("Only %d page/s have" % len(dated) if dated else "No audited page has") + " both a published and a modified date (5+ needed for this check)")
    else:
        by_day = {}
        for p, m in dated:
            if m != p:
                by_day[m] = by_day.get(m, 0) + 1
        top = max(by_day, key=by_day.get) if by_day else None
        share = by_day[top] / len(dated) if top else 0
        add("Content", "Bulk date updates", "Warn" if share >= 0.6 else "Pass",
            "%d of %d dated pages show modified %s (%d%%)" % (by_day[top], len(dated), top, round(share * 100)) if top else "No updated pages", "Medium",
            "Many pages share one 'updated' day. If the content didn't really change, stop bulk-updating dates: Google may ignore or distrust them.")
    return {"checks": checks, "host": host, "site_wide": site_wide, "sitemap_keys": sm["keys"] if sm else set(), "sitemap": sm}


def _span(s):
    if s >= 31536000:
        return "%d year%s" % (round(s / 31536000), "s" if s >= 63072000 else "")
    if s >= 86400:
        return "%d day%s" % (round(s / 86400), "s" if s >= 172800 else "")
    if s >= 3600:
        return "%d hour%s" % (round(s / 3600), "s" if s >= 7200 else "")
    return "%d min" % round(s / 60) if s >= 60 else "%d sec" % s
