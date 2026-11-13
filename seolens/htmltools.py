"""HTML and URL helpers (shared by every check, so pages are read the same way everywhere).

The HTML is read as text with nesting-aware tag scanning instead of a strict parser: broken real-world HTML
(unclosed tags, stray closing tags) is handled the same way as in the Sheets tool.
"""
import hashlib
import html as _html
import re

import regex  # supports \\p{L} (letters in any language)

# ---------------------------------------------------------------- URLs

_URL_RE = re.compile(r"^(https?)://([^/?#]+)([^?#]*)(\?[^#]*)?(#.*)?$", re.I)


def parse_url(url):
    m = _URL_RE.match(str(url or "").strip())
    if not m:
        return None
    return {"protocol": m.group(1).lower(), "host": m.group(2).lower(), "path": m.group(3) or "/",
            "query": m.group(4) or "", "hash": m.group(5) or ""}


def bare_host(host):
    return re.sub(r"^www\.", "", re.sub(r":\d+$", "", str(host or "").lower()))


def is_internal(host, root):
    return host == root or host.endswith("." + root)


def resolve_url(href, base):
    """Resolves a link against the page URL, including ./ and ../ paths. None for non-http links."""
    href = str(href or "").strip()
    if re.match(r"^https?://", href, re.I):
        return href
    b = parse_url(base)
    if not b:
        return None
    if href.startswith("//"):
        return b["protocol"] + ":" + href
    if re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.I) or href.startswith("#"):
        return None
    if href.startswith("?"):
        return b["protocol"] + "://" + b["host"] + b["path"] + href
    joined = href if href.startswith("/") else re.sub(r"[^/]*$", "", b["path"]) + href
    cut = re.search(r"[?#]", joined)
    path_only = joined[:cut.start()] if cut else joined
    rest = joined[cut.start():] if cut else ""
    segs = []
    for seg in path_only.split("/"):
        if seg == "..":
            if len(segs) > 1:
                segs.pop()
        elif seg != ".":
            segs.append(seg)
    path = "/".join(segs)
    if not path.startswith("/"):
        path = "/" + path
    return b["protocol"] + "://" + b["host"] + path + rest


def norm(url):
    """Key used to compare URLs: lower case, no default port, no trailing slash, query kept, fragment dropped."""
    p = parse_url(url)
    if not p:
        return str(url or "").lower()
    host = re.sub(r":(80|443)$", "", p["host"])
    return (p["protocol"] + "://" + host + p["path"].rstrip("/") + p["query"]).lower()


def same_url(a, b):
    return norm(a) == norm(b)


def queue_key(u):
    p = parse_url(u)
    if not p:
        return str(u).lower()
    return (bare_host(p["host"]) + p["path"].rstrip("/") + p["query"]).lower()


def path_of(u):
    p = parse_url(u)
    return p["path"] + p["query"] if p else "/"


# Account and shopping-action URLs (whole path segment or query key only, so /how-to-open-an-account/ is fine).
UTILITY_SEGMENT = re.compile(r"^(add-to-wishlist|add_to_wishlist|wishlists?|compare|cart|my-cart|basket|checkout|my-account|account|"
                             r"dashboard|profile|log-?in|log-?out|sign-?in|sign-?up|sign-?out|register|lost-password|reset-password|"
                             r"forgot-password|track-order|order-tracking|orders|customer_authentication|customer_identity)$", re.I)
UTILITY_QUERY = re.compile(r"[?&](add-to-cart|add_to_cart|add-to-wishlist|add_to_wishlist|wishlist|compare|remove_item)=", re.I)


def is_utility_link(url):
    p = parse_url(url)
    if not p:
        return False
    return bool(UTILITY_QUERY.search(p["query"])) or any(UTILITY_SEGMENT.match(s) for s in p["path"].split("/"))


FILE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|ico|bmp|css|js|json|xml|txt|md|csv|pdf|zip|rar|7z|gz|docx?|xlsx?|pptx?|mp4|m4v|mov|"
                         r"webm|mp3|wav|woff2?|ttf|eot|apk|exe|dmg)$", re.I)
NON_PAGE_PATH_RE = re.compile(r"/(wp-json|wp-admin|wp-login\.php|xmlrpc\.php|feed|cdn-cgi|trackback|comments/feed)(/|$)", re.I)
ASSET_RE = re.compile(r"\.(css|js|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf)(\?.*)?$", re.I)

# ---------------------------------------------------------------- text

_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


def decode_entities(s):
    s = str(s or "")
    if "&" not in s:
        return s
    out = _html.unescape(s)
    return out.replace("\xa0", " ")


def clean_spaces(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(html):
    return clean_spaces(decode_entities(_TAG_RE.sub(" ", str(html or ""))))


_WORDISH = regex.compile(r"[\p{L}\p{N}]")


def count_words(text):
    """Words in any language: a piece counts when it holds at least one letter or number."""
    return sum(1 for w in str(text or "").split() if _WORDISH.search(w)) if text else 0


def hash_text(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def unique(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- tags

def remove_blocks(html, tags):
    for t in tags:
        html = re.sub(r"<" + t + r"\b[\s\S]*?</" + t + r"\s*>", " ", html, flags=re.I)
    return html


def section_of(html, tag):
    if tag == "body":
        m = re.search(r"<body\b[^>]*>([\s\S]*?)(?:</body\s*>|$)", html, re.I)
    else:
        m = re.search(r"<" + tag + r"\b[^>]*>([\s\S]*?)</" + tag + r"\s*>", html, re.I)
    return m.group(1) if m else ""


def get_tags(html, tag):
    return re.findall(r"<" + tag + r"(?=[\s/>])[^>]*>", html, re.I)


def get_elements(html, tag):
    out = []
    for m in re.finditer(r"<" + tag + r"(?=[\s/>])([^>]*)>([\s\S]*?)</" + tag + r"\s*>", html, re.I):
        out.append({"attrs": parse_attributes("<" + tag + m.group(1) + ">"), "inner": m.group(2)})
    return out


_ATTR_RE = re.compile(r"""([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")


def parse_attributes(tag):
    """Attributes of an opening tag, handling double, single and unquoted values."""
    attrs = {}
    inner = re.sub(r"/?>$", "", re.sub(r"^<[^\s/>]+", "", str(tag)))
    for m in _ATTR_RE.finditer(inner):
        name = m.group(1).lower()
        if name in attrs:
            continue
        val = next((g for g in (m.group(2), m.group(3), m.group(4)) if g is not None), "")
        attrs[name] = decode_entities(val)
    return attrs


def rel_has(attrs, value):
    return value in (attrs.get("rel") or "").lower().split()


def class_tokens(open_tag):
    a = parse_attributes(open_tag)
    return [t for t in ((a.get("class") or "") + " " + (a.get("id") or "")).lower().split() if t]


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_OPEN_CLOSE_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>")


def scan_elements(html, pred):
    """Top-level elements matching pred(tag, open_tag), nesting-aware and tolerant of unclosed tags."""
    stack, found, open_ = [], [], None
    for m in _OPEN_CLOSE_RE.finditer(html):
        closing, tag = m.group(1) == "/", m.group(2).lower()
        if tag in VOID_TAGS or (not closing and m.group(0).endswith("/>")):
            continue
        if not closing:
            stack.append(tag)
            if open_ is None and pred(tag, m.group(0)):
                open_ = {"depth": len(stack), "start": m.start(), "inner_start": m.end()}
        else:
            idx = len(stack) - 1 - stack[::-1].index(tag) if tag in stack else -1
            if idx == -1:
                continue
            del stack[idx:]
            if open_ is not None and len(stack) < open_["depth"]:
                found.append({"start": open_["start"], "inner_start": open_["inner_start"], "inner_end": m.start(), "end": m.end()})
                open_ = None
    if open_ is not None:
        found.append({"start": open_["start"], "inner_start": open_["inner_start"], "inner_end": len(html), "end": len(html), "unclosed": True})
    return found


def remove_elements_where(html, pred):
    out, last = [], 0
    for f in scan_elements(html, pred):
        if f.get("unclosed"):
            continue
        out.append(html[last:f["start"]] + " ")
        last = f["end"]
    out.append(html[last:])
    return "".join(out)


# ---------------------------------------------------------------- content area

NON_CONTENT_TAGS = {"header", "footer", "nav", "aside", "dialog", "template", "button", "select"}
SIDEBAR_TOKEN = re.compile(r"^(sidebar|sidebar-container|sidebar-inner|sidebar-widgets|wd-sidebar|widget-area|secondary|site-sidebar|shop-sidebar|blog-sidebar)$")
NON_CONTENT_TOKEN = re.compile(r"^(hidden|d-none|is-hidden|screen-reader-text|sr-only|visually-hidden|widget|menu|toc)$|side-hidden|offcanvas|off-canvas|"
                               r"mobile-nav|mobile-menu|modal|popup|drawer|cookie|menu-item|breadcrumb|table-of-contents|ez-toc|rank-math-toc|lwptoc|"
                               r"(^|-)share(-|$)|sharing|social|related|author-box|^comments?$|comment-|newsletter|toolbar|bottom-bar|mobile-bar|"
                               r"sticky-footer|nav-mb")
REACT_STREAM_ID = re.compile(r"^[SP]:\d+$")
RESPONSIVE_SHOW = re.compile(r"^(sm|md|lg|xl|2xl):(block|flex|grid|inline|inline-block|inline-flex|table|contents|flow-root)$|"
                             r"^d-(sm|md|lg|xl|xxl)-(block|flex|grid|inline|inline-block|inline-flex|table)$")


def is_non_content(tag, open_tag):
    if tag in NON_CONTENT_TAGS:
        return True
    a = parse_attributes(open_tag)
    if ("hidden" in a and not REACT_STREAM_ID.match(a.get("id") or "")) or a.get("aria-hidden") == "true" or a.get("role") in ("navigation", "dialog"):
        return True
    if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style") or "", re.I):
        return True
    tokens = class_tokens(open_tag)
    shown_larger = any(RESPONSIVE_SHOW.match(t) for t in tokens)
    return any((NON_CONTENT_TOKEN.search(t) or SIDEBAR_TOKEN.match(t)) and not t.startswith("elementor-widget")
               and not (shown_larger and t in ("hidden", "d-none")) for t in tokens)


# Grid sidebars without a "sidebar" class: a narrow column (2-4 of 12) right next to a wide one (8-10 of 12).
WIDE_COL = re.compile(r"^(col-(md|lg|xl|xxl)-(8|9|10)|(medium|large)-(8|9|10))$")
NARROW_COL = re.compile(r"^(col-(md|lg|xl|xxl)-[2-4]|(medium|large)-[2-4])$")


def remove_grid_sidebars(html):
    cols = [f for f in scan_elements(html, lambda t, o: any(WIDE_COL.match(x) or NARROW_COL.match(x) for x in class_tokens(o))) if not f.get("unclosed")]
    for f in cols:
        f["wide"] = any(WIDE_COL.match(x) for x in class_tokens(html[f["start"]:f["inner_start"]]))

    def between(a, b):
        return re.sub(r"<!--[\s\S]*?-->", "", html[a["end"]:b["start"]]).strip() == ""

    drop = []
    for i, c in enumerate(cols):
        if c["wide"] or re.search(r"<h1\b", html[c["inner_start"]:c["inner_end"]], re.I):
            continue
        prev_wide = i > 0 and cols[i - 1]["wide"] and between(cols[i - 1], c)
        next_wide = i + 1 < len(cols) and cols[i + 1]["wide"] and between(c, cols[i + 1])
        if prev_wide or next_wide:
            drop.append(c)
    if not drop:
        return html
    out, last = [], 0
    for f in drop:
        out.append(html[last:f["start"]] + " ")
        last = f["end"]
    out.append(html[last:])
    return "".join(out)


def strip_non_content(html):
    """Removes menus, header/footer, sidebars, hidden panels and small forms (login, search, newsletter)."""
    cleaned = remove_elements_where(remove_grid_sidebars(html), is_non_content)
    out, last = [], 0
    for f in scan_elements(cleaned, lambda t, o: t == "form"):
        if f.get("unclosed"):
            continue
        inner = cleaned[f["inner_start"]:f["inner_end"]]
        if re.search(r"type=[\"']password[\"']", inner, re.I) or len(clean_text(inner)) < 400:
            out.append(cleaned[last:f["start"]] + " ")
            last = f["end"]
    out.append(cleaned[last:])
    return "".join(out)


MAIN_CONTENT_TOKEN = re.compile(r"^(entry-content|post-content|wd-entry-content|article-content|article-body|blog-content|single-content|post-body|"
                                r"td-post-content|page-content|rte|elementor-widget-theme-post-content|product-description|term-description)$")


def main_content_html(content_html):
    """The main content area (article body, entry content, <article>, <main>), non-content blocks removed."""
    def pick(pred):
        best, best_len = "", 0
        for f in scan_elements(content_html, pred):
            inner = content_html[f["inner_start"]:f["inner_end"]]
            ln = len(clean_text(strip_non_content(inner)))
            if ln > best_len:
                best, best_len = inner, ln
        return best if best_len > 200 else ""

    root = (pick(lambda t, o: bool(re.search(r"itemprop=[\"']articleBody[\"']", o, re.I)))
            or pick(lambda t, o: any(MAIN_CONTENT_TOKEN.match(x) for x in class_tokens(o)))
            or pick(lambda t, o: t == "article")
            or pick(lambda t, o: t == "main")
            or content_html)
    return strip_non_content(root)


PRODUCT_CARD_TOKEN = re.compile(r"^(card-product|product-card|product-item|product-box|product-tile|product-block|product-grid-item|grid-product|"
                                r"product-miniature|productcard|book-card|item-product|single-product-item|shop-?block|shop-?item|shop-?card|"
                                r"course-?card|course-?item|course-?box|courses?-?block|package-?card|package-?item|plan-?card|pricing-?card|"
                                r"tour-?card|listing-?card|listing-?item)$")


def is_product_grid(tag, open_tag):
    """Product grids or cards: removed before reading headings and contextual links."""
    toks = class_tokens(open_tag)
    if tag in ("ul", "ol", "div", "section") and any(re.match(r"^(products|wd-products|product-grid|products-grid|product-list|collection-grid|product-loop)$", t) for t in toks):
        return True
    return tag in ("li", "div", "article", "section") and any(PRODUCT_CARD_TOKEN.match(t) for t in toks)


def is_bot_check_page(text):
    """A firewall / CDN bot check (Cloudflare, Hostinger, Sucuri, Imperva…) sent instead of the page."""
    t = str(text or "")
    if len(t) > 60000:
        return False
    return bool(re.search(r"just a moment\.\.\.|checking your browser|attention required!? \| cloudflare|cf-chl-|challenge-platform|"
                          r"verify you are (a )?human|are you a robot|ddos protection by|sucuri website firewall|incapsula incident|"
                          r"please enable (javascript and )?cookies to continue|access denied \| [^<]{0,40}used cloudflare|_hcdn_challenge|"
                          r"hcdn-challenge|bot verification", t, re.I)) or (
        len(t) < 3000 and bool(re.search(r"<title>[^<]{0,40}(security check|access denied|captcha|robot check)", t, re.I)))
