"""Core Web Vitals from real Chrome users (Google's Chrome UX Report API), with the user's own free API key.

For every indexable page: LCP, INP and CLS at the 75th percentile on phones, rated like Google does. Pages with too
little traffic have no data of their own; the whole site's figures then go into Site Health instead.
The API allows 150 requests a minute, so a large site takes a while (about 2.4 pages a second).
"""
import threading
import time

import requests

API = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
METRICS = ["largest_contentful_paint", "interaction_to_next_paint", "cumulative_layout_shift"]
GOOD = {"lcp": 2500, "inp": 200, "cls": 0.1}
POOR = {"lcp": 4000, "inp": 500, "cls": 0.25}
PER_SECOND = 2.4
RATING = {0: "Good", 1: "Needs improvement", 2: "Poor"}


class CruxError(Exception):
    pass


def query(key, url=None, origin=None, session=None):
    """{'lcp': ms, 'inp': ms, 'cls': float} (missing metrics left out), None when CrUX has no data. Raises CruxError for a bad key."""
    body = {"formFactor": "PHONE", "metrics": METRICS}
    body["url" if url else "origin"] = url or origin
    for attempt in range(4):
        r = (session or requests).post(API, params={"key": key}, json=body, timeout=20)
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (400, 401, 403):
            raise CruxError("the CrUX API refused the key (HTTP %d): %s" % (r.status_code, r.text[:160]))
        r.raise_for_status()
        m = r.json().get("record", {}).get("metrics", {})
        out = {}
        for name, short in (("largest_contentful_paint", "lcp"), ("interaction_to_next_paint", "inp"), ("cumulative_layout_shift", "cls")):
            p75 = m.get(name, {}).get("percentiles", {}).get("p75")
            if p75 is not None:
                out[short] = float(p75)
        return out or None
    return None


def rate(values):
    """0 good, 1 needs improvement, 2 poor: the worst of the metrics present (Google's rule)."""
    worst = 0
    for k, v in values.items():
        worst = max(worst, 2 if v > POOR[k] else 1 if v > GOOD[k] else 0)
    return worst


def describe(values):
    parts = []
    if "lcp" in values:
        parts.append("LCP %.1f s" % (values["lcp"] / 1000))
    if "inp" in values:
        parts.append("INP %d ms" % values["inp"])
    if "cls" in values:
        parts.append("CLS %.2f" % values["cls"])
    return ", ".join(parts)


def add_crux(crawler, post, site, key, report, stop):
    rows = crawler.rows
    todo = [i for i, r in enumerate(rows) if r.get("Final Status") == 200 and r.get("Indexable") == "Yes"]
    report({"phase": "checking", "step": "Core Web Vitals (CrUX)",
            "log": "Core Web Vitals: asking the Chrome UX Report for %d indexable page/s (about %d s)" % (len(todo), len(todo) / PER_SECOND)})
    session = requests.Session()
    origin = "%s://%s" % (crawler.protocol, crawler.host)
    try:
        site_values = query(key, origin=origin, session=session)
    except CruxError as e:
        report({"log": "Core Web Vitals skipped: %s" % e})
        post.add("Performance", "Core Web Vitals (real users)", "Info", "Not checked: %s" % e)
        return
    except requests.RequestException as e:
        report({"log": "Core Web Vitals skipped: the CrUX API didn't answer (%s)" % str(e)[:80]})
        return

    lock, found, done = threading.Lock(), 0, 0
    next_slot = [time.monotonic()]

    def wait_turn():
        with lock:
            now = time.monotonic()
            slot = max(now, next_slot[0])
            next_slot[0] = slot + 1 / PER_SECOND
        time.sleep(max(0.0, slot - time.monotonic()))

    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        if stop.is_set():
            return i, None
        wait_turn()
        try:
            return i, query(key, url=rows[i].get("Final URL") or rows[i]["URL"], session=session)
        except (CruxError, requests.RequestException):
            return i, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, values in pool.map(one, todo):
            done += 1
            r = rows[i]
            if values:
                found += 1
                level = rate(values)
                r["Core Web Vitals"] = RATING[level]
                r["LCP (s)"] = round(values["lcp"] / 1000, 2) if "lcp" in values else ""
                r["INP (ms)"] = int(values["inp"]) if "inp" in values else ""
                r["CLS"] = round(values["cls"], 3) if "cls" in values else ""
                if level == 2:
                    post.issue(i, "Poor Core Web Vitals")
                elif level == 1:
                    post.issue(i, "Core Web Vitals need improvement")
            else:
                r["Core Web Vitals"] = "No data for this page (too little Chrome traffic)"
            if done % 10 == 0 or done == len(todo):
                report({"phase": "checking", "check_done": done, "check_total": len(todo)})
    if site_values:
        level = rate(site_values)
        post.add("Performance", "Core Web Vitals (whole site, real users)", {0: "Pass", 1: "Warn", 2: "Fail"}[level],
                 "%s on phones (75th percentile of real Chrome users): %s" % (RATING[level], describe(site_values)),
                 "Medium" if level == 2 else "Low",
                 "Improve the worst metric: LCP (hero image, server response), INP (less JavaScript), CLS (reserve space for images, ads and fonts).")
    else:
        post.add("Performance", "Core Web Vitals (whole site, real users)", "Info",
                 "No Chrome UX Report data for %s: the site has too little Chrome traffic for field data." % origin)
    post.add("Performance", "Pages with their own Core Web Vitals data", "Info", "%d of %d indexable page/s" % (found, len(todo)))
    report({"log": "Core Web Vitals: real-user data for %d of %d page/s%s" % (found, len(todo), "" if site_values else "; no data for the whole site")})
