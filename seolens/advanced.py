"""Advanced checks that need the whole crawl, run after the cross-page checks and before scoring:

    URL parameters · canonical clusters · main-content thresholds by page type · indexability conflicts · JavaScript
    rendering summary · pagination · URL structure · internal anchor distribution

Canonical tags, sitemaps, pagination markup, word counts, URL length and anchor repetition are signals, not commands: findings
say "review" where the crawl can't know the intent, and nothing here claims what Google indexed or chose as canonical.
Query-string values that may be private (sessions, tokens, emails) are never written to the report.
"""
import re
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, quote

from .analyze import _NONWORD, GENERIC_ANCHORS, PAGE_NUM_RE
from .htmltools import bare_host, is_internal, norm, parse_url, same_url
from .net import robots_allows

LIST_MAX = 2000          # records per table in the report
DEEP_PATH = 5            # folders in a URL path before it is called deep
MANY_VARIANTS = 10       # parameter variants of one path before it is called a crawl trap risk

PARAM_KINDS = [
    ("private", r"sid|sessionid|session_id|phpsessid|jsessionid|aspsessionid\w*|cfid|cftoken|token|access_token|auth|key|apikey|api_key|"
                r"password|pass|pwd|email|e-mail|mail|user|username|signature|sig|code|otp"),
    ("tracking", r"utm_\w+|gclid|gbraid|wbraid|fbclid|msclkid|dclid|yclid|igshid|mc_cid|mc_eid|_ga|_gl|ref|ref_src|referrer|"
                 r"campaign|cmpid|trk|spm|affiliate|aff|partner"),
    ("pagination", r"page|paged|pg|p|offset|start|limit|per_page|perpage"),
    ("sorting", r"sort|sortby|sort_by|order|orderby|dir|direction"),
    ("filter", r"filter\w*|color|colour|size|brand|price|min_price|max_price|category|cat|tag|type|attr\w*|pa_\w+|facet\w*|f"),
    ("search", r"s|q|query|search|keyword|keywords|k"),
    ("language", r"lang|language|hl|locale|currency|country"),
]
PARAM_KINDS = [(k, re.compile(r"^(%s)$" % rx, re.I)) for k, rx in PARAM_KINDS]

# Pages whose length says little about quality: not judged against the word threshold.
NOT_JUDGED = [("Contact", r"contact(-us|-me)?|reach-us|get-in-touch|locations?|find-us|directions"),
              ("Gallery", r"(photo-|image-|video-)?(gallery|galleries)|photos|portfolio|videos|media"),
              ("Legal", r"privacy(-policy|-notice)?|terms(-of-(service|use)|-and-conditions|-conditions)?|cookies?(-policy)?|"
                        r"disclaimer|refund(-policy)?|returns?(-policy)?|shipping(-policy)?|legal|gdpr|imprint|impressum"),
              ("Utility", r"login|log-in|sign-?in|sign-?up|register|cart|basket|checkout|my-account|account|wishlist|thank-?you|"
                          r"thanks|search|404|sitemap")]
HALF_THRESHOLD = [("About", r"about(-us|-me)?|(our-|the-)?team|our-story|who-we-are")]


def param_kind(name):
    for kind, rx in PARAM_KINDS:
        if rx.match(name):
            return kind
    return "other"


def query_params(url):
    p = parse_url(url) or {}
    q = (p.get("query") or "").lstrip("?")
    return parse_qsl(q, keep_blank_values=True) if q else []


def safe_url(url):
    """The URL with private-looking query values replaced, so session IDs, tokens and emails never reach the report."""
    pairs = query_params(url)
    if not pairs or not any(param_kind(k) == "private" for k, _ in pairs):
        return url
    base = url.split("?", 1)[0]
    return base + "?" + "&".join("%s=%s" % (quote(k, safe="[]_-."), "[hidden]" if param_kind(k) == "private" else quote(v, safe="-_.,"))
                                 for k, v in pairs)


def describe_params(url):
    pairs = query_params(url)
    return ", ".join(dict.fromkeys("%s (%s)" % (k, param_kind(k)) for k, _ in pairs))


def base_of(url):
    return norm(url.split("?", 1)[0])


def similarity(a, b):
    if not a or not b or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


class Advanced:
    def __init__(self, crawler, post, site):
        self.c, self.post, self.site = crawler, post, site
        self.rows, self.ex = crawler.rows, crawler.extras
        self.s = crawler.s
        self.sitemap_keys = site.get("sitemap_keys") or set()
        self.sm = crawler.sitemap or site.get("sitemap")
        self.has_sitemap = bool(self.sm or self.sitemap_keys)
        self.at = {}
        for i, r in enumerate(self.rows):
            self.at.setdefault(norm(r["URL"]), i)
        for i, r in enumerate(self.rows):
            self.at.setdefault(norm(self.final(r)), i)
        self.out = {"params": [], "clusters": [], "anchors": [], "render_links": [], "counts": {}}

    @staticmethod
    def final(r):
        return r.get("Final URL") or r["URL"]

    def is_page(self, r):
        return self.post.is_page(r) and not (r.get("Redirect Hops") or 0)

    def in_sitemap(self, r):
        return norm(self.final(r)) in self.sitemap_keys or norm(r["URL"]) in self.sitemap_keys

    def run(self):
        for step in (self.url_structure, self.main_content, self.parameters, self.canonical_clusters, self.pagination,
                     self.indexability_conflicts, self.rendering, self.anchor_distribution):
            step()
        return self.out

    # ---------------------------------------------------------------- 8 URL structure

    def url_structure(self):
        url_max = int(self.s.get("url_max") or 115)
        lower = defaultdict(list)
        slash = Counter()
        for i, r in enumerate(self.rows):
            p = parse_url(r["URL"])
            if not p:
                continue
            segs = [x for x in p["path"].split("/") if x]
            r["URL Depth"] = len(segs)
            r["URL Length"] = len(r["URL"])
            n_params = len(query_params(r["URL"]))
            r["URL Parameters"] = describe_params(r["URL"]) if n_params else "None"
            notes = []
            if len(segs) > DEEP_PATH:
                notes.append("deep path (%d folders)" % len(segs))
            reps = [s for s, n in Counter(x.lower() for x in segs).items() if n > 1]
            if reps:
                notes.append("repeated segment: " + ", ".join(reps[:3]))
            if re.search(r"%[0-9a-f]{2}", p["path"], re.I) and not re.search(r"%20", p["path"]):
                notes.append("encoded characters")
            if notes:
                old = str(r.get("URL Issues") or "None")
                r["URL Issues"] = ", ".join(([] if old == "None" else [old]) + notes)
                if old == "None":
                    self.post.issue(i, "URL format issues")
            r["URL Structure"] = "Path %d · URL %d characters%s · depth %d · %d parameter%s · %s" % (
                len(p["path"]), len(r["URL"]), " (over %d)" % url_max if len(r["URL"]) > url_max else "", len(segs), n_params,
                "" if n_params == 1 else "s", "ends with /" if p["path"].endswith("/") else "no trailing slash")
            if self.is_page(r) and p["path"] != "/":
                slash["/" if p["path"].endswith("/") else "none"] += 1
                lower[norm(r["URL"]).lower()].append(r["URL"])
        mixed = [v for v in lower.values() if len(v) > 1]
        total = sum(slash.values())
        parts = []
        if mixed:
            parts.append("%d URL/s exist in different letter cases: %s" % (len(mixed), "; ".join(" = ".join(v[:2]) for v in mixed[:10])))
        if total >= 10 and 0.1 < slash["/"] / total < 0.9:
            parts.append("trailing slash is mixed: %d of %d page URLs end with / and %d don't" % (slash["/"], total, slash["none"]))
        self.post.add("Crawling", "URL structure", "Warn" if mixed else "Info" if parts else "Pass",
                      " · ".join(parts) if parts else "Consistent: no case duplicates, one trailing-slash style",
                      "Low", "Link to one version of each URL and 301-redirect the other. Review only: changing established URLs "
                             "for looks rarely pays off.")

    # ---------------------------------------------------------------- 3 main content by page type

    def content_type(self, i, r):
        from .report_data import page_type
        t = page_type(r)
        if t == "Homepage":
            return "Homepage", 0.5
        if t not in ("Unclassified", "Not an HTML page"):
            return t, None  # archives have their own check (thin archive pages)
        if self.ex[i].get("shop"):
            return "Product or shop page", 0.5
        path = ((parse_url(self.final(r)) or {}).get("path") or "").lower()
        last = [x for x in path.split("/") if x]
        for label, rx in NOT_JUDGED:
            if any(re.fullmatch(rx, seg) for seg in last[-2:]):
                return label + " page (from the URL)", None
        for label, rx in HALF_THRESHOLD:
            if any(re.fullmatch(rx, seg) for seg in last[-2:]):
                return label + " page (from the URL)", 0.5
        return "Content page", 1.0

    def main_content(self):
        base = int(self.s.get("content_min") or 300)
        for i, r in enumerate(self.rows):
            if not self.is_page(r) or not isinstance(r.get("Word Count"), int):
                continue
            label, factor = self.content_type(i, r)
            r["Content Type"] = label
            issues = r.get("_base_issues", [])
            if "Thin content" in issues:
                issues.remove("Thin content")
            words = r["Word Count"]
            if factor is None:
                r["Content Flag"] = "Not judged (%s)" % label.split(" (")[0]
                continue
            limit = max(100, int(base * factor))
            if words < limit:
                r["Content Flag"] = "Below review threshold: %d main-content words (threshold %d for this page type)" % (words, limit)
                issues.append("Thin content")
            else:
                r["Content Flag"] = "OK (%d ≥ %d)" % (words, limit)

    # ---------------------------------------------------------------- 1 URL parameters

    def parameters(self):
        found = {}  # norm url -> record

        def rec(u):
            k = norm(u)
            if k not in found:
                found[k] = {"url": u, "linked_from": set(), "sitemap": False, "row": self.at.get(k)}
            return found[k]

        root = bare_host(self.c.host or "")
        for i, e in enumerate(self.ex):
            for u, *_ in e.get("links", []):
                if "?" not in u:
                    continue
                p = parse_url(u)
                if p and p.get("query") and is_internal(bare_host(p["host"]), root or bare_host(p["host"])):
                    rec(u)["linked_from"].add(i)
        for u in (self.sm or {}).get("urls", []):
            if "?" in u and (parse_url(u) or {}).get("query"):
                rec(u)["sitemap"] = True
        for r in self.rows:
            if (parse_url(r["URL"]) or {}).get("query"):
                rec(r["URL"])
        variants = Counter(base_of(f["url"]) for f in found.values())
        crawled_query = self.s.get("include_query") or self.s.get("mode") == "list"
        tracking_sources = set()
        records = []
        for k, f in found.items():
            u, pairs = f["url"], query_params(f["url"])
            kinds = {param_kind(n) for n, _ in pairs}
            row = self.rows[f["row"]] if f["row"] is not None else None
            findings = []
            if "tracking" in kinds and f["linked_from"]:
                findings.append("Internal link uses a tracking parameter: links should go to the clean URL")
                tracking_sources |= f["linked_from"]
            if "private" in kinds:
                findings.append("Session or private value in the URL (value hidden here)")
            if f["sitemap"]:
                findings.append("Parameter URL in the XML sitemap: list the clean URL unless this variant is its own page")
            if variants[base_of(u)] >= MANY_VARIANTS:
                findings.append("%d parameter variants of the same path were found: check sorting/filter links can't multiply" % variants[base_of(u)])
            canon, idx = "", ""
            if row is not None:
                i = f["row"]
                canon = self.ex[i].get("canonical") or ""
                idx = str(row.get("Indexable", ""))
                clean = self.at.get(base_of(u))
                if canon and not same_url(canon, self.final(row)):
                    findings.append("Canonical points to %s" % ("the clean URL (expected consolidation)" if norm(canon) == base_of(u) else canon))
                elif clean is not None and idx == "Yes" and self.is_page(row):
                    sim = similarity(self.ex[i].get("minhash"), self.ex[clean].get("minhash"))
                    if sim is not None and sim >= 0.9:
                        findings.append("Same content as %s (%.0f%% similar) and indexable with a self canonical" % (self.rows[clean]["URL"], sim * 100))
                        self.post.issue(i, "Parameter URL duplicates another page")
                status = "Crawled"
            else:
                status = "Not crawled (parameter URLs are skipped: turn on 'Crawl URLs with ?parameters' to check their content)" \
                    if not crawled_query else "Not crawled (over the URL limit or not reached)"
            if not findings:
                findings.append("No problem seen: %s parameters can be legitimate" % "/".join(sorted(kinds)))
            records.append([safe_url(u), safe_url(u.split("?", 1)[0]), describe_params(u), len(pairs),
                            ("Internal links on %d page%s" % (len(f["linked_from"]), "" if len(f["linked_from"]) == 1 else "s") if f["linked_from"] else "") +
                            (" · " if f["linked_from"] and f["sitemap"] else "") + ("XML sitemap" if f["sitemap"] else "") or "Crawled URL",
                            status, safe_url(canon) if canon else ("" if row is None else "Missing"), idx or "Not measured",
                            "; ".join(findings)])
        for i in tracking_sources:
            self.post.issue(i, "Tracking parameters in internal links")
        records.sort(key=lambda x: (x[8].startswith("No problem"), x[1], x[0]))
        self.out["params"] = records[:LIST_MAX]
        self.out["counts"]["param_urls"] = len(found)
        flagged = sum(1 for x in records if not x[8].startswith("No problem"))
        if found:
            self.post.add("Crawling", "URL parameters", "Warn" if tracking_sources else "Info",
                          "%d parameter URL/s found (%d linked internally, %d in the sitemap); %d need a review%s" % (
                              len(found), sum(1 for f in found.values() if f["linked_from"]), sum(1 for f in found.values() if f["sitemap"]),
                              flagged, "; tracking parameters are used in internal links on %d page/s" % len(tracking_sources) if tracking_sources else ""),
                          "Medium", "Link internally to clean URLs (no utm_ or click IDs), keep parameter variants out of the sitemap and "
                                    "give sorting/filter variants a canonical to the main version. Details: Technical SEO sheet.")
        else:
            self.post.add("Crawling", "URL parameters", "Pass", "No URLs with ?parameters were found in internal links, the sitemap or the crawl")

    # ---------------------------------------------------------------- 2 canonical clusters

    def canonical_clusters(self):
        clusters = defaultdict(list)
        for i, r in enumerate(self.rows):
            if not self.is_page(r):
                continue
            declared = self.ex[i].get("canonical") or ""
            if not declared:
                continue
            t = self.at.get(norm(declared))
            resolved = self.final(self.rows[t]) if t is not None else declared
            clusters[norm(resolved)].append((i, declared, resolved, t))
        rows_out, conflicts = [], 0
        n = 0
        for key, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            others = [m for m in members if not same_url(m[1], self.final(self.rows[m[0]]))]
            if not others:
                continue  # a page that only points to itself is not a cluster worth listing
            n += 1
            cid = "C%03d" % n
            for i, declared, resolved, t in members:
                r = self.rows[i]
                self_c = same_url(declared, self.final(r))
                sim = None if self_c or t is None else similarity(self.ex[i].get("minhash"), self.ex[t].get("minhash"))
                multi = len({x.strip() for x in str(r.get("Canonical URL", "")).split(",") if x.strip()}) > 1
                cstat = str(r.get("Canonical Target Status", ""))
                if multi:
                    kind = "Conflicting canonical declarations (%s)" % r.get("Canonical URL")
                elif self_c:
                    kind = "Self-canonical (cluster target)"
                elif cstat.startswith("⚠"):
                    kind = "Canonical target problem: " + cstat.lstrip("⚠ ").replace("Target ", "target ")
                elif t is None:
                    kind = "Target outside this audit: " + (cstat or "not checked")
                elif base_of(self.final(r)) == base_of(resolved) or norm(self.final(r)).rstrip("/").lower() == norm(resolved).rstrip("/").lower():
                    kind = "Expected consolidation (URL variant)"
                elif sim is not None and sim < 0.5:
                    kind = "Possible mismatch: different content (%.0f%% similar)" % (sim * 100)
                    self.post.issue(i, "Canonical to a different page")
                elif sim is None:
                    kind = "Points to another page (similarity not measured: under 100 words)"
                else:
                    kind = "Points to a similar page (%.0f%% similar)" % (sim * 100)
                conflict = kind.startswith(("Conflicting", "Canonical target problem", "Possible mismatch"))
                conflicts += conflict
                r["Canonical Cluster"] = "%s · %d URL%s → %s" % (cid, len(members), "" if len(members) == 1 else "s", resolved)
                rows_out.append([cid, r["URL"], declared, resolved, len(members), r.get("Final Status", ""), str(r.get("Indexable", "")),
                                 "Not measured" if sim is None and not self_c else "Same page" if self_c else "%.0f%%" % (sim * 100), kind])
        self.out["clusters"] = rows_out[:LIST_MAX]
        self.out["counts"]["canonical_clusters"] = n
        self.out["counts"]["canonical_conflicts"] = conflicts
        self.post.add("Content & links", "Canonical clusters", "Warn" if conflicts else "Info" if n else "Pass",
                      ("%d cluster/s of pages declaring the same canonical; %d declaration/s need a review (see the canonical cluster "
                       "table on the Technical SEO sheet)" % (n, conflicts)) if n else "Every page with a canonical points to itself",
                      "Medium", "A canonical is a hint, not a command: point each one to the page with the same content, make that page "
                                "200, indexable and self-canonical, and use one canonical tag per page.")

    # ---------------------------------------------------------------- 7 pagination

    def pagination(self):
        series = defaultdict(list)
        for i, r in enumerate(self.rows):
            pg = self.ex[i].get("pagination") or {}
            if pg.get("page") and self.is_page(r):
                series[norm(PAGE_NUM_RE.sub("", self.final(r))).rstrip("/")].append(i)
        problems = 0
        for key, members in series.items():
            next_seen = Counter(norm(self.ex[i]["pagination"].get("next") or "") for i in members if self.ex[i]["pagination"].get("next"))
            for i in members:
                r, pg = self.rows[i], self.ex[i]["pagination"]
                notes = []
                nxt = pg.get("next") or ""
                if nxt:
                    t = self.at.get(norm(nxt))
                    m = PAGE_NUM_RE.search(nxt)
                    n_next = int(m.group(1) or m.group(2)) if m else None
                    if same_url(nxt, self.final(r)) or (n_next is not None and n_next <= pg["page"]):
                        notes.append("⚠ loop: the next link goes back to page %s" % (n_next or pg["page"]))
                    if t is not None and (self.rows[t].get("Final Status") != 200):
                        notes.append("⚠ next page link is broken (%s)" % self.rows[t].get("Final Status"))
                    if next_seen[norm(nxt)] > 1:
                        notes.append("⚠ %d pages share this next link" % next_seen[norm(nxt)])
                canon = self.ex[i].get("canonical") or ""
                if pg["page"] >= 2 and canon and not same_url(canon, self.final(r)):
                    first = [j for j in members if self.ex[j]["pagination"]["page"] == 1]
                    is_first = norm(canon).rstrip("/") == key or any(same_url(canon, self.final(self.rows[j])) for j in first)
                    sim = similarity(self.ex[i].get("minhash"), self.ex[first[0]].get("minhash")) if first else None
                    if is_first and (sim is None or sim < 0.8):
                        notes.append("⚠ canonical points to page 1 although this page lists different items")
                        self.post.issue(i, "Paginated page canonicalised to page 1")
                if notes:
                    problems += 1
                    r["Pagination"] = str(r.get("Pagination", "")) + " · " + " · ".join(notes)
        self.out["counts"]["pagination_series"] = len(series)
        self.post.add("Crawling", "Pagination", "Warn" if problems else "Info" if series else "Pass",
                      ("%d paginated series, %d page/s with a pagination problem (see the Pagination column on the Technical SEO "
                       "sheet)" % (len(series), problems)) if series else "No paginated series found",
                      "Medium", "Give every page of a series a crawlable <a href> link to the next page and a self-referencing "
                                "canonical. rel=next/prev is optional markup: Google no longer uses it.")

    # ---------------------------------------------------------------- 4 indexability conflicts

    def indexability_conflicts(self):
        rows, n_pages = self.rows, 0
        for i, r in enumerate(rows):
            if not self.is_page(r):
                continue
            ev = []
            idx = str(r.get("Indexable", ""))
            robots = str(r.get("Meta Robots", ""))
            in_sm = self.has_sitemap and self.in_sitemap(r)
            noindex = "noindex" in idx
            if noindex and in_sm:
                ev.append("Noindex URL in the XML sitemap (%s; listed in the sitemap)" % re.sub(r"^No\s*[–-]\s*", "", idx))
            if "canonicalised elsewhere" in idx and in_sm:
                ev.append("Canonicalised URL in the XML sitemap (canonical: %s)" % (self.ex[i].get("canonical") or "another URL"))
            if noindex and "canonicalised elsewhere" in idx:
                ev.append("Noindex and a canonical to another URL at the same time (canonical: %s)" % self.ex[i].get("canonical"))
                self.post.issue(i, "Noindex page with a canonical to another URL")
            cstat = str(r.get("Canonical Target Status", ""))
            if cstat.startswith("⚠") and not noindex:
                ev.append("Indexable page whose canonical %s (canonical: %s)" % (cstat.lstrip("⚠ ").replace("Target ", "target "),
                                                                              self.ex[i].get("canonical")))
            canons = {x.strip() for x in str(r.get("Canonical URL", "")).split(",") if x.strip()}
            if len(canons) > 1:
                ev.append("Multiple canonical declarations: " + ", ".join(sorted(canons)))
            if "noindex (header)" in idx and re.search(r"\bindex\b", robots, re.I) and not re.search(r"noindex", robots, re.I):
                ev.append("X-Robots-Tag header says noindex but the meta robots tag says index")
            raw = {d[0]: d for d in self.ex[i].get("render_diff") or []}
            if "Robots" in raw and raw["Robots"][1] != "Unchanged":
                a, b = str(raw["Robots"][2]), str(raw["Robots"][3])
                if "noindex" in a.lower() and "noindex" not in b.lower():
                    ev.append("Raw HTML says noindex, JavaScript removes it (raw: %s · rendered: %s)" % (a, b or "none"))
                elif "noindex" in b.lower() and "noindex" not in a.lower():
                    ev.append("JavaScript adds noindex (raw: %s · rendered: %s)" % (a or "none", b))
            if raw.get("Canonical") and raw["Canonical"][1] != "Unchanged":
                ev.append("JavaScript changes the canonical (raw: %s · rendered: %s)" % (raw["Canonical"][2] or "none", raw["Canonical"][3] or "none"))
            if ev:
                n_pages += 1
                r["Indexability Conflicts"] = "; ".join(ev)
        # Sitemap URLs that robots.txt blocks (never crawled, so they have no row)
        blocked = []
        group = getattr(self.c, "robots_group", None)
        if group and self.sm:
            for u in self.sm.get("urls", []):
                p = parse_url(u)
                if p and not robots_allows(group, p["path"] + (p.get("query") or "")):
                    blocked.append(u)
        self.out["counts"]["indexability_conflicts"] = n_pages + len(blocked)
        self.out["sitemap_blocked"] = blocked[:LIST_MAX]
        if self.sm and group is not None:
            self.post.add("Crawling", "Sitemap URLs blocked by robots.txt", "Warn" if blocked else "Pass",
                          ("%d sitemap URL/s are disallowed in robots.txt: %s" % (len(blocked), ", ".join(safe_url(u) for u in blocked[:30])))
                          if blocked else "None", "Medium",
                          "Decide per URL: remove it from the sitemap if it shouldn't be crawled, or allow it in robots.txt if it should.")
        self.post.add("Crawling", "Indexability conflicts", "Warn" if n_pages else "Pass",
                      ("%d page/s send conflicting indexing signals (see the Indexability Conflicts column on the Technical SEO sheet)" % n_pages)
                      if n_pages else "No page sends conflicting indexing signals", "High",
                      "Make the signals agree: indexable pages are 200, self-canonical and in the sitemap; excluded pages are noindex "
                      "and left out of the sitemap. These are signals to investigate, not proof of what Google does.")

    # ---------------------------------------------------------------- 5 + 6 rendering comparison

    def rendering(self):
        on = any(r.get("Rendering Check") for r in self.rows)
        counts = Counter()
        for i, r in enumerate(self.rows):
            st = str(r.get("Rendering Check", ""))
            if not st:
                continue
            counts["Measured" if st.startswith("Measured") else "Failed" if st.startswith("Failed") else
                   "Not" if st.startswith("Not measured") else "n/a"] += 1
            if st.startswith("Measured") and not str(r.get("Rendering Differences", "")).startswith("No differences"):
                counts["diff"] += 1
            rl = self.ex[i].get("render_links") or {}
            for kind, label in (("added", "Rendered-only link"), ("removed", "Removed by JavaScript")):
                for u, anchor in rl.get(kind, []):
                    self.out["render_links"].append([label, r["URL"], u, anchor])
            for anchor, old, new in rl.get("changed", []):
                self.out["render_links"].append(["Destination changed by JavaScript", r["URL"], new, "%s (raw HTML: %s)" % (anchor, old)])
        self.out["render_links"] = self.out["render_links"][:LIST_MAX]
        self.out["counts"].update(rendered=counts["Measured"], render_failed=counts["Failed"], render_skipped=counts["Not"],
                                  render_diff=counts["diff"] if on else None)
        if on:
            self.post.add("Content & links", "JavaScript rendering comparison", "Info",
                          "%d page/s rendered (%d with differences from the raw HTML), %d not selected or over the limit, %d failed" % (
                              counts["Measured"], counts["diff"], counts["Not"], counts["Failed"]))

    # ---------------------------------------------------------------- 9 anchor distribution

    def anchor_distribution(self):
        dest = {}
        for i, e in enumerate(self.ex):
            if not self.post.is_page(self.rows[i]):
                continue
            for u, anchor, where, times in e.get("anchors", []):
                d = dest.setdefault(norm(u), {"url": u, "c": Counter(), "m": Counter(), "src": set()})
                d[where if where in ("c", "m") else "m"][anchor] += times
                d["src"].add(i)
        if not dest:
            return
        top_src = sorted((len(d["src"]) for d in dest.values()), reverse=True)
        important = top_src[max(0, len(top_src) // 10 - 1)] if len(top_src) >= 10 else 10 ** 9

        def generic(a):
            return _NONWORD.sub("", a.lower()).strip() in GENERIC_ANCHORS

        def empty(a):
            return a in ("", "[image: no alt]")

        out, flagged = [], 0
        for k, d in dest.items():
            c = d["c"]
            total_c, total_m = sum(c.values()), sum(d["m"].values())
            if not total_c:
                continue
            g = sum(n for a, n in c.items() if generic(a))
            e = sum(n for a, n in c.items() if empty(a))
            desc = [(a, n) for a, n in c.most_common() if not generic(a) and not empty(a)]
            findings = []
            if g >= 3 or g / total_c >= 0.5:  # one "read more" next to a descriptive link is normal
                findings.append("%d generic anchor%s (%s)" % (g, "" if g == 1 else "s", ", ".join(sorted({a for a in c if generic(a)})[:4])))
            if e:
                findings.append("%d link%s without anchor text or alt" % (e, "" if e == 1 else "s"))
            t = self.at.get(k)
            names = [str(self.rows[t].get(col, "")).lower() for col in ("Meta Title", "H1 Text")] if t is not None else []
            # The page's own title or H1 as anchor (related-post widgets, cards) is natural template repetition.
            # Only judged when the target was crawled (its title is known) and the repetition is heavy.
            title_link = not names or any(desc[0][0].lower().rstrip(" .…") in x for x in names if x) if desc else True
            if desc and not title_link and total_c >= 10 and desc[0][1] / total_c >= 0.9 and len(desc[0][0].split()) >= 2:
                findings.append("the same exact anchor in %d of %d contextual links (review: repetition can be natural)" % (desc[0][1], total_c))
            if len(desc) >= 10:
                findings.append("%d different anchors: check they describe the same topic" % len(desc))
            if len(d["src"]) >= important and (g + e) / total_c >= 0.5:
                findings.append("a much-linked page that gets mostly non-descriptive anchors")
            if not findings:
                continue
            flagged += 1
            top = ", ".join('"%s" (%d)' % (a or "empty", n) for a, n in c.most_common(5))
            out.append([d["url"], len(d["src"]), total_c, total_m, len(c), top, "; ".join(findings)])
        out.sort(key=lambda x: -x[2])
        self.out["anchors"] = out[:LIST_MAX]
        self.out["counts"]["anchor_findings"] = flagged
        self.post.add("Content & links", "Internal anchor text", "Warn" if flagged else "Pass",
                      ("%d linked page/s get generic, empty or very repetitive contextual anchors (menus and footers are counted "
                       "apart; see the Internal Links sheet)" % flagged) if flagged else "Contextual anchors are descriptive and varied",
                      "Low", "In body text, link with words that describe the target page; give icon and image links an alt or aria-label.")


def run_advanced(crawler, post, site):
    return Advanced(crawler, post, site).run()
