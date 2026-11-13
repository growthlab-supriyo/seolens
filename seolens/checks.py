"""After the crawl: link targets, image sizes, outside canonical/hreflang targets, link graph, cross-page checks, scores."""
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .analyze import ISSUES, MINHASH_SIZE, types_of
from .htmltools import (bare_host, get_tags, is_bot_check_page, is_internal, norm, parse_attributes, parse_url, rel_has, remove_blocks,
                        resolve_url, same_url, unique)
from .net import BROWSER_UA

MAX_INTERNAL_CHECKS, MAX_EXTERNAL_CHECKS, MAX_IMAGE_CHECKS, OUTSIDE_TARGET_MAX = 1500, 500, 300, 60
HEAVY_KB = 200
DEEP_CLICKS = 4
NEAR_DUP = 0.8
# Checks taken out of the audit (no SEO value for the reader); also dropped from crawls saved earlier.
REMOVED_ISSUES = {"Heading levels skipped", "target=_blank without noopener", "Split anchor links",
                  # dates, guessed performance and JavaScript risk: removed on request (CrUX measures real performance)
                  "Conflicting dates", "Modified before published", "Future date", "Date changes on every load",
                  "Title year newer than content", "Dates missing from meta & schema", "Content may depend on JavaScript",
                  "High performance risk", "Images missing width/height"}
PRIORITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
PRIORITY_WEIGHT = {"Critical": 20, "High": 8, "Medium": 3, "Low": 1}
EXTRA_ISSUES = {
    "Intermittent server error (5xx)": ["High", "This page failed with a server error (5xx) during the crawl but loaded when it was fetched again slowly. The server fails under load: check hosting limits (PHP workers, memory, database connections) and caching, because Googlebot can hit the same errors."],
    "Heavy images": ["Medium", "Images of 200 KB or more slow the page down (LCP). Compress them, serve WebP/AVIF and size them to how they are shown (listed in the Heavy Images column)."],
    "Content only appears with JavaScript": ["High", "Most of this page's text appears only after JavaScript runs. Google renders JavaScript later and not always completely: render the main content on the server (SSR) or pre-render it."],
    "Links only appear with JavaScript": ["Medium", "Some internal links exist only after JavaScript runs, so crawlers that read the HTML (and Google, until it renders) miss them. Put important links in the server HTML as normal <a href> links."],
    "Links without anchor text": ["Low", "Some links have no text, no image alt and no aria-label, so search engines and screen readers can't tell where they go. Add link text, an alt on the image, or an aria-label."],
    "Tracking parameters in internal links": ["Medium", "Internal links on this page carry tracking parameters (utm_, gclid, fbclid…). They create duplicate URLs and overwrite the visitor's real traffic source in analytics: link to the clean URL instead."],
    "Parameter URL duplicates another page": ["Medium", "This ?parameter URL shows the same content as the clean URL but is indexable with a self canonical. Point its canonical to the clean URL, or stop linking to the variant."],
    "Canonical to a different page": ["Medium", "This page declares as its canonical a page with clearly different content. Google may ignore the hint or drop this page: point the canonical to itself unless the two pages really are the same."],
    "Paginated page canonicalised to page 1": ["Medium", "Page 2+ of a series lists different items but points its canonical to page 1, so those items may not be found through it. Give each page of the series a self-referencing canonical."],
    "Noindex page with a canonical to another URL": ["Medium", "This page is noindex and also points its canonical to another URL: mixed signals. Use one: a canonical (for a duplicate) or noindex (to keep it out of search)."],
    "JavaScript changes SEO tags": ["High", "The title, canonical or robots tag in the server HTML differs from what JavaScript sets in the browser. Google may use either: make the server HTML match the final version."],
}


def priority(name):
    return (ISSUES.get(name) or EXTRA_ISSUES.get(name) or ["Medium"])[0]


def fix_text(name):
    return (ISSUES.get(name) or EXTRA_ISSUES.get(name) or ["", ""])[1]


def fmt_bytes(b):
    return "%.1f MB" % (b / 1048576) if b >= 1048576 else "%d KB" % round(b / 1024)


def listing(arr, n=30, more_note=" (filter the Page Wise Audit tab)"):
    return ", ".join(arr[:n]) + (" … and %d more%s" % (len(arr) - n, more_note) if len(arr) > n else "")


class PostChecks:
    def __init__(self, crawler, report, stop):
        self.c = crawler
        self.rows, self.ex = crawler.rows, crawler.extras
        self.f = crawler.fetcher
        self.report = report
        self.stop = stop
        self.checks = []            # Site Health rows from these checks
        self.page_issues = {}       # row index -> [issue]
        self.broken_rows = []
        self.graph = {}
        self.res = {}               # "int|key" / "ext|key" / "img|key" -> result

    def add(self, category, check, status, result, prio="", fix=""):
        problem = status in ("Fail", "Warn")
        self.checks.append({"category": category, "check": check, "status": status, "result": str(result),
                            "priority": prio if problem else "", "fix": fix if problem else ""})

    def issue(self, i, name):
        self.page_issues.setdefault(i, []).append(name)

    def is_page(self, r):
        return r.get("Final Status") == 200 and r.get("Meta Title") != "N/A (non-HTML)"

    # ---------------------------------------------------------------- requests after the crawl

    def run_requests(self):
        known = set()
        for r in self.rows:
            known.add(norm(r["URL"]))
            if r.get("Final URL"):
                known.add(norm(r["Final URL"]))
        internal, external, images = {}, {}, {}
        for i, ex in enumerate(self.ex):
            sp = parse_url(ex["source"])
            if not sp:
                continue
            for u, _a, _w in ex["links"]:
                p = parse_url(u)
                if not p:
                    continue
                k = norm(u)
                if is_internal(bare_host(p["host"]), bare_host(sp["host"])):
                    if k not in known:
                        internal.setdefault(k, [u, 0])[1] += 1
                else:
                    external.setdefault(k, [u, 0])[1] += 1
            for u in ex["images"]:
                images.setdefault(norm(u), [u, 0])[1] += 1
        top = lambda m, n: [v[0] for v in sorted(m.values(), key=lambda x: -x[1])[:n]]  # noqa: E731
        int_list = top(internal, MAX_INTERNAL_CHECKS)
        ext_list = top(external, MAX_EXTERNAL_CHECKS) if self.c.s.get("check_external", True) else []
        img_list = top(images, MAX_IMAGE_CHECKS)
        self.skipped = {"int": len(internal) - len(int_list), "ext": len(external) - len(ext_list) if self.c.s.get("check_external", True) else 0,
                        "img": len(images) - len(img_list), "ext_off": not self.c.s.get("check_external", True)}
        jobs = [("int", u) for u in int_list] + [("ext", u) for u in ext_list] + [("img", u) for u in img_list]
        total = len(jobs)
        self.report({"phase": "checking", "check_total": total, "check_done": 0,
                     "log": "To check: %d internal link target/s that weren't crawled, %d external URL/s, %d image size/s" % (len(int_list), len(ext_list), len(img_list))})
        ua = BROWSER_UA if self.c.browser else None

        def one(job):
            kind, u = job
            if kind == "img":
                res, err = self.f.get(u, follow=True, headers={"Range": "bytes=0-0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}, user_agent=ua)
                if err:
                    return job, {"e": err}
                if res.status not in (200, 206):
                    return job, {"e": "HTTP %d" % res.status}
                m = re.search(r"/(\d+)\s*$", res.header("content-range"))
                size = int(m.group(1)) if m else (int(res.header("content-length") or 0) or len(res.content)) if res.status == 200 else 0
                return job, {"s": size}
            res, err = self.f.get(u, follow=(kind == "ext"), user_agent=ua if kind == "int" else BROWSER_UA)
            if err:
                return job, {"e": err}
            out = {"c": res.status}
            if kind == "int" and 300 <= res.status < 400:
                out["l"] = resolve_url(res.header("location"), u) or ""
            return job, out

        done = 0

        def record(job, out):
            nonlocal done
            self.res[job[0] + "|" + norm(job[1])] = out
            done += 1
            if done % 10 == 0 or done == total:
                self.report({"phase": "checking", "check_done": done, "check_total": total})

        # The audited site's own links go at the crawl's polite speed (same server); other sites and images 10 at a time.
        own = [j for j in jobs if j[0] == "int"]
        other = [j for j in jobs if j[0] != "int"]
        with ThreadPoolExecutor(max_workers=10) as pool:
            step = max(1, min(self.c.conc, 6))
            for k in range(0, len(own), step):
                for job, out in pool.map(one, own[k:k + step]):
                    record(job, out)
                if self.stop.is_set():
                    return
                if self.c.delay:
                    time.sleep(min(self.c.delay, 5))
            for job, out in pool.map(one, other):
                record(job, out)
                if self.stop.is_set():
                    return
        # A busy server (5xx, 429) is asked once more after a pause, one at a time, before a link is called broken.
        retry = [(k, u) for k, u in jobs if k in ("int", "ext") and (lambda r: r and (r.get("c", 0) >= 500 or r.get("c") == 429))(self.res.get(k + "|" + norm(u)))]
        if retry:
            self.report({"log": "Rechecking %d link/s that answered with a server error or 'too many requests'" % len(retry)})
            time.sleep(3)
            for job in retry:
                if self.stop.is_set():
                    return
                _, out = one(job)
                self.res[job[0] + "|" + norm(job[1])] = out
                time.sleep(0.5)

    # ---------------------------------------------------------------- links, depth, orphans, Link Score, heavy images

    def analyze_links(self, site):
        rows = self.rows
        by_url, src_row = {}, {}
        for i, r in enumerate(rows):
            hops = r.get("Redirect Hops") or 0
            final = r.get("Final URL") or r["URL"]
            target = {"idx": str(r.get("Indexable", "")), "canon": self.ex[i].get("canonical", "")}
            by_url[norm(r["URL"])] = dict({"init": r.get("Initial Status"), "fin": r.get("Final Status"), "hops": hops, "final": final}, **({} if hops else target))
            fk = norm(final)
            by_url.setdefault(fk, dict({"init": r.get("Final Status"), "fin": r.get("Final Status"), "hops": 0, "final": final}, **target))
            if fk not in src_row or hops == 0:
                src_row[fk] = i
        names = {404: "404 Not Found", 410: "410 Gone", 401: "401 Login required", 403: "403 Forbidden", 429: "429 Too many requests", 999: "999 Blocks bots"}
        code_text = lambda c: names.get(c) or ("%d Server error" % c if c >= 500 else str(c))  # noqa: E731

        def classify(url, internal):
            a = by_url.get(norm(url)) if internal else None
            if a:
                if a["init"] == "Error" or a["fin"] == "Error":
                    return "broken", "Doesn't load (see the Page Wise Audit tab)"
                fin = int(a["fin"])
                if a["hops"]:
                    return ("broken", "%s → %s" % (a["init"], code_text(fin))) if fin >= 400 else ("redirect", "%s → %s" % (a["init"], a["final"]))
                if fin in (401, 403, 429):
                    return "unverified", code_text(fin)
                if fin >= 400:
                    return "broken", code_text(fin)
                if "noindex" in a.get("idx", ""):
                    return "noindex", "Page is noindex"
                if "canonicalised elsewhere" in a.get("idx", ""):
                    return "canonicalised", "Canonical points to " + (a.get("canon") or "another URL")
                return ("ok",)
            r = self.res.get(("int|" if internal else "ext|") + norm(url))
            if not r:
                return ("unchecked",)
            if "e" in r:
                return ("broken", "Doesn't load (domain not found)") if re.search(r"dns|domain not found|resolve", r["e"], re.I) else ("unverified", "No answer: " + r["e"][:60])
            c = r["c"]
            if 300 <= c < 400:
                return ("redirect", "%d → %s" % (c, r.get("l") or "?")) if internal else ("ok",)
            if c in (404, 410) or c >= 500:
                return "broken", code_text(c)
            if c >= 400:
                return "unverified", code_text(c)
            return ("ok",)

        fixes = {"broken|Internal": "Point the link to a working page, or 301-redirect the missing URL to the closest match.",
                 "broken|External": "The other site's page is gone: update the link or remove it.",
                 "redirect|Internal": "Link straight to the final URL (saves a redirect for users and crawlers).",
                 "unverified|Internal": "Open it in a browser to confirm: the server refused the automated check.",
                 "unverified|External": "Open it in a browser to confirm: many sites block automated checks.",
                 "noindex|Internal": "Link to an indexable page instead, or remove the noindex if this page should rank.",
                 "canonicalised|Internal": "Link straight to the canonical URL, so visitors and Google land on the page that ranks."}
        labels = {"broken": "Broken", "redirect": "Redirect", "unverified": "Couldn't verify", "noindex": "Links to noindex", "canonicalised": "Links to canonicalised"}
        order = {"broken|Internal": 0, "broken|External": 1, "redirect|Internal": 2, "noindex|Internal": 3, "canonicalised|Internal": 4,
                 "unverified|Internal": 5, "unverified|External": 6}
        broken_on, inbound_from, adjacent, menu_rows, entries = {}, {}, {}, {}, []
        broken_urls = {}  # row index -> the broken link URLs on that page (each once)
        uniq = {k: {} for k in order}
        internal_links, link_pages = 0, set()
        for i, ex in enumerate(self.ex):
            source = ex["source"]
            sp = parse_url(source)
            if not sp or not ex["links"]:
                continue
            link_pages.add(source)
            sk = norm(source)
            for url, anchor, where in ex["links"]:
                p = parse_url(url)
                if not p:
                    continue
                internal = is_internal(bare_host(p["host"]), bare_host(sp["host"]))
                typ = "Internal" if internal else "External"
                if internal:
                    internal_links += 1
                    a = by_url.get(norm(url))
                    tk = norm(a["final"] if a and a["hops"] else url)
                    if tk != sk:
                        inbound_from.setdefault(tk, []).append(source) if source not in inbound_from.get(tk, []) else None
                        adjacent.setdefault(sk, []).append(tk)
                c = classify(url, internal)
                if c[0] in ("ok", "unchecked"):
                    continue
                bucket = c[0] + "|" + typ
                if bucket in uniq:
                    u = uniq[bucket].setdefault(norm(url), {"url": url, "status": c[1], "pages": 0})
                    u["pages"] += 1
                if c[0] == "broken" and sk in src_row:
                    broken_on[src_row[sk]] = broken_on.get(src_row[sk], 0) + 1
                    lst = broken_urls.setdefault(src_row[sk], [])
                    if url not in lst:
                        lst.append(url)
                gk = "" if where == "c" else bucket + "|" + norm(url)
                if gk and gk in menu_rows:
                    menu_rows[gk]["row"][1] += 1
                    continue
                e = {"o": order.get(bucket, 9), "row": [source, 1, url, anchor, "Content" if where == "c" else "Menu / header / footer / sidebar", typ, c[1], labels[c[0]], fixes.get(bucket, "")]}
                if gk:
                    menu_rows[gk] = e
                entries.append(e)
        entries.sort(key=lambda e: (e["o"], -e["row"][1]))
        self.broken_rows = [e["row"] for e in entries[:20000]]

        # Click depth from the start page, orphans, Link Score.
        discovered = len(self.c.queue)
        capped = discovered > len(rows)
        orphan_ok = self.c.s["mode"] == "domain" and self.c.s.get("follow_links", True)
        scope = self.c.scope or self.c.start_path
        start_row = -1
        if orphan_ok:
            for i, r in enumerate(rows):
                p = parse_url(r.get("Final URL") or r["URL"])
                if p and r.get("Final Status") == 200 and ((p["path"].rstrip("/").lower() == scope) if scope else p["path"] == "/"):
                    start_row = i
                    break
        depth = {}
        if start_row != -1:
            level = [norm(rows[start_row].get("Final URL") or rows[start_row]["URL"])]
            depth[level[0]] = 0
            d = 1
            while level:
                nl = []
                for k in level:
                    for t in adjacent.get(k, []):
                        if t not in depth:
                            depth[t] = d
                            nl.append(t)
                level, d = nl, d + 1
        node_keys = [norm(r.get("Final URL") or r["URL"]) for r in rows if self.is_page(r) and not r.get("Redirect Hops")]
        node_at = {}
        for n, k in enumerate(node_keys):
            node_at.setdefault(k, n)
        outs = [unique([node_at[t] for t in adjacent.get(k, []) if t in node_at and t != k]) for k in node_keys]
        link_score = {}
        if node_keys:
            N, D = len(node_keys), 0.85
            pr = [1.0 / N] * N
            for _ in range(20):
                nxt = [(1 - D) / N] * N
                dangling = 0.0
                for n in range(N):
                    if not outs[n]:
                        dangling += pr[n]
                        continue
                    share = D * pr[n] / len(outs[n])
                    for t in outs[n]:
                        nxt[t] += share
                for n in range(N):
                    nxt[n] += D * dangling / N
                pr = nxt
            mx, mn = max(pr), min(pr)
            for n, k in enumerate(node_keys):
                link_score[k] = 100 if mx == mn else round(100 * math.log(pr[n] / mn) / math.log(mx / mn))
        self.graph = {"adjacent": adjacent, "depth": depth, "start_row": start_row, "link_score": link_score}

        sitemap_keys = site.get("sitemap_keys") or set()
        orphans, deep, max_depth = [], [], 0
        img_checked = sum(1 for k in self.res if k.startswith("img|"))
        heavy_seen = {}
        for i, r in enumerate(rows):
            page = self.is_page(r)
            fk = norm(r.get("Final URL") or r["URL"])
            hops = r.get("Redirect Hops") or 0
            frm = inbound_from.get(fk, [])
            r["Linked From (pages)"] = ((("%d, " % len(frm)) + ", ".join(frm[:50]) + (" … +%d more" % (len(frm) - 50) if len(frm) > 50 else "")) if frm else 0) if page and not hops else ""
            r["Link Score (0-100)"] = link_score.get(fk, "") if page and not hops else ""
            if not page or hops:
                r["Click Depth"] = ""
            elif not orphan_ok:
                r["Click Depth"] = "N/A (needs a Domain audit with 'Follow links')"
            elif start_row == -1:
                r["Click Depth"] = "N/A (%s not audited)" % ("start URL" if scope else "homepage")
            elif fk not in depth:
                r["Click Depth"] = "Not reachable by links from the " + ("start URL" if scope else "homepage")
            else:
                r["Click Depth"] = depth[fk]
                max_depth = max(max_depth, depth[fk])
                if depth[fk] >= DEEP_CLICKS and r.get("Indexable") == "Yes":
                    self.issue(i, "Deep page (4+ clicks)")
                    deep.append("%s (%d)" % (r.get("Final URL") or r["URL"], depth[fk]))
            # Unique broken URLs: pages that redirect here share this page's links, so counting every occurrence
            # would count one footer link many times.
            r["Broken Links (on page)"] = len(broken_urls.get(i, [])) if page else ""
            r["_broken_urls"] = broken_urls.get(i, [])
            if broken_on.get(i):
                self.issue(i, "Broken links on page")
            indexable = r.get("Indexable") == "Yes"
            if not indexable:
                r["Orphan Page"] = "N/A (not indexable)" if page else ""
            elif not orphan_ok:
                r["Orphan Page"] = "N/A (needs a Domain audit with 'Follow links')"
            else:
                u = r.get("Final URL") or r["URL"]
                n = len(frm)
                home = (parse_url(u) or {}).get("path") == "/"
                start = depth.get(fk) == 0
                if n or home or start:
                    r["Orphan Page"] = ("No (homepage)" if home else "No (start URL)") if not n else "No (%d link%s from other pages)" % (n, "" if n == 1 else "s")
                else:
                    in_sm = fk in sitemap_keys
                    r["Orphan Page"] = ("Possible" if capped else "Yes") + " – no internal links" + (", found only in the sitemap" if in_sm else "") + \
                        (" (the crawl stopped at the page limit, so some links weren't seen)" if capped else "")
                    orphans.append(u + (" (in sitemap)" if in_sm else ""))
                    if not capped:
                        self.issue(i, "Orphan page")
            # Heavy images
            if not page:
                r["Heavy Images (200 KB+)"] = ""
            else:
                lst = self.ex[i]["images"]
                if not img_checked:
                    r["Heavy Images (200 KB+)"] = "Not checked" if lst else "No images"
                else:
                    heavy = sorted([(u, self.res.get("img|" + norm(u), {}).get("s", 0)) for u in lst], key=lambda x: -x[1])
                    heavy = [h for h in heavy if h[1] >= HEAVY_KB * 1024]
                    r["Heavy Images (200 KB+)"] = ("%d: " % len(heavy) + ", ".join("%s (%s)" % (u, fmt_bytes(s)) for u, s in heavy[:20]) +
                                                  (" … +%d more" % (len(heavy) - 20) if len(heavy) > 20 else "")) if heavy else "None"
                    if heavy:
                        self.issue(i, "Heavy images")
                    for u, s in heavy:
                        h = heavy_seen.setdefault(norm(u), {"u": u, "s": s, "pages": 0})
                        h["pages"] += 1

        # Site Health rows
        lst = lambda b, n=30: ", ".join("%s (%s%s)" % (x["url"], x["status"], ", on %d pages" % x["pages"] if x["pages"] > 1 else "")
                                        for x in sorted(uniq[b].values(), key=lambda x: -x["pages"])[:n]) + \
            (" … and %d more (see the Broken Links tab)" % (len(uniq[b]) - n) if len(uniq[b]) > n else "")  # noqa: E731
        cnt = lambda b: len(uniq[b])  # noqa: E731
        pages_with = lambda b: sum(x["pages"] for x in uniq[b].values())  # noqa: E731
        bi = cnt("broken|Internal")
        more = " · %d more internal link target/s not checked (limit %d)" % (self.skipped["int"], MAX_INTERNAL_CHECKS) if self.skipped.get("int") else ""
        self.add("Links", "Broken internal links", "Pass" if bi == 0 else "Warn" if bi < 10 else "Fail",
                 "None (%d internal links on %d pages checked%s)" % (internal_links, len(link_pages), more) if bi == 0
                 else "%d broken URL/s, linked %d time/s%s: %s" % (bi, pages_with("broken|Internal"), more, lst("broken|Internal")),
                 "High", "Fix or remove these links, or 301-redirect the missing pages to the closest working page. Every link is listed in the Broken Links tab with the page it's on.")
        if not self.skipped.get("ext_off"):
            be = cnt("broken|External")
            checked = sum(1 for k in self.res if k.startswith("ext|"))
            self.add("Links", "Broken external links", "Warn" if be else "Pass",
                     "%d of %d external URL/s checked are broken: %s" % (be, checked, lst("broken|External")) if be else "None (%d external URL/s checked)" % checked,
                     "Medium", "Update or remove links to pages that no longer exist on other websites.")
        rd = cnt("redirect|Internal")
        self.add("Links", "Internal links to redirects", "Warn" if rd else "Pass", "%d linked URL/s redirect: %s" % (rd, lst("redirect|Internal")) if rd else "None",
                 "Low", "Change these links to point straight to the final URL.")
        ni, ca = cnt("noindex|Internal"), cnt("canonicalised|Internal")
        if ni or ca:
            parts = ([("%d noindex page/s are linked: %s" % (ni, lst("noindex|Internal")))] if ni else []) + \
                    ([("%d canonicalised page/s are linked: %s" % (ca, lst("canonicalised|Internal")))] if ca else [])
            self.add("Links", "Internal links to noindex or canonicalised pages", "Warn", " · ".join(parts), "Low",
                     "Point internal links to the indexable, canonical version of each page (every link is in the Broken Links tab).")
        else:
            self.add("Links", "Internal links to noindex or canonicalised pages", "Pass", "None")
        uv = cnt("unverified|Internal") + cnt("unverified|External")
        if uv:
            self.add("Links", "Links not verified", "Info", "%d URL/s refused or didn't answer the check (often sites that block bots): %s" % (
                uv, ", ".join(filter(None, [lst("unverified|Internal", 15), lst("unverified|External", 15)]))))
        if orphan_ok:
            self.add("Links", "Click depth", "Pass" if not deep else "Info",
                     ("Deepest page: %d click/s from the %s" % (max_depth, "start URL" if scope else "homepage")) +
                     (" · %d indexable page/s are %d+ clicks deep: %s" % (len(deep), DEEP_CLICKS, listing(deep)) if deep else " · no indexable page is %d+ clicks deep" % DEEP_CLICKS))
            self.add("Links", "Orphan pages", ("Info" if capped else "Warn") if orphans else "Pass",
                     (("Possible orphans (the crawl stopped at the page limit, so some links weren't seen). " if capped else "") +
                      "%d indexable page/s that no other page links to: %s" % (len(orphans), listing(orphans, 50))) if orphans
                     else "None: every indexable page is linked from at least one other page", "Medium",
                     "Link to these pages from related pages, categories or the menu. If a page isn't needed any more, redirect or noindex it and remove it from the sitemap.")
        heavy_list = sorted(heavy_seen.values(), key=lambda x: -x["s"])
        skipped_img = " · %d more image/s not measured (limit %d)" % (self.skipped["img"], MAX_IMAGE_CHECKS) if self.skipped.get("img") else ""
        self.add("Performance", "Heavy images", "Info" if not img_checked else "Warn" if heavy_list else "Pass",
                 ("No images found to measure" + skipped_img) if not img_checked else
                 ("%d of %d image/s measured are %d KB or more%s: %s" % (len(heavy_list), img_checked, HEAVY_KB, skipped_img,
                  ", ".join("%s (%s%s)" % (x["u"], fmt_bytes(x["s"]), ", on %d pages" % x["pages"] if x["pages"] > 1 else "") for x in heavy_list[:30]))
                  if heavy_list else "None: all %d image/s measured are under %d KB%s" % (img_checked, HEAVY_KB, skipped_img)),
                 "Medium", "Compress these images and serve them as WebP/AVIF at the size they are shown (most photos fit in 100–200 KB). Big images slow down LCP.")

    # ---------------------------------------------------------------- canonical targets, hreflang, near-duplicates, archives

    def outside_targets(self, urls):
        out, seen, todo = {}, set(), []
        for u in urls:
            k = norm(u)
            if re.match(r"^https?://", u, re.I) and k not in seen:
                seen.add(k)
                todo.append(u)
        todo = todo[:OUTSIDE_TARGET_MAX]

        def one(u):
            res, err = self.f.get(u, user_agent=BROWSER_UA if self.c.browser else None)
            if err:
                return u, {"unverified": "didn't load"}
            code = res.status
            if code in (401, 403, 429, 999):
                return u, {"unverified": "answered %d" % code}
            if 300 <= code < 400:
                loc = res.header("location")
                return u, {"problem": ("redirect", "redirects to " + ((loc and resolve_url(loc, u)) or loc or "another URL")), "pairs": []}
            if code != 200:
                return u, {"problem": ("broken", "is broken (%d)" % code), "pairs": []}
            if is_bot_check_page(res.text):
                return u, {"unverified": "sent a bot-check page"}
            html = remove_blocks(re.sub(r"<!--[\s\S]*?-->", "", res.text), ["script", "style", "noscript", "template", "svg"])
            links = [parse_attributes(t) for t in get_tags(html, "link")]
            metas = [parse_attributes(t) for t in get_tags(html, "meta")]
            noindex = any(re.match(r"^(robots|googlebot)$", a.get("name") or "", re.I) and re.search(r"\b(noindex|none)\b", a.get("content") or "", re.I) for a in metas) or \
                bool(re.search(r"\b(noindex|none)\b", res.header("x-robots-tag"), re.I))
            canon = next((a for a in links if rel_has(a, "canonical") and a.get("href")), None)
            cu = (resolve_url(canon["href"].strip(), u) or canon["href"].strip()) if canon else ""
            return u, {"pairs": [(a["hreflang"].strip(), resolve_url(a["href"], u) or a["href"]) for a in links if rel_has(a, "alternate") and a.get("hreflang") and a.get("href")],
                       "problem": ("noindex", "is noindex") if noindex else ("chain", "points its canonical to " + cu) if cu and not same_url(cu, u) else None}

        with ThreadPoolExecutor(max_workers=10) as pool:
            for u, r in pool.map(one, todo):
                out[norm(u)] = r
        return out

    def cross_page(self):
        rows, ex = self.rows, self.ex
        is_page = self.is_page
        hops = lambda r: r.get("Redirect Hops") or 0  # noqa: E731
        final_of = lambda r: r.get("Final URL") or r["URL"]  # noqa: E731
        at = {}
        for i, r in enumerate(rows):
            at.setdefault(norm(r["URL"]), i)
        for i, r in enumerate(rows):
            at.setdefault(norm(final_of(r)), i)

        def add(check, status, result, prio="", fix=""):
            self.add("Content & links", check, status, result, prio, fix)

        def target_problem(tr):
            code = tr.get("Final Status")
            idx = str(tr.get("Indexable", ""))
            if code == "Error" or (isinstance(code, int) and code >= 400):
                return ("broken", "is broken (%s)" % code)
            if hops(tr):
                return ("redirect", "redirects to " + final_of(tr))
            if "noindex" in idx:
                return ("noindex", "is noindex")
            if "canonicalised elsewhere" in idx:
                return ("chain", "points its canonical to " + (ex[rows.index(tr)].get("canonical") or "another URL"))
            return None

        outside_urls = []
        for i, r in enumerate(rows):
            if not is_page(r) or hops(r):
                continue
            c = ex[i].get("canonical")
            if c and not same_url(c, final_of(r)) and norm(c) not in at:
                outside_urls.append(c)
            outside_urls += [p[1] for p in ex[i]["hreflang"] if norm(p[1]) not in at]
        self.report({"phase": "site", "step": "Checking canonical and hreflang targets"})
        outside = self.outside_targets(outside_urls)
        note = lambda o: (("couldn't verify: target " + o["unverified"]) if o.get("unverified") else "") if o else "not checked (over the %d-URL limit)" % OUTSIDE_TARGET_MAX  # noqa: E731

        # Canonical targets
        canon_issue = {"broken": "Canonical points to a broken page", "redirect": "Canonical points to a redirect", "noindex": "Canonical points to a noindex page", "chain": "Canonical chain"}
        bad, fine, unknown = [], [], []
        for i, r in enumerate(rows):
            if not is_page(r):
                r["Canonical Target Status"] = ""
                continue
            if hops(r):
                r["Canonical Target Status"] = "N/A (URL redirects)"
                continue
            c = ex[i].get("canonical")
            if not c:
                r["Canonical Target Status"] = "Missing"
                continue
            if same_url(c, final_of(r)):
                r["Canonical Target Status"] = "Self (OK)"
                continue
            t = at.get(norm(c))
            o = outside.get(norm(c)) if t is None else None
            where = " (outside this audit)" if t is None else ""
            if t is None and (not o or o.get("unverified")):
                r["Canonical Target Status"] = "Not verified: " + note(o)
                unknown.append(final_of(r) + " → " + c)
                continue
            p = o["problem"] if t is None else target_problem(rows[t])
            if not p:
                r["Canonical Target Status"] = "OK (target is indexable%s)" % where
                fine.append(final_of(r) + " → " + c + where)
                continue
            r["Canonical Target Status"] = "⚠ Target " + p[1] + where
            self.issue(i, canon_issue[p[0]])
            bad.append("%s → %s (%s)" % (final_of(r), c, p[1]))
        parts = []
        if bad:
            parts.append("%d page/s point their canonical to a broken, redirecting, noindex or canonicalised page: %s" % (len(bad), listing(bad)))
        if fine:
            parts.append("%d page/s point their canonical to another URL that works and is indexable: %s" % (len(fine), listing(fine)))
        if unknown:
            parts.append("%d canonical target/s couldn't be verified: %s" % (len(unknown), listing(unknown)))
        add("Canonical targets", "Warn" if bad else "Info" if unknown else "Pass",
            " · ".join(parts) if parts else "Every audited page has a self-referencing canonical (or none): no canonical points to another URL",
            "High", "Point every canonical straight to a working, indexable page that has a self-referencing canonical.")

        # Hreflang
        lang_re = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)
        hl_bad, hl_none, hl_pages, out_checked_all, out_unknown_all = [], [], 0, 0, 0
        for i, r in enumerate(rows):
            pairs = ex[i]["hreflang"]
            if not is_page(r):
                r["Hreflang Issues"] = ""
                continue
            if hops(r):
                r["Hreflang Issues"] = "N/A (URL redirects)"
                continue
            if not pairs:
                r["Hreflang Issues"] = "No hreflang"
                hl_none.append(i)
                continue
            hl_pages += 1
            me = norm(final_of(r))
            probs = []
            codes = [p[0] for p in pairs]
            invalid = unique([c for c in codes if not lang_re.match(c)])
            if invalid:
                probs.append("invalid code: " + ", ".join(c + (" (use %s)" % c.replace("_", "-") if "_" in c else "") for c in invalid))
            uk = unique([c for c in codes if c.lower().endswith("-uk")])
            if uk:
                probs.append(", ".join(uk) + " (the United Kingdom is -gb)")
            by_code = {}
            for c, u in pairs:
                by_code.setdefault(c.lower(), set()).add(norm(u))
            twice = [c for c, s in by_code.items() if len(s) > 1]
            if twice:
                probs.append("same code for different URLs: " + ", ".join(twice))
            if not any(norm(u) == me for _, u in pairs):
                probs.append("no self-reference")
            oc = ou = 0
            seen = {me}
            for code, u in pairs:
                tk = norm(u)
                if tk in seen:
                    continue
                seen.add(tk)
                t = at.get(tk)
                if t is None:
                    o = outside.get(tk)
                    if not o or o.get("unverified"):
                        ou += 1
                        continue
                    oc += 1
                    badp, back = o.get("problem"), o.get("pairs", [])
                else:
                    badp, back = target_problem(rows[t]), ex[t]["hreflang"]
                if badp:
                    probs.append("%s target %s" % (code, badp[1]))
                elif not any(norm(q[1]) == me for q in back):
                    probs.append("no return link from %s (%s)" % (code, u))
            out_checked_all += oc
            out_unknown_all += ou
            nt = (" · %d target/s outside this audit checked" % oc if oc else "") + (" · %d target/s couldn't be verified" % ou if ou else "")
            r["Hreflang Issues"] = ("⚠ " + "; ".join(probs[:8]) + (" … +%d more" % (len(probs) - 8) if len(probs) > 8 else "") + nt) if probs else "OK" + nt
            if probs:
                self.issue(i, "Hreflang errors")
                hl_bad.append(final_of(r))
        if hl_pages:
            for i in hl_none:
                rows[i]["Hreflang Issues"] = "⚠ No hreflang (other audited pages have it)"
                self.issue(i, "Missing hreflang (other pages have it)")
            parts = ["%d of %d page/s have hreflang: %s" % (hl_pages, hl_pages + len(hl_none),
                     ("%d with errors (codes, self-reference, return links or targets): %s" % (len(hl_bad), listing(hl_bad))) if hl_bad else
                     "all consistent" + (" (including %d target link/s outside this audit)" % out_checked_all if out_checked_all else ""))]
            if hl_none:
                parts.append("%d page/s have no hreflang while the others do: %s" % (len(hl_none), listing([final_of(rows[i]) for i in hl_none])))
            if out_unknown_all:
                parts.append("%d target link/s couldn't be verified" % out_unknown_all)
            add("Hreflang", "Warn" if hl_bad or hl_none else "Pass", " · ".join(parts), "Medium",
                "Use valid codes (en, en-GB, x-default), list the page itself, make every alternate page link back with the same set, and add hreflang to pages that have a version in another language.")
        else:
            add("Hreflang", "Info", "No hreflang tags on the audited pages (only needed for sites in several languages or countries)")

        # Thin archives
        arch = [r for r in rows if is_page(r) and re.search(r"archive|Search results", str(r.get("Archive Page", "")))]
        thin = [r for r in arch if "⚠" in str(r.get("Archive Page", ""))]
        if arch:
            add("Thin tag & archive pages", "Warn" if thin else "Pass",
                "%d of %d archive page/s are indexable but list only 1–2 posts (or are search results): %s" % (
                    len(thin), len(arch), listing([final_of(r) + " (" + re.sub(r" ⚠.*$", "", r["Archive Page"]) + ")" for r in thin])) if thin
                else "All %d archive page/s (tags, categories, authors, dates) list enough posts" % len(arch),
                "Medium", "Noindex tag, author and date archives that list only 1–2 posts (or merge those tags), keep search results out of the index, and give category pages a short unique intro.")

        # Near-duplicates (MinHash, 8 bands of 4 values)
        sig = [ex[i]["minhash"] if is_page(r) and not hops(r) and r.get("Indexable") == "Yes" and ex[i]["minhash"] else None for i, r in enumerate(rows)]
        buckets = {}
        for i, s in enumerate(sig):
            if not s:
                continue
            for b in range(8):
                buckets.setdefault((b, tuple(s[b * 4:b * 4 + 4])), []).append(i)
        near, compared = {}, set()
        for lst in buckets.values():
            lst = lst[:400]
            for a in range(len(lst)):
                for b in range(a + 1, len(lst)):
                    i, j = lst[a], lst[b]
                    if (i, j) in compared:
                        continue
                    compared.add((i, j))
                    hi, hj = rows[i].get("_content_hash"), rows[j].get("_content_hash")
                    if hi and hi == hj:
                        continue
                    sim = sum(1 for x in range(MINHASH_SIZE) if sig[i][x] == sig[j][x]) / MINHASH_SIZE
                    if sim < NEAR_DUP:
                        continue
                    near.setdefault(i, []).append((j, sim))
                    near.setdefault(j, []).append((i, sim))
        near_list = []
        for i, r in enumerate(rows):
            if i in near:
                m = sorted(near[i], key=lambda x: -x[1])
                r["Near Duplicate"] = "Yes – %d similar page%s: " % (len(m), "" if len(m) == 1 else "s") + \
                    ", ".join("%s (~%d%%)" % (final_of(rows[j]), min(99, round(s * 100))) for j, s in m[:10]) + (" … +%d more" % (len(m) - 10) if len(m) > 10 else "")
                self.issue(i, "Near-duplicate content")
                near_list.append(final_of(r))
            else:
                r["Near Duplicate"] = "No" if sig[i] else ("N/A (URL redirects)" if hops(r) else "N/A (not indexable or under 100 words)") if is_page(r) else ""
        add("Near-duplicate pages", "Warn" if near_list else "Pass",
            "%d indexable page/s share 80%%+ of their text with another page: %s" % (len(near_list), listing(near_list)) if near_list
            else "None found among %d indexable page/s with 100+ words of own content" % sum(1 for s in sig if s),
            "Medium", "Merge near-identical pages, point them to one canonical, or rewrite each one for its own search intent.")

    # ---------------------------------------------------------------- duplicates and scores

    def scores(self):
        rows = self.rows
        ok = lambda r: r.get("Final Status") == 200 and not (r.get("Redirect Hops") or 0)  # noqa: E731
        key = lambda v: str(v or "").strip().lower()  # noqa: E731

        def counter(col):
            m = {}
            for r in rows:
                k = key(r.get(col))
                if not ok(r) or not k or k == "missing" or k.startswith("n/a"):
                    continue
                # A copy that points its canonical to another page (?page=1, filters) is not a separate page.
                if "canonicalised elsewhere" in str(r.get("Indexable", "")):
                    continue
                m[k] = m.get(k, 0) + 1
            return m

        counts = {c: counter(c) for c in ("Meta Title", "Meta Description", "H1 Text", "_content_hash")}
        for i, r in enumerate(rows):
            issues = [x for x in list(r.get("_base_issues", [])) + self.page_issues.get(i, []) if x not in REMOVED_ISSUES]

            def dup(col, name):
                if not ok(r):
                    return "N/A (redirects)" if r.get("Redirect Hops") else ""
                if "canonicalised elsewhere" in str(r.get("Indexable", "")):
                    return "No (canonicalised to another page)"
                k = key(r.get(col))
                if k and counts[col].get(k, 0) > 1:
                    issues.append(name)
                    return "Yes (%d pages)" % counts[col][k]
                return "No"

            r["Duplicate Title"] = dup("Meta Title", "Duplicate meta title")
            r["Duplicate Description"] = dup("Meta Description", "Duplicate meta description")
            r["Duplicate H1"] = "No" if r.get("H1 Text") == "(empty)" else dup("H1 Text", "Duplicate H1")
            r["Duplicate Content"] = dup("_content_hash", "Duplicate content")
            lst = sorted(unique(issues), key=lambda n: PRIORITY_RANK[priority(n)])
            c = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
            for n in lst:
                c[priority(n)] += 1
            broken = "Page fetch failed" in lst or "Page not returning 200" in lst
            penalty = sum(c[k] * PRIORITY_WEIGHT[k] for k in c)
            r.update(c)
            r["Page Score"] = "N/A" if r.get("Meta Title") == "N/A (non-HTML)" else 0 if broken else max(0, 100 - penalty)
            r["Issues"] = "; ".join(lst)
