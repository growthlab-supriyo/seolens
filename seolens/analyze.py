"""Page analysis: everything the Page Wise Audit tab shows for one URL (a port of the Sheets tool's analyzeUrl)."""
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import regex

from .htmltools import (ASSET_RE, bare_host, class_tokens, clean_spaces, clean_text, count_words, decode_entities, get_elements,
                        get_tags, hash_text, is_bot_check_page, is_internal, is_product_grid, is_utility_link, main_content_html, norm,
                        parse_attributes, parse_url, rel_has, remove_blocks, remove_elements_where, resolve_url, same_url, section_of,
                        strip_non_content, unique)

_RULES = json.load(open(os.path.join(os.path.dirname(__file__), "data", "rules.json"), encoding="utf-8"))
ISSUES = _RULES["ISSUES"]
SCHEMA_RULES = _RULES["SCHEMA_RULES"]
LOCAL_BUSINESS_TYPES = re.compile(_RULES["LOCAL_BUSINESS_TYPES"]["source"])
TECH_RULES = [(g, n, re.compile(t["source"], re.I), re.compile(h["source"], re.I) if h else None) for g, n, t, h in _RULES["TECH_RULES"]]

TITLE_MIN, TITLE_MAX, DESC_MIN, DESC_MAX = 30, 60, 70, 160
THIN_CONTENT_WORDS = 300
THIN_ARCHIVE_POSTS = 2

GENERIC_ANCHORS = {"click here", "here", "read more", "learn more", "more", "this", "link", "this link", "click", "go", "details",
                   "more info", "continue", "continue reading", "see more", "view more", "find out more"}
PLACEHOLDERS = [(r"lorem ipsum", "lorem ipsum"), (r"just another wordpress site", "just another wordpress site"), (r"\bsample page\b", "sample page"),
                (r"\bhello world!", "hello world!"), (r"\bunder construction\b", "under construction"), (r"\btest page\b", "test page"),
                (r"your (company|business) name", "your company name"), (r"\[insert ", "[insert ")]
TITLE_STOPWORDS = set(("a an and are as at be by for from how in into is it its of on or our the this to vs was what when where which "
                       "who why with you your best top free new").split())
ARCHIVE_TYPES = [
    ("Search results", r" search-results | search ", r"[?&]s=|/search/(?!label/)"),
    ("Tag archive", r" tag | tag-\S+ ", r"/(tags?|label)/[^/]+"),
    ("Author archive", r" author | author-\S+ ", r"/author/[^/]+"),
    ("Date archive", r" date ", r"/\d{4}/(\d{2}/)?(\d{2}/)?$"),
    ("Category archive", r" category | category-\S+ ", r"/(category|categories|topics?)/[^/]+"),
    ("Archive", r" archive | post-type-archive ", None),
]


# ---------------------------------------------------------------- small checks

def url_hygiene(url):
    p = parse_url(url)
    if not p:
        return []
    out = []
    if re.search(r"[A-Z]", p["path"]):
        out.append("uppercase letters")
    if "_" in p["path"]:
        out.append("underscores (use hyphens)")
    if re.search(r"\s|%20", p["path"]):
        out.append("spaces")
    if re.search(r"//+", p["path"]):
        out.append("double slash")
    if re.search(r"(^|/)index\.(html?|php|aspx?)$", p["path"], re.I):
        out.append("ends in an index file")
    if re.search(r"[?&;](sid|sessionid|phpsessid|jsessionid|session_id)=", p["query"] + ";" + p["path"], re.I):
        out.append("session ID in the URL")
    params = len([x for x in p["query"][1:].split("&") if x]) if p["query"] else 0
    if params > 3:
        out.append("%d parameters" % params)
    if len(url) > 115:
        out.append("long URL (%d characters)" % len(url))
    return out


_NONWORD = regex.compile(r"[^\p{L}\p{N}\s]+")


def title_vs_h1(title, h1):
    if not title or not h1:
        return "N/A (%s)" % ("no title" if not title else "no H1")
    main = re.split(r"\s+[|·•–—-]\s+", title)[0]
    if clean_spaces(main).lower() == clean_spaces(h1).lower():
        return "Same"

    def words(s):
        return {w for w in _NONWORD.sub(" ", s.lower()).split() if w and w not in TITLE_STOPWORDS}

    a, b = words(main), words(h1)
    if not a or not b:
        return "N/A (too short to compare)"
    shared = len(a & b)
    share = shared / min(len(a), len(b))
    if share >= 0.6:
        return "Similar (%d%% of main words shared)" % round(share * 100)
    if share >= 0.25:
        return "Partly similar (%d%% of main words shared)" % round(share * 100)
    return "Different ⚠ (%d main word%s shared)" % (shared, "" if shared == 1 else "s")


# MinHash of 3-word phrases (32 values): pages sharing ~80% of values share ~80% of their phrases.
MINHASH_SIZE = 32
M32 = 0xFFFFFFFF


def _imul(a, b):
    return (a * b) & M32


def _hash32(s):
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch) & 0xFFFF
        h = _imul(h, 0x01000193)
    return h


def _mix32(x):
    x &= M32
    x ^= x >> 16
    x = _imul(x, 0x7FEB352D)
    x ^= x >> 15
    x = _imul(x, 0x846CA68B)
    x ^= x >> 16
    return x


MINHASH_SEEDS = [_mix32(i + 0x9E3779B9) for i in range(MINHASH_SIZE)]
_SPLIT = regex.compile(r"[^\p{L}\p{N}]+")


def min_hash(text):
    w = [x for x in _SPLIT.split(text.lower()) if x]
    if len(w) < 3:
        return None
    mins = [M32] * MINHASH_SIZE
    for i in range(len(w) - 2):
        h = _hash32(w[i] + " " + w[i + 1] + " " + w[i + 2])
        for k in range(MINHASH_SIZE):
            v = _mix32(h ^ MINHASH_SEEDS[k])
            if v < mins[k]:
                mins[k] = v
    return mins


def detect_archive(body_class, page):
    cls = " " + re.sub(r"\s+", "  ", body_class.lower()) + " "
    url = page["path"] + page["query"]
    pm = re.search(r"/page/(\d+)/?|[?&]paged?=(\d+)", url, re.I)
    num = int(pm.group(1) or pm.group(2)) if pm else (2 if " paged " in cls else 0)
    test_url = re.sub(r"/page/\d+/?$", "/", url, flags=re.I)
    for name, cls_re, url_re in ARCHIVE_TYPES:
        if re.search(cls_re, cls) or (url_re and re.search(url_re, test_url, re.I)):
            return {"type": name, "page": num if num > 1 else 0}
    return None


def is_shop_page(og_type, nodes, body_class, path):
    if re.match(r"^product", og_type or "", re.I):
        return True
    if any(any(t in ("Product", "ProductGroup") for t in types_of(n)) and (n.get("offers") or n.get("hasVariant")) for n in nodes):
        return True
    if re.search(r"(^|\s)(single-product|template-product|template-collection|tax-product_cat|tax-product_tag|post-type-archive-product|woocommerce-shop)(\s|$)", body_class, re.I):
        return True
    return bool(re.search(r"/(products?|collections|product-category|shop)(/|$)", path, re.I))


def detect_platform(html, generator, headers):
    header_text = "\n".join(k + ": " + str(v) for k, v in (headers or {}).items())
    groups = {}

    def put(g, v):
        groups.setdefault(g, [])
        if v not in groups[g]:
            groups[g].append(v)

    for g, n, t, h in TECH_RULES:
        if t.search(html) or (h and h.search(header_text)):
            put(g, n)
    if generator:
        put("Generator", generator)
    powered = (headers or {}).get("x-powered-by", "")
    lang = re.search(r"PHP/?[\d.]*|ASP\.NET|Express|Next\.js|Phusion Passenger", powered, re.I)
    lang = lang.group(0) if lang else ("PHP" if re.search(r"PHPSESSID|laravel_session|XSRF-TOKEN=eyJpdiI", header_text, re.I) else "")
    if lang:
        put("Language", re.sub(r"^php", "PHP", lang, flags=re.I))
    server = (headers or {}).get("server", "")
    if server:
        put("Server", server[:40])
    for name, rx in [("Cloudflare", r"cf-ray|cloudflare"), ("Vercel", r"x-vercel"), ("Netlify", r"x-nf-request-id|netlify"),
                     ("CloudFront", r"x-amz-cf-id|cloudfront"), ("Fastly", r"fastly|x-served-by"), ("Akamai", r"akamai"),
                     ("Hostinger", r"x-hcdn|hostinger"), ("LiteSpeed Cache", r"x-litespeed-cache"), ("Sucuri", r"x-sucuri"),
                     ("WP Engine", r"wpe-backend|x-wpe"), ("Kinsta", r"x-kinsta")]:
        if re.search(rx, header_text, re.I):
            put("CDN / Hosting", name)
    order = ["CMS", "E-commerce", "Page builder", "SEO plugin", "Framework", "Language", "Server", "CDN / Hosting", "JavaScript", "CSS", "Generator"]
    out = [g + ": " + ", ".join(groups[g]) for g in order if g in groups]
    if "CMS" not in groups and "Framework" not in groups and ("Language" in groups or "Server" in groups):
        out.insert(0, "CMS: none detected (custom-built site)")
    return " · ".join(out) if out else "Unknown"


def detect_tracking(html):
    lst = []
    ga4 = unique(re.findall(r"(?:gtag/js\?id=|[\"'])(G-[A-Z0-9]{6,12})(?=[\"'&])", html))
    gtm = unique(re.findall(r"GTM-[A-Z0-9]{4,9}\b", html))
    ua = unique(re.findall(r"\bUA-\d{4,10}-\d{1,4}\b", html))
    ads = unique(re.findall(r"\bAW-\d{6,12}\b", html))
    if ga4:
        lst.append("GA4 (%s)" % ", ".join(ga4))
    if gtm:
        lst.append("GTM (%s)" % ", ".join(gtm))
    if ua:
        lst.append("Legacy UA (%s)" % ", ".join(ua))
    if ads:
        lst.append("Google Ads (%s)" % ", ".join(ads))
    other = False
    for name, rx, analytics in [("Meta Pixel", r"connect\.facebook\.net/[^\"']*fbevents\.js|fbq\(\s*[\"']init", False),
                                ("Microsoft Clarity", r"clarity\.ms/tag", True), ("Hotjar", r"static\.hotjar\.com|hotjar\.com/c/hotjar", False),
                                ("LinkedIn Insight", r"snap\.licdn\.com", False), ("TikTok Pixel", r"analytics\.tiktok\.com", False),
                                ("HubSpot", r"js\.hs-scripts\.com|js\.hs-analytics\.net", True), ("Matomo", r"matomo\.js|piwik\.js", True),
                                ("Plausible", r"plausible\.io/js", True), ("Adobe Analytics", r"assets\.adobedtm\.com", True),
                                ("Segment", r"cdn\.segment\.com", True),
                                ("Shopify web pixels (contents not visible)", r"webPixelsConfigList", False)]:
        if re.search(rx, html, re.I):
            lst.append(name)
            other = other or analytics
    consent = [n for n, rx in [("Cookiebot", "cookiebot"), ("OneTrust", r"onetrust|cookielaw\.org"), ("CookieYes", "cookieyes"),
                                ("Termly", r"termly\.io"), ("iubenda", "iubenda"), ("Usercentrics", "usercentrics"), ("Complianz", "complianz"),
                                ("Osano", "osano"), ("Didomi", "didomi")] if re.search(rx, html, re.I)]
    if consent:
        lst.append("Consent: " + "/".join(consent))
    return {"list": lst, "has_analytics": bool(ga4 or gtm or other), "legacy_ua": bool(ua)}


# ---------------------------------------------------------------- schema

def extract_json_ld(html):
    nodes, errors = [], 0
    for s in get_elements(html, "script"):
        if "ld+json" not in (s["attrs"].get("type") or "").lower():
            continue
        txt = re.sub(r"\]\]>\s*$", "", re.sub(r"^\s*<!\[CDATA\[", "", s["inner"].strip())).strip()
        if not txt:
            continue
        try:
            _collect(json.loads(txt), nodes)
        except (ValueError, RecursionError):
            errors += 1
    return nodes, errors


def _collect(obj, nodes):
    if isinstance(obj, list):
        for o in obj:
            _collect(o, nodes)
    elif isinstance(obj, dict):
        if obj.get("@type"):
            nodes.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _collect(v, nodes)


def types_of(node):
    t = node.get("@type")
    return [re.sub(r"^https?://schema\.org/", "", str(x), flags=re.I) for x in (t if isinstance(t, list) else [t]) if x]


def has_value(v):
    return v is not None and v != "" and not (isinstance(v, list) and not v)


def breadcrumb_text(nodes, markup):
    bc = next((n for n in nodes if "BreadcrumbList" in types_of(n) and isinstance(n.get("itemListElement"), list)), None)
    if bc:
        items = sorted([i for i in bc["itemListElement"] if isinstance(i, dict)], key=lambda i: _num(i.get("position")))
        names = [i.get("name") or (i.get("item").get("name") if isinstance(i.get("item"), dict) else None) for i in items]
        names = [str(n) for n in names if n]
        if names:
            return " > ".join(names)
    block = (re.search(r"<nav[^>]*aria-label=[\"']breadcrumbs?[\"'][^>]*>([\s\S]*?)</nav>", markup, re.I)
             or re.search(r"<(?:ol|ul)[^>]*class=[\"'][^\"']*breadcrumb[^\"']*[\"'][^>]*>([\s\S]*?)</(?:ol|ul)>", markup, re.I)
             or re.search(r"<(?:nav|div)[^>]*class=[\"'][^\"']*breadcrumb[^\"']*[\"'][^>]*>([\s\S]*?)</(?:nav|div)>", markup, re.I))
    if block:
        items = [clean_text(e["inner"]) for e in get_elements(block.group(1), "li")] or [clean_text(e["inner"]) for e in get_elements(block.group(1), "a")]
        items = [t for t in items if t and not re.match(r"^[>/»›|]+$", t)]
        if items:
            return " > ".join(items) + " (HTML)"
    return "Missing"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- articles, authors, dates

def detect_article(meta, nodes, body_class, path, content_html, not_article):
    if not_article:
        return False
    if re.match(r"^article$", meta("og:type"), re.I):
        return True
    if any(any(re.match(r"^(Article|BlogPosting|NewsArticle|TechArticle|HowTo|Report|ScholarlyArticle)$", t) for t in types_of(n)) for n in nodes):
        return True
    cls = " " + body_class.lower() + " "
    if re.search(r" single-post | post-template-default ", cls):
        return True
    if re.search(r"<article\b[^>]*class=[\"'][^\"']*\btype-post\b", content_html, re.I):
        return True
    return bool(re.search(r"/(blogs?|news|guides?|articles?|insights|how-to|knowledge-base|resources)/", path + "/", re.I))


def find_author(meta, nodes, links, content_html):
    m = meta("author")
    if m:
        return m + " (meta)"
    art = next((n for n in nodes if n.get("author") and any(re.search(r"Article|Posting|WebPage", t) for t in types_of(n))), None)
    if art:
        names = []
        for a in (art["author"] if isinstance(art["author"], list) else [art["author"]]):
            if isinstance(a, str):
                names.append(a)
            elif isinstance(a, dict):
                if a.get("name"):
                    names.append(str(a["name"]))
                elif a.get("@id"):
                    ref = next((x for x in nodes if x.get("@id") == a["@id"] and x.get("name")), None)
                    if ref:
                        names.append(str(ref["name"]))
        if names:
            return ", ".join(names) + " (schema)"
    rel = next((a for a in get_elements(content_html, "a") if rel_has(a["attrs"], "author") and clean_text(a["inner"])), None)
    if rel:
        return clean_text(rel["inner"]) + " (author link)"
    by = re.search(r"<([a-z0-9]+)[^>]*class=[\"'][^\"']*\b(author|byline|post-author|entry-author|author-name)\b[^\"']*[\"'][^>]*>([\s\S]*?)</\1>", content_html, re.I)
    if by:
        text = re.sub(r"^(posted|written|published)?\s*by[:\s]*", "", clean_text(by.group(3)), flags=re.I).strip()
        if text and len(text) <= 60:
            return text + " (byline)"
    for a in get_elements(content_html, "a"):
        mm = re.search(r"/author/([^/?#]+)", a["attrs"].get("href") or "", re.I)
        if mm:
            return re.sub(r"[-_]+", " ", mm.group(1)).title() + " (author URL)"
    if any(rel_has(a, "author") for a in links):
        return "Unknown name (rel=author link in head)"
    return "Not found"


DAY = 86400.0


def parse_date(s):
    """Seconds since 1970 for ISO / RFC dates, or None."""
    if not s:
        return None
    t = str(s).strip()
    t = re.sub(r"^(\d{4}-\d{2}-\d{2}) (\d)", r"\1T\2", t)
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(str(s)).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def day_of(s):
    t = parse_date(s)
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t is not None else str(s)[:10]


def analyze_dates(meta, nodes, markup, content_html, last_modified, title):
    now = time.time()
    art = [n for n in nodes if any(re.search(r"Article|Posting|WebPage", t) for t in types_of(n))]

    def schema_date(key):
        n = next((x for x in art if has_value(x.get(key)) and not isinstance(x.get(key), (dict, list))), None) or \
            next((x for x in nodes if has_value(x.get(key)) and not isinstance(x.get(key), (dict, list))), None)
        return str(n[key]) if n else ""

    def itemprop(name):
        tag = re.search(r"<[^>]+itemprop=[\"']" + name + r"[\"'][^>]*>", markup, re.I)
        if not tag:
            return ""
        a = parse_attributes(tag.group(0))
        return a.get("content") or a.get("datetime") or ""

    times = [a for a in (parse_attributes(t) for t in get_tags(content_html, "time")) if a.get("datetime")]

    def time_by(rx):
        return next((a["datetime"] for a in times if re.search(rx, a.get("class") or "", re.I)), "")

    pub_src = [["meta", meta("article:published_time")], ["schema", schema_date("datePublished")],
               ["time tag", time_by(r"\b(published|entry-date|post-date|date-published)\b") or itemprop("datePublished")]]
    mod_src = [["meta", meta("article:modified_time")], ["og:updated", meta("og:updated_time")], ["schema", schema_date("dateModified")],
               ["time tag", time_by(r"\b(updated|modified|date-modified)\b") or itemprop("dateModified")]]
    if not any(s[1] for s in pub_src):
        first = next((a for a in times if not re.search(r"\b(updated|modified)\b", a.get("class") or "", re.I)), None)
        if first:
            pub_src.append(["first time tag", first["datetime"]])
    best = lambda lst: next((s[1] for s in lst if s[1]), "")  # noqa: E731
    pub, mod = best(pub_src), best(mod_src)
    issues, flags = [], []

    def spread(lst):
        ts = [parse_date(s[1]) for s in lst]
        ts = [x for x in ts if x is not None]
        return max(ts) - min(ts) if len(ts) > 1 else 0

    conflict = False
    if spread(pub_src) > DAY:
        conflict = True
        flags.append("published dates differ between sources")
    if spread(mod_src) > DAY:
        conflict = True
        flags.append("modified dates differ between sources")
    if conflict:
        issues.append("Conflicting dates")
    pt, mt = parse_date(pub), parse_date(mod)
    if pt is not None and mt is not None and mt < pt - DAY:
        issues.append("Modified before published")
        flags.append("modified date is before the publish date")
    if any((parse_date(s[1]) or 0) > now + DAY for s in pub_src + mod_src):
        issues.append("Future date")
        flags.append("date in the future")
    if any(parse_date(s[1]) is not None and abs(now - parse_date(s[1])) < 600 for s in mod_src):
        issues.append("Date changes on every load")
        flags.append("modified date equals the time of the crawl")
    years = [int(y) for y in re.findall(r"\b20\d{2}\b", title or "")]
    ty = max(years) if years else 0
    if ty and pt is not None and ty > datetime.fromtimestamp(pt, timezone.utc).year and (mt is None or datetime.fromtimestamp(mt, timezone.utc).year < ty):
        issues.append("Title year newer than content")
        flags.append("title says %d but the content date is older" % ty)
    structured = lambda lst: any(s[1] and s[0] not in ("time tag", "first time tag") for s in lst)  # noqa: E731
    if (pub and not structured(pub_src)) or (mod and not structured(mod_src)):
        issues.append("Dates missing from meta & schema")
        flags.append("Date only in visible HTML")
    show = lambda lst: " · ".join(s[0] + ": " + (day_of(s[1]) if s[1] else "missing") for s in lst)  # noqa: E731
    header = " · Last-Modified header: " + day_of(last_modified) if last_modified else ""
    if not pub and not mod:
        check = "No published/modified dates found" + (" |" + header[2:] if header else "")
    else:
        check = "Pub → " + show(pub_src) + " | Mod → " + show(mod_src) + header + "".join(" | ⚠ " + f for f in flags)
    display = ("Pub: " + (day_of(pub) if pub else "-") + " | Mod: " + (day_of(mod) if mod else "-") + (" ⚠ conflict" if conflict else "")) if (pub or mod) else "Not found"
    return display, check, issues


# ---------------------------------------------------------------- links

def extract_content_links(html, base_url, page_url, page_root):
    """Contextual links in the main content: split anchors merged, image links use alt text, utility links skipped."""
    out = {"in_urls": [], "in_anchors": [], "out_urls": [], "out_anchors": [], "split": 0}
    me = norm(page_url)
    text_of = lambda inner: decode_entities(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", " ", inner, flags=re.I)))  # noqa: E731
    links = []
    for m in re.finditer(r"<a(?=[\s>])([^>]*)>([\s\S]*?)</a\s*>", html, re.I):
        attrs = parse_attributes("<a" + m.group(1) + ">")
        href = (attrs.get("href") or "").strip()
        prev = links[-1] if links else None
        between = html[prev["end"] if prev else 0:m.start()]
        if prev and href and prev["href"] == href and not clean_text(between) and \
                not re.search(r"</?(li|div|p|br|h[1-6]|td|th|tr|ul|ol|section|article|figure|button)\b", between, re.I):
            prev["text"] += text_of(m.group(2))
            prev["inner"] += m.group(2)
            prev["end"] = m.end()
            prev["pieces"] += 1
            continue
        links.append({"href": href, "text": text_of(m.group(2)), "inner": m.group(2), "end": m.end(), "pieces": 1})
    seen = set()
    for l in links:
        if l["pieces"] > 1:
            out["split"] += 1
        href = l["href"]
        if not href or href.startswith("#") or re.match(r"^(javascript|mailto|tel|data):", href, re.I) or ASSET_RE.search(href):
            continue
        anchor = clean_spaces(l["text"])
        if not anchor:
            imgs = [parse_attributes(t) for t in get_tags(l["inner"], "img")]
            if not imgs:
                continue
            alt = (imgs[0].get("alt") or "").strip()
            anchor = "[image: %s]" % (clean_spaces(alt) if alt else "no alt")
        full = resolve_url(href, base_url)
        p = parse_url(full) if full else None
        if not p or is_utility_link(full):
            continue
        n = norm(full)
        if n == me or (n + "|" + anchor.lower()) in seen:
            continue
        seen.add(n + "|" + anchor.lower())
        if is_internal(bare_host(p["host"]), page_root):
            out["in_urls"].append(full)
            out["in_anchors"].append(anchor)
        else:
            out["out_urls"].append(full)
            out["out_anchors"].append(anchor)
    return out


# ---------------------------------------------------------------- one page

_LINK_RE = re.compile(r"<a\b[^>]*>[\s\S]*?</a\s*>", re.I)


def content_blocks(main_html):
    """Text of the paragraphs, list items, table cells and sub-headings in the main content (for internal link suggestions).
    Text that is already a link is replaced by " ¦ ", so it is never suggested as a new anchor."""
    out, seen, size = [], set(), 0
    for tag in ("p", "li", "td", "h2", "h3", "h4", "blockquote"):
        for e in get_elements(main_html, tag):
            if tag in ("li", "td") and re.search(r"<(p|li|ul|ol|table)\b", e["inner"], re.I):
                continue  # its own paragraphs / items are read separately
            text = clean_text(_LINK_RE.sub(" ¦ ", e["inner"]))
            if len(text) >= 25 and text not in seen:
                seen.add(text)
                out.append(text[:2000])
                size += min(len(text), 2000)
                if size > 15000:  # about 2,500 words: plenty to find anchors, small enough for big sites
                    return out
    if size < 300:
        # Page builders and custom sites often keep their text in <div>s / <span>s with <br>: split at block boundaries.
        flat = re.sub(r"</?(div|section|article|br|h[1-6]|ul|ol|li|tr|table|header|footer|aside|figure|figcaption)\b[^>]*>", "\n", _LINK_RE.sub(" ¦ ", main_html), flags=re.I)
        for line in flat.split("\n"):
            text = clean_text(line)
            if len(text) >= 25 and text not in seen:
                seen.add(text)
                out.append(text[:2000])
                size += min(len(text), 2000)
                if size > 15000:
                    break
    return out


def analyze_page(url, ch):
    """ch = Fetcher.chain() result. Returns (row dict, extras dict for the cross-page checks)."""
    d = {"URL": url}
    issues = []
    ex = {"links": [], "discovered": [], "images": [], "hreflang": [], "minhash": None, "source": ch["final"]}
    probs = url_hygiene(url)
    d["URL Issues"] = ", ".join(probs) if probs else "None"
    if probs:
        issues.append("URL format issues")
    chain = ch["chain"]
    redirects = len(chain) if ch["error"] else len(chain) - 1
    text = " → ".join("%s %s" % (c, u) for c, u in chain)
    if ch["error"]:
        text += (" → " if text else "") + "Error: " + ch["error"]
    d["Initial Status"] = chain[0][0] if chain else "Error"
    d["Redirect Hops"] = redirects
    d["Redirect Chain"] = text if text and (redirects or ch["error"]) else "None"
    d["Final URL"] = ch["final"]
    if redirects >= 2:
        issues.append("Redirect chain (2+ hops)")
    elif redirects == 1:
        issues.append("URL redirects")

    def verdict(reasons):
        if not redirects:
            return "No – " + ", ".join(reasons) if reasons else "Yes"
        return "No – redirects (%s) to Final URL; Final URL %s" % (chain[0][0], "not indexable: " + ", ".join(reasons) if reasons else "is indexable")

    def done():
        d["_base_issues"] = issues
        return d, ex

    if ch["error"]:
        d["Final Status"] = "Error"
        d["Indexable"] = "No – fetch failed"
        issues.append("Page fetch failed")
        return done()
    res = ch["response"]
    status = res.status
    ctype = res.header("content-type")
    x_robots = res.header("x-robots-tag")
    size_kb = round(len(res.content) / 1024, 1)
    d["Final Status"] = status
    d["HTML Size (KB)"] = size_kb
    if status != 200:
        d["Indexable"] = verdict(["status %s" % status])
        issues.append("Page not returning 200")
        return done()
    if ctype and not re.search(r"html|xml", ctype, re.I):
        typ = ctype.split(";")[0].strip()
        d["Indexable"] = verdict(["not an HTML page (%s)" % typ]) if redirects else "N/A – not an HTML page (%s)" % typ
        d["Meta Title"] = "N/A (non-HTML)"
        ex["non_html"] = True
        return done()

    page_url = ch["final"]
    page = parse_url(page_url) or {"protocol": "https", "host": "", "path": "/", "query": ""}
    page_root = bare_host(page["host"])
    if is_bot_check_page(res.text):
        d["Indexable"] = "Unknown – the server sent a bot-check page instead of the real page"
        d["Meta Title"] = "N/A (bot-check page)"
        issues.append("Bot-check page instead of content")
        return done()

    html = re.sub(r"<!--[\s\S]*?-->", "", res.text)
    markup = remove_blocks(html, ["script", "style", "noscript", "template", "svg"])
    head = section_of(markup, "head") or markup
    body = section_of(markup, "body") or markup
    raw_head = section_of(html, "head")
    content = remove_blocks(body, ["iframe", "select"])
    visible = clean_text(content)
    word_count = count_words(visible)
    own = strip_non_content(content)
    own_text = clean_text(own)
    lost = (not re.search(r"<h1\b", own, re.I)) if re.search(r"<h1\b", content, re.I) else True
    if count_words(own_text) < 50 and word_count >= 150 and lost:
        own, own_text = content, visible
    content_words = count_words(own_text)

    metas = [parse_attributes(t) for t in get_tags(markup, "meta")]

    def meta(name):
        n = name.lower()
        for a in metas:
            if a.get("content") is not None and any((a.get(k) or "").lower() == n for k in ("name", "property", "itemprop")):
                return clean_spaces(a["content"])
        return ""

    titles = [clean_text(e["inner"]) for e in get_elements(head, "title")]
    title = titles[0] if titles else ""
    d["Meta Title"] = title or "Missing"
    d["Title Length"] = len(title)
    if not title:
        issues.append("Missing meta title")
    elif not TITLE_MIN <= len(title) <= TITLE_MAX:
        issues.append("Title length out of range")
    if len(titles) > 1:
        issues.append("Multiple title tags")
    desc = meta("description")
    d["Meta Description"] = desc or "Missing"
    d["Description Length"] = len(desc)
    if not desc:
        issues.append("Missing meta description")
    elif not DESC_MIN <= len(desc) <= DESC_MAX:
        issues.append("Description length out of range")

    links = [parse_attributes(t) for t in get_tags(markup, "link")]
    base_tag = next((a for a in (parse_attributes(t) for t in get_tags(head, "base")) if a.get("href")), None)
    base_url = (resolve_url(base_tag["href"], page_url) or page_url) if base_tag else page_url

    canon_hrefs = unique([a["href"].strip() for a in links if rel_has(a, "canonical") and a.get("href")])
    hc = re.search(r"<([^>]+)>\s*;[^,]*rel=[\"']?canonical", res.header("link"), re.I)
    if not canon_hrefs and hc:
        canon_hrefs.append(hc.group(1))
    canon_url = ""
    if not canon_hrefs:
        issues.append("Missing canonical")
    else:
        canon_url = resolve_url(canon_hrefs[0], base_url) or canon_hrefs[0]
        if len(canon_hrefs) > 1:
            issues.append("Multiple canonical tags")
        if not same_url(canon_url, page_url):
            issues.append("Canonical points to another URL")
        if not re.match(r"^https?://", canon_hrefs[0], re.I):
            issues.append("Relative canonical URL")
    d["Canonical URL"] = ", ".join(canon_hrefs) if canon_hrefs else "Missing"
    ex["canonical"] = canon_url

    robots = [a for a in metas if re.match(r"^(robots|googlebot)$", a.get("name") or "", re.I) and a.get("content")]
    d["Meta Robots"] = " | ".join(a["name"].lower() + ": " + a["content"] for a in robots) if robots else "Not specified"
    reasons = []
    if any(re.search(r"\b(noindex|none)\b", a["content"], re.I) for a in robots):
        reasons.append("noindex (meta)")
    if re.search(r"\b(noindex|none)\b", x_robots, re.I):
        reasons.append("noindex (header)")
    if reasons:
        issues.append("Page is noindex")
    if canon_url and not same_url(canon_url, page_url):
        reasons.append("canonicalised elsewhere")
    d["Indexable"] = verdict(reasons)

    html_tag = re.search(r"<html\b[^>]*>", html, re.I)
    lang = parse_attributes(html_tag.group(0)).get("lang", "") if html_tag else ""
    d["HTML Lang"] = lang or "Missing"
    if not lang:
        issues.append("Missing html lang attribute")
    og_locale = meta("og:locale")
    og_alt = unique([clean_spaces(a["content"]) for a in metas if (a.get("property") or "").lower() == "og:locale:alternate" and a.get("content")])
    lang_of = lambda s: re.split(r"[-_]", s.lower())[0]  # noqa: E731
    clash = bool(og_locale and lang and lang_of(og_locale) != lang_of(lang))
    d["OG Locale"] = (og_locale or "Missing") + (" ⚠ differs from HTML lang (%s)" % lang if clash else " (matches HTML lang)" if og_locale and lang else "") + \
        (" · alternates: " + ", ".join(og_alt[:10]) if og_alt else "")
    if clash:
        issues.append("og:locale doesn't match HTML lang")

    hl_tags = [re.sub(r"\s*/?>$", " />", clean_spaces(t)) for t in get_tags(markup, "link") if rel_has(parse_attributes(t), "alternate") and parse_attributes(t).get("hreflang")]
    d["Hreflang Tags"] = ("%d tag%s: " % (len(hl_tags), "" if len(hl_tags) == 1 else "s") + "  ".join(hl_tags[:50]) +
                         ("  … +%d more" % (len(hl_tags) - 50) if len(hl_tags) > 50 else "")) if hl_tags else "None"
    ex["hreflang"] = [(a["hreflang"].strip(), resolve_url(a["href"], base_url) or a["href"]) for a in links
                      if rel_has(a, "alternate") and a.get("hreflang") and a.get("href")][:60]
    viewport = meta("viewport")
    d["Viewport"] = viewport or "Missing"
    if not viewport:
        issues.append("Missing viewport meta tag")
    og_missing = [p for p in ("og:title", "og:description", "og:image", "og:url") if not meta(p)]
    d["Open Graph"] = "Missing: " + ", ".join(og_missing) if og_missing else "All present"
    if og_missing:
        issues.append("Incomplete Open Graph tags")
    rel_meta = ["hreflang " + a["hreflang"] for a in links if rel_has(a, "alternate") and a.get("hreflang") and a.get("href") and not re.match(r"^https?://", a["href"].strip(), re.I)] + \
               [p for p in ("og:url", "og:image") if meta(p) and not re.match(r"^https?://", meta(p), re.I)]
    if rel_meta:
        d["Open Graph"] += " · ⚠ relative URL: " + ", ".join(unique(rel_meta)[:5])
        issues.append("Relative URL in hreflang or Open Graph")
    d["Twitter Card"] = meta("twitter:card") or "Missing"

    # Headings
    h1s = [clean_text(m.group(2)) for m in re.finditer(r"<h([1-6])\b[^>]*>([\s\S]*?)</h\1\s*>", content, re.I) if m.group(1) == "1"]
    d["H1 Count"] = len(h1s)
    d["H1 Text"] = " | ".join(h or "(empty)" for h in h1s) if h1s else "Missing"
    d["Title vs H1"] = title_vs_h1(title, h1s[0] if h1s else "")
    if d["Title vs H1"].startswith("Different"):
        issues.append("Title and H1 don't match")
    main_html = main_content_html(content)
    main_no_grid = remove_elements_where(main_html, is_product_grid)
    ex["blocks"] = content_blocks(main_no_grid)

    def heading_list(level):
        texts = [t for t in (clean_text(e["inner"]) for e in get_elements(main_no_grid, "h%d" % level)) if t]
        return ", ".join(texts) if texts else "None"

    d["H2 Tags (body)"] = heading_list(2)
    d["H3 Tags (body)"] = heading_list(3)
    if not any(h1s):
        issues.append("Missing H1")
    if len(h1s) > 1:
        issues.append("Multiple H1 tags")
    prev, skips = 0, []
    for m in re.finditer(r"<h([1-6])\b[^>]*>[\s\S]*?</h\1\s*>", own, re.I):
        lv = int(m.group(1))
        if prev and lv > prev + 1:
            skips.append("H%d→H%d" % (prev, lv))
        prev = lv
    d["Word Count"] = content_words
    if content_words < THIN_CONTENT_WORDS:
        issues.append("Thin content")

    # Images
    imgs = [parse_attributes(t) for t in get_tags(content, "img")]
    modern_re = re.compile(r"\.(webp|avif)(\?|$|\s|,)", re.I)
    modern = sum(len(get_tags(p["inner"], "img")) for p in get_elements(content, "picture")
                 if re.search(r"type=[\"']image/(webp|avif)", p["inner"], re.I) or modern_re.search(p["inner"]))
    modern += sum(1 for a in (parse_attributes(t) for t in get_tags(remove_blocks(content, ["picture"]), "img"))
                  if modern_re.search(" ".join(str(a.get(k) or "") for k in ("src", "srcset", "data-src", "data-srcset"))))

    def src_of(a):
        s = next((x for x in (a.get("src"), a.get("data-src"), a.get("data-lazy-src")) if x and not x.strip().lower().startswith("data:")), None)
        return (resolve_url(s, base_url) or s) if s else "(inline/embedded image)"

    def sized(a):
        if a.get("width") and a.get("height"):
            return True
        st = a.get("style") or ""
        if re.search(r"aspect-ratio", st, re.I) or (re.search(r"(^|;)\s*width\s*:", st, re.I) and re.search(r"(^|;)\s*height\s*:", st, re.I)):
            return True
        if a.get("data-nimg") == "fill":
            return True
        c = " " + (a.get("class") or "") + " "
        if re.search(r"\s(aspect-(square|video|\[[^\]]+\]|\d+/\d+)|ratio)\s", c):
            return True
        w = re.search(r"\s(w|size)-(full|screen|\d+(\.\d+)?|\[[^\]]+\]|px)\s", c)
        h = re.search(r"\s(h|size)-(full|screen|\d+(\.\d+)?|\[[^\]]+\]|px)\s", c)
        return bool(w and h)

    missing_alt = [a for a in (parse_attributes(t) for t in get_tags(main_html, "img")) if "alt" not in a]
    unsized = [a for a in imgs if not sized(a)]
    d["Images Missing Alt (URLs)"] = ", ".join(unique(src_of(a) for a in missing_alt)) if missing_alt else "None"
    d["Images Missing Width/Height (URLs)"] = ", ".join(unique(src_of(a) for a in unsized)) if unsized else "None"
    if missing_alt:
        issues.append("Images missing alt text")
    if unsized:
        issues.append("Images missing width/height")
    if len(imgs) >= 3 and not modern:
        issues.append("No WebP/AVIF images")

    def first_src(s):
        return str(s or "").strip().split(",")[0].strip().split(" ")[0]

    img_urls = []
    for a in imgs:
        s = next((x for x in (a.get("src"), a.get("data-src"), a.get("data-lazy-src"), first_src(a.get("srcset")), first_src(a.get("data-srcset")))
                  if x and not str(x).strip().lower().startswith("data:")), None)
        u = resolve_url(str(s).strip(), base_url) if s else None
        if u and re.match(r"^https?://", u, re.I) and not re.search(r"\.svg(\?|$)", u, re.I):
            img_urls.append(u)
    ex["images"] = unique(img_urls)[:40]

    # Links (whole page: menus and footer too)
    forms = {"absolute": 0, "root": 0, "relative": 0, "protocol": 0}
    root_list, rel_list = [], []
    page_nofollow = any(re.search(r"\b(nofollow|none)\b", a["content"], re.I) for a in robots)
    total = generic = blank = insecure = tel = mail = 0
    page_links = []
    for a in get_elements(content, "a"):
        href = (a["attrs"].get("href") or "").strip()
        if href.lower().startswith("tel:"):
            tel += 1
            continue
        if href.lower().startswith("mailto:"):
            mail += 1
            continue
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if re.match(r"^https?://", href, re.I):
            forms["absolute"] += 1
        elif href.startswith("//"):
            forms["protocol"] += 1
        elif href.startswith("/"):
            forms["root"] += 1
            root_list.append(href)
        elif not re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.I):
            forms["relative"] += 1
            rel_list.append(href)
        full = resolve_url(href, base_url)
        p = parse_url(full) if full else None
        if not p:
            continue
        total += 1
        internal = is_internal(bare_host(p["host"]), page_root)
        if not is_utility_link(full):
            anchor = clean_text(a["inner"])
            if not anchor:
                im = [parse_attributes(t) for t in get_tags(a["inner"], "img")]
                anchor = "[image: %s]" % (clean_spaces(im[0]["alt"]) if im and (im[0].get("alt") or "").strip() else "no alt") if im else ""
            page_links.append((re.sub(r"#.*$", "", full), anchor))
        if internal and page["protocol"] == "https" and p["protocol"] == "http":
            insecure += 1
        rel = (a["attrs"].get("rel") or "").lower()
        if not re.search(r"nofollow|sponsored|ugc", rel) and not page_nofollow and internal:
            ex["discovered"].append(full)
        if (a["attrs"].get("target") or "").lower() == "_blank" and not re.search(r"noopener|noreferrer", rel):
            blank += 1
        if _NONWORD.sub("", clean_text(a["inner"]).lower()).strip() in GENERIC_ANCHORS:
            generic += 1

    def listed(arr):
        u = unique(arr)
        return (": " + ", ".join(u[:50]) + (" … +%d more" % (len(u) - 50) if len(u) > 50 else "")) if u else ""

    d["Link URL Types"] = ("Absolute %d · Root-relative %d%s · Relative %d%s%s" % (
        forms["absolute"], forms["root"], listed(root_list), forms["relative"], listed(rel_list),
        " · Protocol-relative %d" % forms["protocol"] if forms["protocol"] else "")) if total else "No links"
    if forms["relative"]:
        issues.append("Document-relative links")
    if generic:
        issues.append("Generic anchor text")
    if insecure:
        issues.append("Internal links using http://")

    cl = extract_content_links(main_no_grid, base_url, page_url, page_root)
    d["Internal Links on Page (content)"] = ", ".join(cl["in_urls"])
    d["Internal Link Anchors (content)"] = ", ".join(cl["in_anchors"])
    d["External Links on Page (content)"] = ", ".join(cl["out_urls"])
    d["External Link Anchors (content)"] = ", ".join(cl["out_anchors"])
    in_content = {norm(u) for u in cl["in_urls"] + cl["out_urls"]}
    me = norm(page_url)
    seen = {}
    for u, anchor in page_links:
        k = norm(u)
        if k == me:
            continue
        where = "c" if k in in_content else "m"
        if k in seen:
            if where == "c":
                seen[k][2] = "c"
            continue
        seen[k] = [u, anchor[:60], where]
    ex["links"] = list(seen.values())

    # Speed signals
    def is_exec(a):
        t = (a.get("type") or "").lower()
        return not t or bool(re.search(r"javascript|ecmascript|module", t))

    scripts = [s for s in get_elements(html, "script") if is_exec(s["attrs"])]
    blocking = [s for s in get_elements(raw_head, "script") if is_exec(s["attrs"]) and s["attrs"].get("src") and "async" not in s["attrs"]
                and "defer" not in s["attrs"] and (s["attrs"].get("type") or "").lower() != "module"]

    def short_name(src):
        full = resolve_url(src, base_url)
        p = parse_url(full) if full else None
        if not p:
            return src
        f = [x for x in p["path"].split("/") if x]
        return (f[-1] if f else p["path"]) + " (" + bare_host(p["host"]) + ")"

    inline_bytes = sum(len(s["inner"]) for s in scripts if not s["attrs"].get("src")) + sum(len(s["inner"]) for s in get_elements(html, "style"))
    hints_all = ("preconnect", "dns-prefetch", "prefetch", "preload", "modulepreload")
    dom_hints, dom_order = {}, []

    def note(u, hint=None):
        full = resolve_url(u, base_url)
        p = parse_url(full) if full else None
        h = bare_host(p["host"]) if p else ""
        if not h or is_internal(h, page_root):
            return
        if h not in dom_hints:
            dom_hints[h] = []
            dom_order.append(h)
        if hint and hint not in dom_hints[h]:
            dom_hints[h].append(hint)

    for s in scripts:
        if s["attrs"].get("src"):
            note(s["attrs"]["src"])
    for a in links:
        if not a.get("href"):
            continue
        rels = (a.get("rel") or "").lower().split()
        hs = [x for x in hints_all if x in rels]
        if hs:
            for x in hs:
                note(a["href"], x)
        elif "stylesheet" in rels or any("icon" in r for r in rels):
            note(a["href"])
    for a in imgs:
        if a.get("src"):
            note(a["src"])
    for t in get_tags(html, "iframe"):
        a = parse_attributes(t)
        if a.get("src"):
            note(a["src"])
    dom_count = len(re.findall(r"<[a-z][a-z0-9-]*[\s>/]", markup, re.I))
    risk, why = 0, []
    if size_kb > 1024:
        risk += 2
        why.append("HTML %s KB > 1 MB (+2)" % size_kb)
    elif size_kb > 300:
        risk += 1
        why.append("HTML %s KB > 300 KB (+1)" % size_kb)
    if len(blocking) >= 3:
        risk += 1
        why.append("%d render-blocking scripts ≥ 3 (+1)" % len(blocking))
    if len(scripts) > 25:
        risk += 1
        why.append("%d scripts > 25 (+1)" % len(scripts))
    if len(dom_order) > 10:
        risk += 1
        why.append("%d third-party domains > 10 (+1)" % len(dom_order))
    if dom_count > 1500:
        risk += 1
        why.append("{:,} HTML elements > 1,500 (+1)".format(dom_count))
    perf = "High" if risk >= 4 else "Medium" if risk >= 2 else "Low"
    d["Scripts"] = len(scripts)
    d["Render-Blocking Scripts"] = "%d: %s" % (len(blocking), ", ".join(short_name(s["attrs"]["src"]) for s in blocking[:15])) if blocking else "None"
    d["Third-Party Domains"] = ("%d: " % len(dom_order) + ", ".join(h + (" (%s)" % ", ".join(dom_hints[h]) if dom_hints[h] else "") for h in dom_order[:15])) if dom_order else 0
    d["Performance Risk"] = "%s – %d pt%s%s. Scale: 0–1 Low, 2–3 Medium, 4+ High" % (perf, risk, "" if risk == 1 else "s", ": " + "; ".join(why) if why else " (all 6 checks within limits)")
    if perf == "High":
        issues.append("High performance risk")
    if len(blocking) >= 3:
        issues.append("Render-blocking scripts in head")

    # Technology
    d["CMS / Platform"] = detect_platform(html, meta("generator"), res.headers)
    tr = detect_tracking(html)
    d["Tracking Detected"] = ", ".join(tr["list"]) if tr["list"] else "None found"
    if not tr["has_analytics"] and "webPixelsConfigList" not in html:
        issues.append("No analytics detected")
    if tr["legacy_ua"]:
        issues.append("Legacy Universal Analytics code")
    app_shell = bool(re.search(r"<div[^>]+id=[\"'](root|app|__next|__nuxt|svelte)[\"'][^>]*>\s*</div>", html, re.I))
    js_risk = "High" if word_count < 50 and (app_shell or len(scripts) > 5) else "Medium" if word_count < 150 and (app_shell or inline_bytes > 100 * 1024) else "Low"
    d["JS-Rendering Risk"] = "%s – %d words in raw HTML, %d scripts%s. Rule: High = under 50 words + app shell or 5+ scripts; Medium = under 150 words + app shell or 100 KB+ inline code" % (
        js_risk, word_count, len(scripts), ", empty app container (#root/#app)" if app_shell else "")
    if js_risk == "High":
        issues.append("Content may depend on JavaScript")

    # Structured data
    nodes, ld_errors = extract_json_ld(html)
    micro = [m + " (microdata)" for m in re.findall(r"itemtype=[\"']https?://schema\.org/([A-Za-z]+)[\"']", markup, re.I)]
    types = unique([t for n in nodes for t in types_of(n)] + micro)
    missing_props = []
    for n in nodes:
        if not [k for k in n if k not in ("@type", "@id", "@context")]:
            continue
        for t in types_of(n):
            rule = SCHEMA_RULES.get(t) or (SCHEMA_RULES["LocalBusiness"] if LOCAL_BUSINESS_TYPES.match(t) else None)
            if not rule:
                continue
            lacks = [p.split("|")[0] for p in rule if not any(has_value(n.get(k)) for k in p.split("|"))]
            if lacks:
                missing_props.append(t + ": " + ", ".join(lacks))
    d["Schema Types"] = ", ".join(types) if types else "None"
    d["Schema Parse Errors"] = ld_errors
    d["Schema Missing Properties"] = "; ".join(unique(missing_props)[:8]) if missing_props else "None"
    d["Breadcrumbs"] = breadcrumb_text(nodes, markup)
    if not types:
        issues.append("No structured data")
    if ld_errors:
        issues.append("Structured data has errors")
    if missing_props:
        issues.append("Structured data missing key properties")

    body_tag = re.search(r"<body\b[^>]*>", html, re.I)
    body_class = parse_attributes(body_tag.group(0)).get("class", "") if body_tag else ""
    shop = is_shop_page(meta("og:type"), nodes, body_class, page["path"])
    archive = None if shop else detect_archive(body_class, page)
    if archive:
        articles = len(re.findall(r"<article\b", main_html, re.I))
        post_links = len([u for u in cl["in_urls"] if not re.search(r"/(tags?|category|categories|author|page|label|topics?)/", u, re.I) and not same_url(u, page_url)])
        posts = articles or post_links
        thin = archive["type"] != "Search results" and not archive["page"] and posts <= THIN_ARCHIVE_POSTS and d["Indexable"] == "Yes"
        d["Archive Page"] = "%s%s · %s%d post%s listed%s%s" % (archive["type"], " · page %d" % archive["page"] if archive["page"] else "", "" if articles else "≈",
                                                             posts, "" if posts == 1 else "s", " ⚠ thin" if thin else "",
                                                             " ⚠ indexable" if archive["type"] == "Search results" and d["Indexable"] == "Yes" else "")
        if "Thin content" in issues:
            issues.remove("Thin content")
        if thin:
            issues.append("Thin archive page")
        if archive["type"] == "Search results" and d["Indexable"] == "Yes":
            issues.append("Search results page is indexable")
    else:
        d["Archive Page"] = "No"

    article = detect_article(meta, nodes, body_class, page["path"], content, shop or bool(archive))
    d["Author"] = find_author(meta, nodes, links, content) if article else "N/A (not a blog/article)"
    disp, check, date_issues = analyze_dates(meta, nodes, markup, content, res.header("last-modified"), title)
    d["Published / Modified"] = disp
    d["Date Check"] = check
    issues.extend(date_issues)
    hits = [label for rx, label in PLACEHOLDERS if re.search(rx, visible, re.I) or re.search(rx, title, re.I)]
    d["Placeholder Text"] = ", ".join(hits) if hits else "None"
    if hits:
        issues.append("Placeholder text found")
    d["_content_hash"] = hash_text(own_text.lower()) if content_words >= 50 else ""
    ex["minhash"] = min_hash(own_text) if content_words >= 100 else None
    return done()
