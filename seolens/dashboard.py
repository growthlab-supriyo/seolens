"""The results dashboard: a filter bar shared by every tab, then Overview, Action plan, Issues, Pages, Links,
Canonicals and parameters, Coverage and site health.

The tables are the Excel report's own tables (report_sheets.collect_tables), built from the same model, for the filtered
pages: what you see here is what the workbook says. Every table downloads as CSV or Excel, and "Filtered report" builds
the full workbook for the filtered pages only.
"""
import hashlib
import io
import re
import tempfile
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from .htmltools import norm, parse_url
from .report_data import build_model, page_type
from .report_sheets import collect_tables, write_workbook
from .snapshot import rebuild
from .theme import SEVERITY
from .themes import THEMES, theme_of

SEVERITIES = ["Critical", "High", "Medium", "Low"]
PRIORITY_BADGE = {"P0": "red", "P1": "orange", "P2": "yellow", "P3": "blue"}
STATUS_GROUPS = ["2xx", "3xx (redirects)", "4xx", "5xx", "Blocked / not readable", "Didn't load"]
URL_COLUMNS = ("URL", "Source URL", "Destination URL", "Page URL", "Image URL", "Declared Canonical", "Resolved Canonical",
               "Clean URL")
OPERATORS = ["contains", "doesn't contain", "equals", "isn't", "greater than", "less than", "is empty", "isn't empty"]
KEY_COLUMNS = ["URL", "Final Status", "Crawl Access", "Indexable", "Meta Title", "Title Length", "Meta Description",
               "Description Length", "H1 Text", "Word Count", "Canonical URL", "Click Depth", "Link Score (0-100)",
               "Linked From (pages)", "Page Score", "Issues"]
VIEWS = {"Page audit": "PageAudit", "On-page": "OnPage", "Technical": "Technical", "Images and speed": "ImagesSpeed",
         "Schema and social": "SchemaSocial"}


# ---------------------------------------------------------------- data

def _issue_list(row):
    return [i.strip() for i in str(row.get("Issues", "") or "").split(";") if i.strip()]


@st.cache_resource(max_entries=8, show_spinner="Building the dashboard…")
def _model(_db, rid, updated, keep_key, _keep):
    crawler, post, site, started, finished = rebuild(_db, rid, keep=_keep)
    m = build_model(crawler, post, site, started, finished)
    return m, collect_tables(m)


def _arrow_safe(df):
    """Columns with numbers and text mixed become text, so the table can be shown and sorted."""
    for c in df.columns:
        if df[c].dtype == object:
            vals = df[c].dropna()
            kinds = {type(v) for v in vals}
            if len(kinds) > 1 or (kinds and not kinds <= {str}):
                nums = pd.to_numeric(df[c], errors="coerce")
                if nums.notna().sum() == df[c].map(lambda v: v not in ("", None)).sum() and nums.notna().any():
                    df[c] = nums
                else:
                    df[c] = df[c].map(lambda v: "" if v is None else str(v))
    return df


@st.cache_data(max_entries=4, show_spinner=False)
def _pages(_m, rid, updated):
    """One row per audited page with the fields the filters use (all pages, before filtering)."""
    m = _m
    from .checks import priority
    out = []
    for r in m.rows:
        issues = _issue_list(r)
        st_ = r.get("Final Status")
        access = r.get("Crawl Access", "OK") or "OK"
        if access != "OK":
            group = "Blocked / not readable"
        elif st_ == "Error" or not isinstance(st_, int):
            group = "Didn't load"
        elif r.get("Redirect Hops"):
            group = "3xx (redirects)"
        else:
            group = "%dxx" % (st_ // 100) if st_ < 600 else "Didn't load"
        sevs = sorted({priority(i) for i in issues}, key=SEVERITIES.index) if issues else []
        p = parse_url(r.get("Final URL") or r["URL"]) or {}
        segs = [s for s in (p.get("path") or "/").split("/") if s]
        in_sm = None
        if m.has_sitemap:
            in_sm = norm(r.get("Final URL") or r["URL"]) in m.sitemap_keys or norm(r["URL"]) in m.sitemap_keys
        out.append({"URL": r["URL"], "_key": norm(r["URL"]), "_final": norm(r.get("Final URL") or r["URL"]),
                    "Status group": group, "Indexable?": "Yes" if r.get("Indexable") == "Yes" else "No",
                    "Severities": sevs, "Issue list": issues, "Themes": sorted({theme_of(i) for i in issues}),
                    "Page type": r.get("Content Type") or page_type(r), "Folder": "/" + segs[0] + "/" if len(segs) > 1 else "/",
                    "Depth": r.get("Click Depth") if isinstance(r.get("Click Depth"), int) else None,
                    "Words": r.get("Word Count") if isinstance(r.get("Word Count"), int) else None,
                    "In sitemap": in_sm})
    base = pd.DataFrame(out)
    raw = pd.DataFrame(m.rows)
    from .report import HIDDEN_COLUMNS
    raw = raw.drop(columns=[c for c in raw.columns if c.startswith("_") or c in HIDDEN_COLUMNS], errors="ignore")
    return base, _arrow_safe(raw)


# ---------------------------------------------------------------- filters

def _filters(base, raw, link_graph, has_sitemap, rid):
    """The filter bar. Returns (mask over the pages, list of active filter labels)."""
    k = lambda name: "f%s_%s" % (rid, name)  # noqa: E731  (filters are per crawl)
    with st.container(border=True):
        c = st.columns([2.2, 1.1, 1.3, 0.9, 1.4])
        q = c[0].text_input("URL contains", key=k("q"), placeholder="/blog/  (several: /blog/, /news/)")
        folders = [f for f, n in Counter(base["Folder"]).most_common() if n >= 2 and f != "/"]
        folder = c[1].multiselect("Folder", folders, key=k("folder"))
        status = c[2].multiselect("Status", [s for s in STATUS_GROUPS if s in set(base["Status group"])], key=k("status"))
        idx = c[3].selectbox("Indexable", ["All", "Yes", "No"], key=k("idx"))
        sev = c[4].multiselect("Severity", SEVERITIES + ["No issues"], key=k("sev"))
        with st.expander("More filters"):
            d = st.columns(3)
            rules = sorted({i for lst in base["Issue list"] for i in lst})
            issue = d[0].multiselect("Issue", rules, key=k("issue"))
            theme = d[1].multiselect("Issue theme", [t for t in THEMES if any(t in x for x in base["Themes"])], key=k("theme"))
            ptype = d[2].multiselect("Page type", sorted(set(base["Page type"])), key=k("ptype"))
            e = st.columns(3)
            depth = words = None
            if link_graph and base["Depth"].notna().any():
                hi = int(base["Depth"].max())
                if hi > 0:
                    depth = e[0].slider("Click depth", 0, hi, (0, hi), key=k("depth"))
            if base["Words"].notna().any():
                hiw = int(base["Words"].max())
                if hiw > 0:
                    words = e[1].slider("Main content words", 0, hiw, (0, hiw), key=k("words"))
            sm = e[2].selectbox("In XML sitemap", ["All", "Yes", "No"], key=k("sm"), disabled=not has_sitemap,
                                help=None if has_sitemap else "No XML sitemap was found for this site.")
            st.markdown("**Add a filter on any column**")
            conds = st.session_state.setdefault(k("conds"), [])
            for n, cond in enumerate(conds):
                g = st.columns([2, 1.4, 2, 0.5])
                cond["col"] = g[0].selectbox("Column", list(raw.columns), index=list(raw.columns).index(cond["col"])
                                             if cond["col"] in raw.columns else 0, key=k("cc%d" % n), label_visibility="collapsed")
                cond["op"] = g[1].selectbox("Operator", OPERATORS, index=OPERATORS.index(cond["op"]), key=k("co%d" % n),
                                            label_visibility="collapsed")
                cond["val"] = g[2].text_input("Value", cond["val"], key=k("cv%d" % n), label_visibility="collapsed",
                                              disabled=cond["op"] in ("is empty", "isn't empty"))
                if g[3].button("✕", key=k("cx%d" % n), help="Remove this filter"):
                    conds.pop(n)
                    st.rerun()
            if st.button("+ Add filter", key=k("add")):
                conds.append({"col": "Meta Title", "op": "contains", "val": ""})
                st.rerun()

    mask = pd.Series(True, index=base.index)
    active = []
    if q.strip():
        terms = [t.strip().lower() for t in q.split(",") if t.strip()]
        mask &= base["URL"].str.lower().map(lambda u: any(t in u for t in terms))
        active.append("URL contains " + ", ".join(terms))
    if folder:
        mask &= base["Folder"].isin(folder)
        active.append("Folder " + ", ".join(folder))
    if status:
        mask &= base["Status group"].isin(status)
        active.append("Status " + ", ".join(status))
    if idx != "All":
        mask &= base["Indexable?"] == idx
        active.append("Indexable " + idx)
    if sev:
        want = set(sev) - {"No issues"}
        mask &= base["Severities"].map(lambda s: bool(want & set(s)) or ("No issues" in sev and not s))
        active.append("Severity " + ", ".join(sev))
    if issue:
        mask &= base["Issue list"].map(lambda lst: bool(set(issue) & set(lst)))
        active.append("Issue " + ", ".join(issue))
    if theme:
        mask &= base["Themes"].map(lambda lst: bool(set(theme) & set(lst)))
        active.append("Theme " + ", ".join(theme))
    if ptype:
        mask &= base["Page type"].isin(ptype)
        active.append("Page type " + ", ".join(ptype))
    if depth and (depth[0] > 0 or depth[1] < int(base["Depth"].max())):
        mask &= base["Depth"].between(depth[0], depth[1])
        active.append("Click depth %d–%d" % depth)
    if words and (words[0] > 0 or words[1] < int(base["Words"].max())):
        mask &= base["Words"].between(words[0], words[1])
        active.append("Words %d–%d" % words)
    if sm != "All" and has_sitemap:
        mask &= base["In sitemap"] == (sm == "Yes")
        active.append("In sitemap " + sm)
    for cond in st.session_state.get(k("conds"), []):
        col, op, val = cond["col"], cond["op"], cond["val"]
        if col not in raw.columns or (op not in ("is empty", "isn't empty") and val == ""):
            continue
        s = raw[col]
        text = s.map(lambda v: "" if v is None or (isinstance(v, float) and v != v) else str(v))
        if op == "contains":
            m = text.str.contains(val, case=False, regex=False)
        elif op == "doesn't contain":
            m = ~text.str.contains(val, case=False, regex=False)
        elif op == "equals":
            m = text.str.lower() == val.lower()
        elif op == "isn't":
            m = text.str.lower() != val.lower()
        elif op in ("greater than", "less than"):
            num, ref = pd.to_numeric(s, errors="coerce"), pd.to_numeric(val, errors="coerce")
            if pd.isna(ref):
                continue
            m = (num > ref) if op == "greater than" else (num < ref)
            m = m.fillna(False)
        elif op == "is empty":
            m = text.str.strip().isin(["", "None", "Missing", "Not measured"])
        else:
            m = ~text.str.strip().isin(["", "None", "Missing", "Not measured"])
        mask &= m.values
        active.append("%s %s %s" % (col, op, val) if op not in ("is empty", "isn't empty") else "%s %s" % (col, op))
    return mask, active


def _clear_filters(rid):
    for key in list(st.session_state):
        if str(key).startswith("f%s_" % rid):
            del st.session_state[key]


# ---------------------------------------------------------------- tables

def _xlsx(df, sheet):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet[:31])
    return buf.getvalue()


def _show(tables, name, key, drop=(), empty="Nothing to list.", height=None, only=None):
    """A report table with search and sorting (built in), plus CSV and Excel downloads of exactly what is shown."""
    headers, rows = tables.get(name, ([], []))
    if not rows:
        st.info(empty)
        return None
    df = pd.DataFrame(rows, columns=headers)
    if only is not None:
        df = only(df)
        if df.empty:
            st.info(empty)
            return df
    df = df.drop(columns=[c for c in drop if c in df.columns])
    df = _arrow_safe(df)
    cfg = {c: st.column_config.LinkColumn(c, display_text=None) for c in df.columns if c in URL_COLUMNS
           and df[c].astype(str).str.startswith("http").any()}
    st.dataframe(df, hide_index=True, width="stretch", height=height or min(560, 38 + 35 * len(df)), column_config=cfg)
    c = st.columns([1, 1, 5])
    c[0].download_button("⬇ CSV", df.to_csv(index=False).encode("utf-8-sig"), "%s.csv" % key, "text/csv", key="csv_" + key)
    c[1].download_button("⬇ Excel", lambda: _xlsx(df, name), "%s.xlsx" % key,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="xlsx_" + key)
    c[2].caption("%s row%s · search and sort with the table's own tools" % (f"{len(df):,}", "" if len(df) == 1 else "s"))
    return df


# ---------------------------------------------------------------- tabs

def _theme_type():
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001 - older Streamlit or not known yet
        return "light"


def _card(label, value, note="", help=None):
    with st.container(border=True):
        st.metric(label, value, help=help)
        if note:
            st.caption(note)


def _overview(m, tables, res, filtered, base_total, snap_ok=True):
    rows = m.rows
    page_issues = [i for i in m.issues if i.scope == "Page"]
    site_findings = [i for i in m.issues if i.scope == "Site"]
    html = [r for r in rows if r.get("Final Status") == 200 and not str(r.get("Meta Title", "")).startswith("N/A")]
    adv = getattr(m.post, "advanced", {}) or {}
    indexable = sum(1 for r in rows if r.get("Indexable") == "Yes")
    conflicts = sum(1 for r in adv.get("clusters", []) if str(r[8]).startswith(("Conflicting", "Canonical target", "Possible mismatch")))
    idx_conf = sum(1 for r in rows if r.get("Indexability Conflicts"))
    measured = [r for r in rows if str(r.get("Rendering Check", "")).startswith("Measured")]
    diffs = sum(1 for r in measured if not str(r.get("Rendering Differences", "")).startswith("No differences"))
    score = res.get("overall")
    scores = [r["Page Score"] for r in rows if isinstance(r.get("Page Score"), (int, float))]

    c = st.columns(4)
    with c[0]:
        if filtered:
            _card("Average page score", "%d / 100" % round(sum(scores) / len(scores)) if scores else "–",
                  "of the filtered pages · site health %s" % (score if score is not None else "–"))
        else:
            _card("Health score", "%s / 100" % score if score is not None else "–",
                  ("Good" if score >= 80 else "Needs improvement" if score >= 60 else "Poor") if score is not None else "")
    with c[1]:
        _card("Pages", f"{len(rows):,}", ("of %s audited" % f"{base_total:,}") if filtered else "of %s discovered URLs" % f"{len(m.queue):,}")
    with c[2]:
        _card("Indexable", f"{indexable:,}", "%d%% of these pages" % round(100 * indexable / len(rows)) if rows else "")
    with c[3]:
        _card("Issue records", f"{len(page_issues):,}", "%d rules · %d site-wide finding%s" % (
            len({i.rule for i in page_issues}), len(site_findings), "" if len(site_findings) == 1 else "s"))
    old = "Not measured in this crawl (made with an older version)"
    c = st.columns(4)
    with c[0]:
        _card("Canonical conflicts", f"{conflicts:,}" if snap_ok else "–", ("in %d cluster row%s" % (
            len(adv.get("clusters", [])), "" if len(adv.get("clusters", [])) == 1 else "s")) if snap_ok else old)
    with c[1]:
        _card("Parameter URLs", f"{len(adv.get('params', [])):,}" if snap_ok else "–", "in links, sitemap or crawl" if snap_ok else old)
    with c[2]:
        _card("Indexability conflicts", f"{idx_conf:,}" if snap_ok else "–", "pages with mixed signals" if snap_ok else old)
    with c[3]:
        _card("JS rendering differences", f"{diffs:,}" if measured else "Off",
              ("of %d pages rendered" % len(measured)) if measured else "Not measured: rendering was off (installed version only)")

    colours = SEVERITY[_theme_type()]
    left, mid, right = st.columns([1, 1.5, 1.2])
    with left:
        with st.container(border=True):
            st.markdown("**Severity**")
            sev = Counter(i.severity for i in page_issues)
            if sev:
                d = pd.DataFrame([{"Severity": s, "Records": sev.get(s, 0)} for s in SEVERITIES])
                arc = alt.Chart(d).mark_arc(innerRadius=58, outerRadius=88, cornerRadius=3, padAngle=0.015).encode(
                    theta=alt.Theta("Records:Q", stack=True), order=alt.Order("order:Q"),
                    color=alt.Color("Severity:N", scale=alt.Scale(domain=SEVERITIES, range=[colours[s] for s in SEVERITIES]),
                                    legend=alt.Legend(orient="bottom", title=None, columns=2, symbolType="circle")),
                    tooltip=["Severity:N", alt.Tooltip("Records:Q", format=",")]).transform_calculate(
                    order="indexof(['Critical','High','Medium','Low'], datum.Severity)")
                st.altair_chart(arc.properties(height=250), width="stretch")
                st.caption("Issue–URL records. %d site-wide finding%s counted apart." % (len(site_findings), "" if len(site_findings) == 1 else "s"))
            else:
                st.success("No page-level issues.")
    with mid:
        with st.container(border=True):
            st.markdown("**Issue distribution**")
            per = Counter(i.rule for i in page_issues).most_common(10)
            if per:
                sev_of = {i.rule: i.severity for i in page_issues}
                short = lambda r: r if len(r) <= 30 else r[:29].rstrip() + "…"  # noqa: E731
                d = pd.DataFrame([{"Rule": short(r), "Issue": r, "Pages": n, "Severity": sev_of[r]} for r, n in per])
                bar = alt.Chart(d).mark_bar(cornerRadiusEnd=4, height=16).encode(
                    y=alt.Y("Rule:N", sort="-x", title=None, axis=alt.Axis(labelLimit=0)),
                    x=alt.X("Pages:Q", title=None, axis=alt.Axis(grid=True, tickCount=4)),
                    color=alt.Color("Severity:N", scale=alt.Scale(domain=SEVERITIES, range=[colours[s] for s in SEVERITIES]), legend=None),
                    tooltip=["Issue:N", "Severity:N", alt.Tooltip("Pages:Q", format=",")])
                text = bar.mark_text(align="left", dx=4).encode(text=alt.Text("Pages:Q", format=","), color=alt.value("#8c96bd"))
                st.altair_chart((bar + text).properties(height=290), width="stretch")
                st.caption("Unique URLs per rule, top %d, coloured by severity." % len(per))
            else:
                st.success("No page-level issues.")
    with right:
        with st.container(border=True):
            st.markdown("**Top priorities**")
            if not m.tasks:
                st.success("Nothing to fix.")
            for t in m.tasks[:5]:
                st.markdown(":%s-badge[%s] **%s**" % (PRIORITY_BADGE.get(t.priority, "gray"), t.priority, t.rule))
                st.caption("%s · %s" % (("%d URL%s" % (t.affected, "" if t.affected == 1 else "s")) if t.affected else "Site-wide",
                                        t.fix if len(t.fix) <= 110 else t.fix[:110].rsplit(" ", 1)[0] + "…"))

    st.markdown("##### Thematic scores")
    n_html = len(html) or 1
    cols = st.columns(len(THEMES))
    for col, theme in zip(cols, THEMES):
        bad = sum(1 for r in html if any(theme_of(i) == theme for i in _issue_list(r)))
        pct = (100 * (n_html - bad)) // n_html
        with col.container(border=True):
            st.caption(theme)
            st.markdown("### %d%%" % pct)
            st.progress(pct / 100)
            st.caption("%s page%s with issues" % (f"{bad:,}", "" if bad == 1 else "s"))

    a, b = st.columns([1.4, 1])
    with a:
        depth = pd.Series([r.get("Click Depth") for r in rows if isinstance(r.get("Click Depth"), int)], dtype=float)
        with st.container(border=True):
            st.markdown("**Pages by click depth**")
            if len(depth):
                d = depth.clip(upper=6).astype(int).map(lambda v: "6+" if v >= 6 else str(v)).value_counts().rename_axis("Clicks").reset_index(name="Pages")
                d["Group"] = d["Clicks"].map(lambda v: "4+ clicks" if v in ("4", "5", "6+") else "Within 3 clicks")
                ch = alt.Chart(d).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=28).encode(
                    x=alt.X("Clicks:N", sort=["0", "1", "2", "3", "4", "5", "6+"], axis=alt.Axis(labelAngle=0), title="Clicks from the start page"),
                    y=alt.Y("Pages:Q", axis=alt.Axis(grid=True, tickCount=4)),
                    color=alt.Color("Group:N", scale=alt.Scale(domain=["Within 3 clicks", "4+ clicks"], range=[colours["Low"], colours["Medium"]]),
                                    legend=alt.Legend(orient="top", title=None)),
                    tooltip=["Clicks:N", alt.Tooltip("Pages:Q", format=",")])
                st.altair_chart(ch.properties(height=240), width="stretch")
            else:
                st.caption("Not measured: a URL-list audit doesn't follow links.")
    with b:
        with st.container(border=True):
            st.markdown("**Status codes**")
            sc = Counter("Blocked" if (r.get("Crawl Access") or "OK") != "OK" else
                         ("%dxx" % (r["Final Status"] // 100) if isinstance(r.get("Final Status"), int) else "Error") for r in rows)
            d = pd.DataFrame([{"Code": k, "Pages": v} for k, v in sc.items()])
            pal = {"2xx": "#16a34a", "3xx": colours["Low"], "4xx": colours["Critical"], "5xx": colours["High"], "Error": "#8c96bd", "Blocked": "#8c96bd"}
            ch = alt.Chart(d).mark_bar(cornerRadiusEnd=4, size=18).encode(
                y=alt.Y("Code:N", sort=list(pal), title=None), x=alt.X("Pages:Q", axis=alt.Axis(grid=True, tickCount=4), title=None),
                color=alt.Color("Code:N", scale=alt.Scale(domain=list(pal), range=list(pal.values())), legend=None),
                tooltip=["Code:N", alt.Tooltip("Pages:Q", format=",")])
            st.altair_chart(ch.properties(height=240), width="stretch")
    notes = m.notices or ["Every planned check ran: no limits reached, nothing refused."]
    with st.expander("Audit completeness", expanded=bool(m.notices)):
        st.markdown("\n".join("- " + n for n in notes))


def _action_plan(m, tables):
    st.caption("One task per fix, highest priority first. Select a task to see the URLs behind it.")
    headers, rows = tables.get("ActionPlan", ([], []))
    if not rows:
        st.success("No issues found: nothing to fix.")
        return
    df = _arrow_safe(pd.DataFrame(rows, columns=headers).drop(columns=["Owner", "Status", "Target Date", "Notes"], errors="ignore"))
    ev = st.dataframe(df, hide_index=True, width="stretch", height=min(520, 38 + 35 * len(df)), on_select="rerun",
                      selection_mode="single-row", key="plan_pick")
    c = st.columns([1, 1, 5])
    c[0].download_button("⬇ CSV", df.to_csv(index=False).encode("utf-8-sig"), "action-plan.csv", "text/csv", key="csv_plan")
    c[1].download_button("⬇ Excel", lambda: _xlsx(df, "Action Plan"), "action-plan.xlsx", key="xlsx_plan")
    picked = ev.selection.rows if ev and getattr(ev, "selection", None) else []
    if picked:
        tid = df.iloc[picked[0]]["Task ID"]
        st.markdown("**%s · %s**" % (tid, df.iloc[picked[0]]["Issue"]))
        _show(tables, "IssueDetails", "task-" + str(tid), drop=("Task ID",), only=lambda d: d[d["Task ID"] == tid] if "Task ID" in d else d)


def _pages_tab(tables, raw, keep_urls):
    view = st.segmented_control("View", list(VIEWS) + ["All columns"], default="Page audit", key="pages_view", label_visibility="collapsed")
    view = view or "Page audit"
    if view == "All columns":
        df = raw[raw["URL"].map(lambda u: norm(u) in keep_urls)] if keep_urls is not None else raw
        cols = st.multiselect("Columns", list(df.columns), [c for c in KEY_COLUMNS if c in df.columns], key="all_cols")
        df = df[cols or ["URL"]]
        st.dataframe(df, hide_index=True, width="stretch", height=560,
                     column_config={"URL": st.column_config.LinkColumn("URL")} if "URL" in df else None)
        c = st.columns([1, 1, 5])
        c[0].download_button("⬇ CSV", df.to_csv(index=False).encode("utf-8-sig"), "pages.csv", "text/csv", key="csv_allcols")
        c[1].download_button("⬇ Excel", lambda: _xlsx(df, "Pages"), "pages.xlsx", key="xlsx_allcols")
        return
    name = VIEWS[view]
    if name == "Technical":
        _show(tables, name, "technical", empty="No pages.")
    elif name == "ImagesSpeed":
        _show(tables, name, "images-speed", empty="No images over 200 KB or without alt text were found.")
    else:
        _show(tables, name, re.sub(r"\W+", "-", view.lower()), empty="No HTML pages.")


def _links_tab(tables):
    groups = {"Problem links": lambda t: t.startswith("Problem link"), "Page link summary": lambda t: t == "Page link summary",
              "Anchor distribution": lambda t: t == "Anchor distribution",
              "JavaScript differences": lambda t: t in ("Rendered-only link", "Removed by JavaScript", "Destination changed by JavaScript")}
    headers, rows = tables.get("InternalLinks", ([], []))
    present = [g for g, f in groups.items() if any(f(str(r[0])) for r in rows)]
    pick = st.segmented_control("Show", present, default=present[0] if present else None, key="links_view",
                                label_visibility="collapsed") if present else None
    f = groups.get(pick or "", lambda t: True)
    _show(tables, "InternalLinks", "internal-links", only=lambda d: d[d["Edge Type"].astype(str).map(f)], empty="No links to list.")


def _canon_tab(m, tables):
    st.markdown("**Canonical clusters**")
    st.caption("Pages that declare another page as canonical, grouped by the canonical they resolve to. A canonical is a hint, "
               "not proof of what Google picks.")
    _show(tables, "CanonicalClusters", "canonical-clusters", empty="No canonical clusters: every page with a canonical points to itself.")
    st.markdown("**URL parameters**")
    crawled = m.settings.get("include_query") or m.mode == "list"
    st.caption("URLs with ?parameters from internal links, the sitemap or the crawl. Private-looking values are hidden."
               + ("" if crawled else " Parameter URLs weren't crawled in this audit, so their content isn't measured."))
    _show(tables, "ParameterURLs", "parameter-urls", empty="No URLs with ?parameters were found.")
    blocked = (getattr(m.post, "advanced", {}) or {}).get("sitemap_blocked", [])
    if blocked:
        st.markdown("**Sitemap URLs blocked by robots.txt**")
        st.dataframe(pd.DataFrame({"URL": blocked}), hide_index=True, width="stretch",
                     column_config={"URL": st.column_config.LinkColumn("URL")})


def _coverage_tab(m, tables):
    st.markdown("**Crawl coverage**")
    _show(tables, "CrawlCoverage", "crawl-coverage")
    st.markdown("**Site health**")
    st.caption("Every site-wide check, passed ones included.")
    if m.site_checks:
        order = {"Fail": 0, "Warn": 1, "Info": 2, "Pass": 3}
        d = pd.DataFrame([{"Status": c["status"], "Severity": c.get("priority", ""), "Category": c["category"], "Check": c["check"],
                           "Result": c["result"], "Recommendation": c.get("fix", "")} for c in m.site_checks])
        d = d.sort_values("Status", key=lambda s: s.map(order))
        st.dataframe(d, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(d)))
        st.download_button("⬇ CSV", d.to_csv(index=False).encode("utf-8-sig"), "site-health.csv", "text/csv", key="csv_sitehealth")
    else:
        st.caption("Site checks are saved from version 0.5 on: crawl again to see them here.")
    bad = [r for r in m.rows if (r.get("Crawl Access") or "OK") != "OK"]
    st.markdown("**Blocked and not readable**")
    if bad:
        st.caption("The site refused these pages or didn't answer. They aren't counted as broken. SEO Lens never gets around a "
                   "firewall: for a site you own, allow the SEOLens user-agent, then crawl again.")
        st.dataframe(pd.DataFrame([{"URL": r["URL"], "Status": str(r.get("Final Status", "")), "Why": r.get("Crawl Access", ""),
                                    "Redirect chain": str(r.get("Redirect Chain", "") or "")} for r in bad]),
                     hide_index=True, width="stretch", column_config={"URL": st.column_config.LinkColumn("URL")})
    else:
        st.success("Every crawled page could be read.")
    with st.expander("Audit settings and rules"):
        _show(tables, "AuditInfo", "audit-info")


def _site_map_tab(res, filtered):
    """The interactive site architecture map, with its own 'Download PNG' button for the view on screen."""
    path = Path(res["map"]) if res.get("map") else None
    if not path or not path.exists():
        st.info("The site architecture map is drawn when a whole-site or folder crawl finishes (a URL list has no link structure).")
        return
    from streamlit.components.v1 import html as embed
    from .report import with_png_button
    st.caption("Link map by click depth or folder tree. Hover a circle for details, click it to see its links, scroll to zoom. "
               "**Download PNG** in the map's toolbar saves the whole map as an image."
               + (" The map shows the whole crawl: filters don't apply to it." if filtered else ""))
    embed(with_png_button(path.read_text(encoding="utf-8")), height=760, scrolling=False)
    st.download_button("⬇ Interactive map (HTML)", path.read_bytes(), path.name, mime="text/html", key="dl_map_tab")


# ---------------------------------------------------------------- page

def render(db, run, rid):
    res = run.get("result") or {}
    snap_ok = db.snapshot(rid) is not None
    full, full_tables = _model(db, rid, run["updated"], None, None)
    if not full.rows:
        st.info("No pages yet.")
        return
    base, raw = _pages(full, rid, run["updated"])
    mask, active = _filters(base, raw, full.link_graph, full.has_sitemap, rid)
    filtered = bool(active)
    keep = None
    if filtered:
        sel = base[mask]
        keep = set(sel["_key"])
        key = hashlib.sha1("\n".join(sorted(keep)).encode()).hexdigest()
        m, tables = _model(db, rid, run["updated"], key, keep) if len(sel) else (None, None)
    else:
        m, tables = full, full_tables

    bar = st.columns([4, 1])
    bar[0].caption(("Showing **%s of %s pages** · %s" % (f"{int(mask.sum()):,}", f"{len(base):,}", " · ".join(active)))
                   if filtered else "Showing all %s pages. Filters apply to every tab and to the downloads." % f"{len(base):,}")
    if filtered and bar[1].button("Clear filters", width="stretch"):
        _clear_filters(rid)
        st.rerun()

    d = st.columns([1, 1, 1.4, 3])
    if res.get("xlsx") and Path(res["xlsx"]).exists():
        d[0].download_button("⬇ Excel report", Path(res["xlsx"]).read_bytes(), Path(res["xlsx"]).name, type="primary", width="stretch")
        if res.get("map") and Path(res["map"]).exists():
            d[1].download_button("⬇ Site map", Path(res["map"]).read_bytes(), Path(res["map"]).name, mime="text/html", width="stretch")
    if filtered and m is not None:
        name = (Path(res["xlsx"]).stem if res.get("xlsx") else "seolens-report") + "_filtered-%d-pages.xlsx" % len(m.rows)

        def build():
            crawler, post, site, started, finished = rebuild(db, rid, keep=keep)
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "report.xlsx"
                write_workbook(str(p), crawler, post, site, started, finished)
                return p.read_bytes()
        d[2].download_button("⬇ Filtered report (%d pages)" % len(m.rows), build, name, width="stretch",
                             help="The full 11-sheet report for the filtered pages only. Site-wide findings still describe the whole site.",
                             key="dl_filtered")
    if not snap_ok and run["status"] == "done":
        st.info("This crawl was made with an older version: the action plan, site checks and advanced tables are partly empty. "
                "Crawl again to see everything.")
    if m is None:
        st.warning("No page matches these filters.")
        return

    tabs = st.tabs(["Overview", "Action plan", "Issues", "Pages", "Links", "Canonicals and parameters", "Coverage and site health",
                    "Site map"])
    with tabs[0]:
        _overview(m, tables, res, filtered, len(base), snap_ok)
    with tabs[1]:
        _action_plan(m, tables)
    with tabs[2]:
        st.caption("One row per affected URL per rule. Site-wide findings have Scope 'Site'.")
        _show(tables, "IssueDetails", "issue-details", empty="No issues found.")
    with tabs[3]:
        _pages_tab(tables, raw, keep)
    with tabs[4]:
        _links_tab(tables)
    with tabs[5]:
        _canon_tab(m, tables)
    with tabs[6]:
        _coverage_tab(m, tables)
    with tabs[7]:
        _site_map_tab(res, filtered)
