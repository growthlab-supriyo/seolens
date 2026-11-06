"""Fetching pages: redirect chains, user agents, robots.txt and XML sitemaps."""
import re
import threading

import requests
import urllib3

from .htmltools import decode_entities, norm, resolve_url

urllib3.disable_warnings()

from . import PROJECT_URL, __version__

# Simple Scope always tells websites who it is: it never pretends to be a person's browser to get past a block.
BOT_UA = "Mozilla/5.0 (compatible; SimpleScope/%s; +%s)" % (__version__, PROJECT_URL)
BROWSER_UA = BOT_UA  # kept for the modules that ask for it; it is the same honest identity
TIMEOUT = 20
MAX_REDIRECTS = 5
MAX_BODY = 15 * 1024 * 1024  # Google stops reading HTML after 15 MB


class Response:
    """What the audit needs from one HTTP answer."""

    def __init__(self, url, status, headers, content, text):
        self.url, self.status, self.headers, self.content, self.text = url, status, headers, content, text

    def header(self, name):
        return self.headers.get(name.lower(), "")


class Fetcher:
    """One requests session per thread; every request counted; the user agent can switch to a browser's."""

    def __init__(self, user_agent=BOT_UA):
        self.user_agent = user_agent
        self._local = threading.local()
        self.count = 0
        self._lock = threading.Lock()
        # Authorized access for a site the user owns: extra headers (e.g. a secret header a Cloudflare rule allows) and/or
        # HTTP Basic auth. Sent ONLY to the audited site's own host, never to external links, images or redirects elsewhere.
        self.auth_host = ""
        self.auth_headers = {}
        self.basic_auth = None

    def set_access(self, host, headers=None, basic_auth=None):
        from .htmltools import bare_host
        self.auth_host = bare_host(host)
        self.auth_headers = dict(headers or {})
        self.basic_auth = basic_auth if basic_auth and basic_auth[0] else None

    def _authorized(self, url):
        if not (self.auth_headers or self.basic_auth) or not self.auth_host:
            return False
        from .htmltools import bare_host
        m = re.match(r"^https?://([^/?#]+)", url, re.I)
        return bool(m) and bare_host(m.group(1).split("@")[-1]) == self.auth_host

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                              "Accept-Language": "en-US,en;q=0.9"})
            self._local.s = s
        return s

    def get(self, url, follow=False, headers=None, user_agent=None):
        """One request. Returns (Response, None) or (None, error message)."""
        with self._lock:
            self.count += 1
        h = {"User-Agent": user_agent or self.user_agent}
        if headers:
            h.update(headers)
        auth = None
        if self._authorized(url):
            if follow:
                # Follow redirects by hand so the secret headers never travel to another host.
                cur = url
                for _ in range(MAX_REDIRECTS + 1):
                    res, err = self.get(cur, False, headers, user_agent)
                    if err or not (300 <= res.status < 400 and res.header("location")):
                        return res, err
                    nxt = resolve_url(res.header("location"), cur)
                    if not nxt:
                        return res, err
                    cur = nxt
                return None, "Too many redirects (%d+)" % MAX_REDIRECTS
            h.update(self.auth_headers)
            auth = self.basic_auth
        try:
            r = self._session().get(url, headers=h, auth=auth, allow_redirects=follow, timeout=TIMEOUT, stream=True, verify=True)
            content = r.raw.read(MAX_BODY, decode_content=True) if r.raw else b""
            r.close()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            return Response(r.url, r.status_code, hdrs, content, _decode(content, hdrs.get("content-type", ""))), None
        except requests.exceptions.SSLError as e:
            return None, "SSL error: " + _short(e)
        except requests.exceptions.ConnectionError as e:
            msg = _short(e)
            return None, ("DNS: domain not found" if re.search(r"getaddrinfo|Name or service|nodename|resolve", msg, re.I) else "Connection error: " + msg)
        except requests.exceptions.Timeout:
            return None, "Timeout after %d s" % TIMEOUT
        except Exception as e:  # noqa: BLE001 - any network problem becomes an error row, never a crash
            return None, _short(e)

    def chain(self, url, user_agent=None):
        """Follows redirects one hop at a time: {chain: [(code, url)], final, response, error}."""
        chain, cur = [], url
        for _ in range(MAX_REDIRECTS + 1):
            res, err = self.get(cur, user_agent=user_agent)
            if err:
                return {"chain": chain, "final": cur, "response": None, "error": err}
            chain.append((res.status, cur))
            if 300 <= res.status < 400 and res.header("location"):
                nxt = resolve_url(res.header("location"), cur)
                if not nxt:
                    return {"chain": chain, "final": cur, "response": res, "error": None}
                if any(u.lower() == nxt.lower() for _, u in chain):
                    return {"chain": chain, "final": nxt, "response": None, "error": "Redirect loop"}
                cur = nxt
                continue
            return {"chain": chain, "final": cur, "response": res, "error": None}
        return {"chain": chain, "final": cur, "response": None, "error": "Too many redirects (%d+)" % MAX_REDIRECTS}


def _decode(content, content_type):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        head = content[:2048].decode("ascii", "ignore")
        mm = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
        enc = mm.group(1) if mm else "utf-8"
    try:
        return content.decode(enc, "replace")
    except LookupError:
        return content.decode("utf-8", "replace")


def _short(e):
    s = str(e)
    s = re.sub(r"HTTPSConnectionPool\(host='[^']+', port=\d+\): ", "", s)
    return s[:160]


# ---------------------------------------------------------------- robots.txt

def parse_robots(txt):
    groups, sitemaps, current, last_agent = [], [], None, False
    for raw in txt.splitlines():
        line = re.sub(r"#.*$", "", raw).strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if not last_agent or current is None:
                current = {"agents": [], "rules": [], "crawl_delay": 0}
                groups.append(current)
            current["agents"].append(val.lower())
            last_agent = True
            continue
        last_agent = False
        if key == "sitemap":
            sitemaps.append(val)
        elif key in ("allow", "disallow") and current is not None:
            current["rules"].append((key, val))
        elif key == "crawl-delay" and current is not None:
            try:
                current["crawl_delay"] = float(val)
            except ValueError:
                pass
    return {"groups": groups, "sitemaps": sitemaps}


def find_robots_group(groups, agent="googlebot"):
    a = agent.lower()
    return next((g for g in groups if a in g["agents"]), None) or next((g for g in groups if "*" in g["agents"]), None)


def robots_allows(group, path):
    """Google's rule: the longest matching path wins; on a tie, Allow wins."""
    if not group:
        return True
    best_len, allow = -1, True
    for kind, rule in group["rules"]:
        if not rule:
            continue
        anchored = rule.endswith("$")
        body = re.escape(rule[:-1] if anchored else rule).replace(r"\*", ".*")
        if re.match("^" + body + ("$" if anchored else ""), path):
            ln = len(rule)
            if ln > best_len or (ln == best_len and kind == "allow"):
                best_len, allow = ln, kind == "allow"
    return allow


# ---------------------------------------------------------------- sitemaps

def _locs(xml):
    return [decode_entities(m.strip()) for m in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([\s\S]*?)\s*(?:\]\]>)?\s*</loc>", xml, re.I)]


def _url_entries(xml):
    out = []
    for m in re.finditer(r"<url>([\s\S]*?)</url>", xml, re.I):
        loc = re.search(r"<loc>\s*(?:<!\[CDATA\[)?\s*([\s\S]*?)\s*(?:\]\]>)?\s*</loc>", m.group(1), re.I)
        lm = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", m.group(1), re.I)
        if loc:
            out.append((decode_entities(loc.group(1).strip()), lm.group(1) if lm else ""))
    return out


def read_sitemap(fetcher, candidates, max_children=200):
    """Reads EVERY valid sitemap among the candidates (robots.txt can list several) and the children of sitemap indexes
    (up to max_children in total), merged: {url, files, is_index, child_total, child_read, urls, keys, lastmods, latest}."""
    urls, lastmods, latest = [], {}, ""
    files, child_total, child_read, is_index = [], 0, 0, False
    seen_files = set()

    def add(x):
        nonlocal latest
        for loc, lm in _url_entries(x):
            urls.append(loc)
            if lm:
                lastmods[norm(loc)] = lm
                latest = max(latest, lm)

    for url in candidates:
        if norm(url) in seen_files:
            continue
        seen_files.add(norm(url))
        res, err = fetcher.get(url, follow=True)
        if err or res.status != 200:
            continue
        xml = res.text
        if re.search(r"<sitemapindex[\s>]", xml, re.I):
            is_index = True
            files.append(url)
            # Children already read as a sitemap listed in robots.txt are not counted twice.
            children = [u for u in _locs(xml) if norm(u) not in seen_files]
            child_total += len(children)
            for child in [u for u in children if not re.search(r"\.gz(\?|$)", u, re.I)]:
                if child_read >= max_children or norm(child) in seen_files:
                    continue
                seen_files.add(norm(child))
                cr, cerr = fetcher.get(child, follow=True)
                child_read += 1
                if not cerr and cr.status == 200:
                    add(cr.text)
        elif re.search(r"<urlset[\s>]", xml, re.I):
            files.append(url)
            add(xml)
    if not files:
        return None
    urls = list(dict.fromkeys(urls))
    label = files[0] if len(files) == 1 else "%d sitemaps: %s" % (len(files), ", ".join(files[:4]) + (" …" if len(files) > 4 else ""))
    return {"url": label, "files": files, "is_index": is_index, "child_total": child_total, "child_read": child_read,
            "urls": urls, "keys": {norm(u) for u in urls}, "lastmods": lastmods, "latest": latest}
