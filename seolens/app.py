"""SEO Lens: the Streamlit app. It only starts crawls and reads the database; the crawl itself runs in worker.py."""
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # `streamlit run seolens/app.py` from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from seolens import MAX_URLS, ONLINE_MAX_URLS, __version__
from seolens.store import Store
from seolens.worker import pid_alive, start_worker

ONLINE = os.environ.get("SEOLENS_ONLINE") == "1"   # the shared online version: smaller crawls, no secrets, no JS rendering
LIVE = ("queued", "running", "finishing")
STATUS_ICON = {"queued": "⏳", "running": "🕷️", "finishing": "🧮", "done": "✅", "paused": "⏸", "stopped": "⏹",
               "failed": "❌", "interrupted": "⚠️"}
KEY_COLUMNS = ["URL", "Final Status", "Crawl Access", "Indexable", "Meta Title", "Title Length", "Meta Description",
               "Description Length", "H1 Text", "Word Count", "Canonical URL", "Click Depth", "Link Score (0-100)",
               "Linked From (pages)", "Page Score", "Issues"]

st.set_page_config(page_title="SEO Lens", page_icon="🔎", layout="wide")


@st.cache_resource
def store():
    return Store()


db = store()


def viewer_tz():
    """The visitor's own time zone (from the browser), so every device shows its local time, not the server's."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    try:
        if st.context.timezone:
            return ZoneInfo(st.context.timezone)
    except Exception:  # noqa: BLE001 - unknown zone name: fall back to the offset
        pass
    off = getattr(st.context, "timezone_offset", None)
    return timezone(timedelta(minutes=-off)) if isinstance(off, (int, float)) else None


def fmt_time(ts, fmt="%d %b %H:%M"):
    return datetime.fromtimestamp(ts, viewer_tz()).strftime(fmt) if ts else ""


def run_label(r):
    return "%s #%d %s · %s" % (STATUS_ICON.get(r["status"], ""), r["id"], r["target"][:40], fmt_time(r["created"]))


def check_alive(run):
    """A crawl whose process is gone (computer slept, app closed) becomes 'interrupted' and can resume."""
    if run["status"] in LIVE and run["pid"] and not pid_alive(run["pid"]) and time.time() - run["updated"] > 5:
        db.update_run(run["id"], status="interrupted")
        run["status"] = "interrupted"
    return run


# ---------------------------------------------------------------- sidebar: new crawl + history

with st.sidebar:
    st.title("🔎 SEO Lens")
    st.caption("Free, open-source SEO crawler · v%s" % __version__)
    # Outside the form, so choosing "List of URLs" shows the URL box at once.
    mode = st.radio("What to crawl", ["Website or folder", "List of URLs"], horizontal=True)
    with st.form("new"):
        site, urls = "", ""
        if mode == "List of URLs":
            urls = st.text_area("URLs (one per line)", height=120, placeholder="https://example.com/page-1\nhttps://example.com/page-2")
        else:
            site = st.text_input("Website", placeholder="example.com or example.com/blog/")
        cap = ONLINE_MAX_URLS if ONLINE else MAX_URLS
        max_pages = st.number_input("Maximum URLs", 1, cap, min(500, cap), step=100)
        speed = st.select_slider("Speed", ["Polite", "Medium", "Fast"], "Medium",
                                 help="Starting pages at a time: Polite 4, Medium 10, Fast 20. It adjusts by itself: "
                                      "slower when the server gets slow or asks to slow down, faster when it answers quickly.")
        render, render_max, crux_key = "off", 100, ""
        with st.expander("More settings"):
            if not ONLINE:  # JavaScript rendering needs a browser on the computer: installed version only
                render = st.selectbox("JavaScript comparison (optional)", ["off", "auto", "all"],
                                      format_func={"off": "Off (default): HTML only, fastest",
                                                   "auto": "Selective: start page + pages that look incomplete without JavaScript",
                                                   "all": "All: every page"}.get,
                                      help="Compares the raw HTML with the page after JavaScript runs (title, description, H1, canonical, "
                                           "robots, words, schema, internal links). Needs Playwright: pip install \"seolens[js]\" then "
                                           "playwright install chromium.")
                render_max = st.slider("Maximum pages to render", 25, 500, 100, step=25,
                                       help="Rendering is slow: pages over this limit are listed as 'Not measured'.")
            content_min = st.number_input("Main-content review threshold (words)", 100, 1000, 300, step=50,
                                          help="Pages whose main content (menus, header and footer not counted) is shorter are flagged "
                                               "for review. Half for the homepage, product and about pages; contact, gallery, legal "
                                               "pages and archives are not judged.")
            url_max = st.number_input("URL length review threshold (characters)", 75, 250, 115, step=5,
                                      help="Longer URLs are listed for review. Not an error: established URLs rarely need changing.")
            follow = st.checkbox("Follow links", True)
            robots = st.checkbox("Respect robots.txt", True, help="Turn off only for a site you own.", disabled=ONLINE)
            external = st.checkbox("Check external links", True)
            query = st.checkbox("Crawl URLs with ?parameters", False)
            sitewide = st.checkbox("Folder crawls: also check site-wide health", False)
            crux_key = st.text_input(
                "CrUX API key (optional)", type="password",
                help="Adds real-user Core Web Vitals (LCP, INP, CLS) for each page from Google's Chrome UX Report. Free key: "
                     "console.cloud.google.com → create a project → APIs & Services → Library → enable \"Chrome UX Report API\" → "
                     "Credentials → Create credentials → API key." + (" Used for this crawl only and never saved." if ONLINE else ""))
        auth_headers = auth_user = auth_pass = ""
        if not ONLINE:  # never ask for secrets on a shared public server
            with st.expander("Authorized access (sites you own)"):
                st.caption("For a site you own or may audit that sits behind Cloudflare or a login. Create a rule on your side that "
                           "lets these requests through, then add its secret here. Sent only to this website, never to other sites.")
                auth_headers = st.text_area("Extra request headers", height=68, placeholder="X-Audit-Key: your-secret-value")
                auth_user = st.text_input("Username (HTTP login)", "")
                auth_pass = st.text_input("Password (HTTP login)", "", type="password")
        go = st.form_submit_button("Start crawl", type="primary", width="stretch")
    if go:
        settings = {"mode": "list" if mode == "List of URLs" else "domain", "site": site.strip(),
                    "urls": [u.strip() for u in urls.splitlines() if u.strip()][:cap], "max_pages": int(max_pages),
                    "speed": speed, "render_js": "off" if ONLINE else render, "follow_links": follow, "respect_robots": robots,
                    "check_external": external, "include_query": query, "site_wide_health": sitewide,
                    "render_max": int(render_max), "content_min": int(content_min), "url_max": int(url_max),
                    "auth_headers": auth_headers, "auth_user": auth_user.strip(), "auth_pass": auth_pass,
                    "tz": str(getattr(viewer_tz(), "key", "") or "")}
        settings["tz_offset"] = getattr(st.context, "timezone_offset", None)
        if crux_key.strip():
            if ONLINE:
                settings["crux_requested"] = True
            else:
                settings["crux_key"] = crux_key.strip()  # your own computer: kept with the crawl so a resume can use it
        if settings["mode"] == "domain" and not settings["site"]:
            st.error("Enter a website.")
        elif settings["mode"] == "list" and not settings["urls"]:
            st.error("Paste at least one URL.")
        elif any(r["status"] in LIVE and pid_alive(db.run(r["id"])["pid"]) for r in db.runs(20)):
            st.error("A crawl is already running. Pause or finish it first.")
        else:
            rid = db.create_run(settings)
            # Online: the key goes only to the crawler process's memory, never into the database.
            db.update_run(rid, pid=start_worker(rid, crux_key=crux_key.strip() if ONLINE else ""))
            st.session_state.run = rid
            st.session_state.setdefault("my_runs", []).append(rid)
            st.rerun()

    st.divider()
    history = db.runs(50)
    if ONLINE:  # on the shared online version, each visitor sees only the crawls started in their own browser session
        history = [r for r in history if r["id"] in st.session_state.get("my_runs", [])]
    if history:
        ids = [r["id"] for r in history]
        cur = st.session_state.get("run", ids[0])
        st.session_state.run = st.radio("Crawls", ids, index=ids.index(cur) if cur in ids else 0,
                                        format_func=lambda i: run_label(next(r for r in history if r["id"] == i)))
    if ONLINE:
        st.info("Online version: up to %d URLs per crawl. For 10,000-URL crawls and JavaScript rendering, install it free "
                "on your computer: `pipx install seolens`" % ONLINE_MAX_URLS)


# ---------------------------------------------------------------- main area

rid = st.session_state.get("run")
if ONLINE and rid not in st.session_state.get("my_runs", []):
    rid = None
if not rid or not db.run(rid):
    st.header("A free SEO crawler you can trust")
    if ONLINE:
        st.markdown(
            "This online version crawls up to **%d URLs** per crawl. Installed on your own computer, SEO Lens crawls up to "
            "**%s URLs** and can render JavaScript: `pipx install seolens`, then `seolens`.\n\n"
            "- Checks status codes, redirects, titles, descriptions, headings, canonicals, hreflang, schema, images, "
            "internal links, click depth, orphan pages, duplicates and near-duplicates.\n"
            "- Crawls you start here are visible only to you, and are deleted when the app restarts. No account, no tracking.\n"
            "- **Honest crawling.** It says who it is, respects robots.txt, slows down when a site asks, and reports blocked "
            "pages instead of hiding them.\n\n"
            "Enter a website on the left to start." % (ONLINE_MAX_URLS, f"{MAX_URLS:,}"))
        st.stop()
    st.markdown(
        "- Crawls up to **%s URLs**: status codes, redirects, titles, descriptions, headings, canonicals, hreflang, "
        "schema, images, internal links, click depth, orphan pages, duplicates and near-duplicates.\n"
        "- Runs **on your computer**. Crawl data never leaves it, and there's no account and no tracking.\n"
        "- **Honest crawling.** It says who it is, respects robots.txt, slows down when a site asks, and reports blocked "
        "pages instead of hiding them.\n\n"
        "Enter a website on the left to start." % f"{MAX_URLS:,}")
    st.stop()


@st.fragment(run_every=2)
def live_view(run_id):
    run = check_alive(db.run(run_id))
    p = run["progress"]
    if run["status"] not in LIVE:
        st.rerun()  # finished: draw the full results page
    total, done = p.get("total") or 0, p.get("done") or 0
    phase = {"discovering": "Reading robots.txt and the sitemap", "crawling": "Crawling", "checking": "Checking links and images",
             "site": "Site Health checks", "scoring": "Scoring pages", "summary": "Writing the report"}.get(p.get("phase"), "Starting")
    st.progress(min(1.0, done / total) if total else 0.0, text="%s · %d / %d pages" % (phase, done, total))
    if p.get("step") and p.get("phase") != "crawling":
        st.caption("Now: " + p["step"])
    if p.get("phase") == "checking" and p.get("check_total"):
        st.progress(min(1.0, (p.get("check_done") or 0) / p["check_total"]),
                    text="%s · %d / %d" % ("Rechecking busy links" if str(p.get("step", "")).startswith("Rechecking") else
                                           "Checking link targets and images", p.get("check_done") or 0, p["check_total"]))
    c = st.columns(5)
    c[0].metric("Crawled", f"{done:,}")
    c[1].metric("Discovered", f"{p.get('discovered', 0):,}")
    c[2].metric("Pages / second", "%.1f" % (p.get("rate") or 0))
    c[3].metric("Pages at a time", p.get("conc", "–"), help="Set automatically by the speed controller.")
    c[4].metric("Blocked / not readable", p.get("blocked", 0))
    b = st.columns([1, 1, 4])
    if run["status"] == "running" and not run["control"]:
        if b[0].button("⏸ Pause", help="Stops after the pages in progress. Resume any time, even after a restart."):
            db.update_run(run_id, control="pause")
        if b[1].button("⏹ Finish now", help="Stops crawling and builds the report from the pages crawled so far."):
            db.update_run(run_id, control="finish")
    elif run["control"]:
        b[0].info("Pausing…" if run["control"] == "pause" else "Finishing…")
    with st.expander("Log (newest first: every page, link check and step)", expanded=True):
        lines = db.logs(run_id, 300)
        st.code("\n".join("%s  %s" % (fmt_time(t, "%H:%M:%S"), m) for t, m in reversed(lines)) or "…", language=None, height=420)


@st.cache_data(max_entries=4)
def pages_frame(run_id, updated):
    rows = db.page_rows(run_id)
    df = pd.DataFrame(rows)
    from seolens.report import HIDDEN_COLUMNS
    df = df.drop(columns=[c for c in df.columns if c.startswith("_") or c in HIDDEN_COLUMNS], errors="ignore")
    for col in df.columns:  # mixed columns (numbers and "" or text): numbers where they all are, else text
        if df[col].dtype == object:
            filled = df[col].map(lambda v: v not in ("", None))
            nums = pd.to_numeric(df[col].where(filled), errors="coerce")
            df[col] = nums if nums.notna().sum() == filled.sum() and filled.any() else df[col].map(
                lambda v: "" if v is None else v if isinstance(v, str) else str(v))
    blank = lambda v: v in ("", None) or (isinstance(v, str) and v.startswith("N/A")) or (isinstance(v, float) and v != v)  # noqa: E731
    return df[[c for c in df.columns if c == "URL" or not df[c].map(blank).all()]]  # columns that don't apply are left out


run = check_alive(db.run(rid))
shown = "running" if run["status"] in LIVE else run["status"]  # the header isn't redrawn while the live panel updates
st.subheader("%s %s" % (STATUS_ICON.get(shown, ""), run["target"]))
st.caption("Crawl #%d · started %s · %s" % (run["id"], fmt_time(run["created"]), shown))

if run["status"] in LIVE:
    live_view(rid)
    st.stop()

if run["status"] in ("paused", "interrupted", "stopped"):
    p = run["progress"]
    st.info("%s after %d of %d pages. Everything crawled so far is saved." % (
        "Paused" if run["status"] == "paused" else "The crawl stopped (the app or computer was closed)",
        p.get("done", 0), p.get("total", 0)))
    c = st.columns([1, 1, 4])
    if c[0].button("▶ Resume", type="primary"):
        db.update_run(rid, status="queued", pid=start_worker(rid, resume=True))
        st.rerun()
    if c[1].button("📄 Build report now"):
        db.update_run(rid, status="queued", pid=start_worker(rid, resume=True, finish=True))
        st.rerun()
    if ONLINE and run["settings"].get("crux_requested"):
        st.caption("Core Web Vitals need the CrUX key again after a resume: it is never saved on the online version.")
if run["status"] == "failed":
    st.error(run["error"].split("\n")[0])
    if "\n" in run["error"]:  # only an unexpected error has technical details worth showing
        with st.expander("Technical details"):
            st.code(run["error"])

df = pages_frame(rid, run["updated"])
res = run["result"]
if df.empty:
    st.stop()

# ---------------------------------------------------------------- results

from seolens.overview import column_of
status = pd.to_numeric(column_of(df, "Final Status"), errors="coerce")
access = df.get("Crawl Access", pd.Series(["OK"] * len(df))).fillna("OK")
c = st.columns(6)
c[0].metric("Health score", "%s / 100" % res["overall"] if res.get("overall") is not None else "–")
c[1].metric("Pages", f"{len(df):,}")
c[2].metric("Indexable", int((df.get("Indexable") == "Yes").sum()) if "Indexable" in df else 0)
c[3].metric("4xx", int(((status >= 400) & (status < 500)).sum()))
c[4].metric("5xx", int((status >= 500).sum()))
c[5].metric("Blocked / not readable", int((access != "OK").sum()))

if res.get("xlsx") and Path(res["xlsx"]).exists():
    d = st.columns([1, 1, 4])
    d[0].download_button("⬇ Excel report", Path(res["xlsx"]).read_bytes(), Path(res["xlsx"]).name, type="primary")
    if res.get("map") and Path(res["map"]).exists():
        d[1].download_button("⬇ Site architecture map", Path(res["map"]).read_bytes(), Path(res["map"]).name, mime="text/html")
elif run["status"] != "done":
    st.caption("The Excel report is written when the crawl finishes.")

tabs = st.tabs(["Overview", "Issues", "All pages", "Response codes", "Blocked & failed", "Broken links", "Site health"])

with tabs[0]:
    from seolens.overview import overview
    overview(df, res, access)
tabs = tabs[1:]

with tabs[0]:
    counts = Counter(i.strip() for v in df.get("Issues", pd.Series(dtype=str)).fillna("") for i in str(v).split(";") if i.strip())
    if counts:
        from seolens.checks import fix_text, priority
        idf = pd.DataFrame([{"Issue": k, "Priority": priority(k), "Pages": n, "How to fix": fix_text(k)} for k, n in counts.items()])
        idf["_r"] = idf["Priority"].map({"Critical": 0, "High": 1, "Medium": 2, "Low": 3})
        idf = idf.sort_values(["_r", "Pages"], ascending=[True, False]).drop(columns="_r")
        pick = st.selectbox("Show the pages with this issue", ["–"] + list(idf["Issue"]))
        if pick != "–":
            hit = df[column_of(df, "Issues").fillna("").astype(str).str.contains(pick, regex=False)]
            st.dataframe(hit[[c for c in KEY_COLUMNS if c in hit]], width="stretch", hide_index=True)
        st.dataframe(idf, width="stretch", hide_index=True)
    else:
        st.write("No page issues found (the issue list is built when the crawl finishes).")

with tabs[1]:
    f = st.columns([3, 1, 1])
    q = f[0].text_input("Filter URLs", placeholder="/blog/")
    only_idx = f[1].selectbox("Indexable", ["All", "Yes", "No"])
    cols = f[2].multiselect("Columns", list(df.columns), [c for c in KEY_COLUMNS if c in df.columns])
    view = df
    if q:
        view = view[view["URL"].str.contains(q, case=False, regex=False)]
    if only_idx == "Yes":
        view = view[view["Indexable"] == "Yes"]
    elif only_idx == "No":
        view = view[view["Indexable"] != "Yes"]
    st.caption("%d pages" % len(view))
    st.dataframe(view[cols or ["URL"]], width="stretch", hide_index=True, height=520)
    st.download_button("⬇ CSV (this view)", view[cols or ["URL"]].to_csv(index=False).encode("utf-8-sig"), "pages.csv", "text/csv")

with tabs[2]:
    codes = column_of(df, "Final Status").astype(str).value_counts().rename_axis("Status").reset_index(name="Pages")
    st.dataframe(codes, hide_index=True)
    redir = df[pd.to_numeric(df.get("Redirect Hops"), errors="coerce").fillna(0) > 0] if "Redirect Hops" in df else df.iloc[0:0]
    if len(redir):
        st.markdown("**Redirects**")
        st.dataframe(redir[[c for c in ("URL", "Redirect Hops", "Redirect Chain", "Final URL", "Final Status") if c in redir]],
                     width="stretch", hide_index=True)

with tabs[3]:
    bad = df[access != "OK"]
    st.markdown("Pages the crawler **couldn't read**. They are not counted as broken pages: the site refused them, or didn't answer. "
                "SEO Lens doesn't try to get around a firewall. For a site you own, allow its User-Agent "
                "(`SEOLens`) in Cloudflare (WAF → custom rule → skip) or in your firewall, then crawl again.")
    if len(bad):
        st.dataframe(bad[[c for c in ("URL", "Final Status", "Crawl Access", "Redirect Chain") if c in bad]], width="stretch", hide_index=True)
    else:
        st.success("Every crawled page could be read.")

with tabs[4]:
    if res.get("broken"):
        st.dataframe(pd.DataFrame(res["broken"], columns=["Found on page", "Pages", "Link URL", "Anchor", "Where", "Type", "Status", "Result", "How to fix"]),
                     width="stretch", hide_index=True)
    else:
        st.write("No broken or redirecting links found." if run["status"] == "done" else "Built when the crawl finishes.")

with tabs[5]:
    if res.get("checks"):
        st.dataframe(pd.DataFrame(res["checks"])[["status", "priority", "category", "check", "result", "fix"]],
                     width="stretch", hide_index=True)
    else:
        st.write("All site-wide checks passed." if run["status"] == "done" else "Built when the crawl finishes.")

with st.expander("Danger zone"):
    if st.button("Delete this crawl", help="Removes it from the database (the Excel files stay in the reports folder)."):
        db.delete_run(rid)
        st.session_state.pop("run", None)
        st.rerun()
