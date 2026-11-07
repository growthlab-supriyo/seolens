"""The Overview tab: site health at a glance (health score, crawled-pages breakdown, errors / warnings / notices,
thematic scores, top issues, click depth and status codes)."""
from collections import Counter

import altair as alt
import pandas as pd
import streamlit as st

from .checks import fix_text, priority

# Status colours (fixed, never reused for a data series) + a neutral for "not readable".
GOOD, WARNING, SERIOUS, CRITICAL, NEUTRAL, BLUE = "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#8a8984", "#2a78d6"
LEVEL = {"Critical": "Errors", "High": "Errors", "Medium": "Warnings", "Low": "Notices"}
LEVEL_ICON = {"Errors": "⛔", "Warnings": "⚠️", "Notices": "ℹ️"}

THEMES = {
    "Crawlability & indexing": ["Page fetch failed", "Page not returning 200", "Page is noindex", "Redirect chain", "URL redirects",
                                "Bot-check page", "Search results page is indexable", "isn't indexable", "canonical", "Canonical"],
    "On-page SEO": ["meta title", "Title length", "meta description", "Description length", "H1", "title tags",
                    "Title and H1", "Title year", "viewport"],
    "Content": ["Thin content", "Duplicate content", "Near-duplicate", "Placeholder", "Thin archive", "date", "Date",
                "JavaScript", "published"],
    "Internal linking": ["Broken links", "Orphan page", "Deep page", "anchor", "Document-relative", "Internal links using http",
                         "target=_blank"],
    "Markup & social": ["structured data", "Structured data", "Open Graph", "og:", "Relative URL in hreflang or Open Graph"],
    "Performance": ["images", "Images", "Render-blocking", "performance", "WebP", "Core Web Vitals", "analytics", "Universal Analytics"],
    "International": ["hreflang", "html lang", "og:locale"],
}


def theme_of(issue):
    for theme, keys in THEMES.items():
        if any(k in issue for k in keys):
            return theme
    return "Other"


def _issues(df):
    return df.get("Issues", pd.Series([""] * len(df))).fillna("").map(lambda v: [i.strip() for i in str(v).split(";") if i.strip()])


def _card(title):
    box = st.container(border=True)
    box.markdown("**%s**" % title)
    return box


def overview(df, result, access):
    issues = _issues(df)
    status = pd.to_numeric(df.get("Final Status"), errors="coerce")
    hops = pd.to_numeric(df.get("Redirect Hops"), errors="coerce").fillna(0) if "Redirect Hops" in df else pd.Series(0, index=df.index)
    html = (status == 200) & (access == "OK") & ~df.get("Meta Title", pd.Series("", index=df.index)).astype(str).str.startswith("N/A")
    has_error = issues.map(lambda lst: any(LEVEL[priority(i)] == "Errors" for i in lst))

    # ---------------------------------------------------------------- row 1: health · crawled pages · issue levels
    c1, c2, c3 = st.columns([1, 1.6, 1.2])
    score = result.get("overall")
    with c1:
        box = _card("Site Health")
        if score is None:
            box.caption("Calculated when the crawl finishes.")
        else:
            colour = GOOD if score >= 80 else WARNING if score >= 60 else CRITICAL
            ring = pd.DataFrame({"part": ["Score", "Rest"], "value": [score, 100 - score]})
            chart = alt.Chart(ring).mark_arc(innerRadius=58, outerRadius=72, cornerRadius=4, padAngle=0.02).encode(
                theta=alt.Theta("value:Q", stack=True, sort=None),
                color=alt.Color("part:N", scale=alt.Scale(domain=["Score", "Rest"], range=[colour, "#8a898433"]), legend=None),
                tooltip=[alt.Tooltip("value:Q", title="Points")])
            label = alt.Chart(pd.DataFrame({"t": ["%d%%" % score]})).mark_text(fontSize=28, fontWeight="bold").encode(text="t:N")
            box.altair_chart((chart + label).properties(height=170), width="stretch", theme="streamlit")
            grade = "Good" if score >= 80 else "Needs work" if score >= 60 else "Poor"
            box.caption("%s · average page score minus site-wide penalties" % grade)

    with c2:
        box = _card("Crawled pages")
        cats = [("Healthy", GOOD, (html & ~has_error).sum()),
                ("Have errors", SERIOUS, (html & has_error).sum()),
                ("Redirected", BLUE, ((hops > 0) & (access == "OK")).sum()),
                ("Broken (4xx/5xx)", CRITICAL, ((status >= 400) & (access == "OK")).sum()),
                ("Not readable", NEUTRAL, (access != "OK").sum())]
        cats = [(n, c, int(v)) for n, c, v in cats]
        box.metric("Pages crawled", f"{len(df):,}", label_visibility="collapsed")
        bar = pd.DataFrame([{"Pages": v, "Group": n, "order": i} for i, (n, _c, v) in enumerate(cats) if v])
        if len(bar):
            chart = alt.Chart(bar).mark_bar(height=22, cornerRadius=3, stroke="white", strokeWidth=2).encode(
                x=alt.X("sum(Pages):Q", stack="zero", axis=None),
                color=alt.Color("Group:N", scale=alt.Scale(domain=[n for n, _c, _v in cats], range=[c for _n, c, _v in cats]),
                                legend=None),
                order=alt.Order("order:Q"),
                tooltip=["Group:N", alt.Tooltip("Pages:Q", format=",")])
            box.altair_chart(chart.properties(height=40), width="stretch")
        box.markdown("<div style='display:flex;flex-wrap:wrap;gap:6px 18px;font-size:0.85rem'>" + "".join(
            "<span style='white-space:nowrap'><span style='display:inline-block;width:10px;height:10px;border-radius:2px;"
            "background:%s;margin-right:5px'></span>%s <b>%s</b></span>" % (c, n, f"{v:,}") for n, c, v in cats) + "</div>",
            unsafe_allow_html=True)

    with c3:
        box = _card("Issues found")
        counts = Counter(LEVEL[priority(i)] for lst in issues for i in lst)
        site_checks = result.get("checks") or []
        for chk in site_checks:
            counts["Errors" if chk.get("priority") in ("Critical", "High") else "Warnings" if chk.get("priority") == "Medium" else "Notices"] += 1
        box.markdown("<div style='display:flex;gap:10px;justify-content:space-between'>" + "".join(
            "<div><div style='font-size:0.8rem'>%s %s</div><div style='font-size:1.7rem;font-weight:700'>%s</div></div>"
            % (LEVEL_ICON[l], l, f"{counts.get(l, 0):,}") for l in ("Errors", "Warnings", "Notices")) + "</div>", unsafe_allow_html=True)
        box.caption("Each issue counted once per page it appears on, plus %d site-wide check%s. "
                    "Errors = Critical/High, Warnings = Medium, Notices = Low." % (len(site_checks), "" if len(site_checks) == 1 else "s"))

    # ---------------------------------------------------------------- row 2: thematic reports
    st.markdown("##### Thematic reports")
    n_html = int(html.sum()) or 1
    themes = [t for t in THEMES]
    cols = st.columns(len(themes))
    for col, theme in zip(cols, themes):
        bad = int(sum(1 for ok, lst in zip(html, issues) if ok and any(theme_of(i) == theme for i in lst)))
        pct = (100 * (n_html - bad)) // n_html  # rounded down: 100% only when no page has an issue
        with col.container(border=True):
            st.markdown("<small>%s</small>" % theme, unsafe_allow_html=True)
            st.markdown("<span style='font-size:1.6rem;font-weight:700'>%d%%</span>" % pct, unsafe_allow_html=True)
            st.progress(pct / 100)
            st.caption("%s page%s with issues" % (f"{bad:,}", "" if bad == 1 else "s"))

    # ---------------------------------------------------------------- row 3: top issues · depth · status codes
    left, right = st.columns([1.5, 1])
    with left:
        box = _card("Top issues")
        per = Counter(i for lst in issues for i in lst)
        if per:
            top = pd.DataFrame([{"Issue": k, "Level": LEVEL[priority(k)], "Pages": v, "Share": round(100 * v / len(df)), "How to fix": fix_text(k)}
                                for k, v in per.items()])
            top["_r"] = top["Level"].map({"Errors": 0, "Warnings": 1, "Notices": 2})
            top = top.sort_values(["_r", "Pages"], ascending=[True, False]).drop(columns="_r").head(12)
            top["Level"] = top["Level"].map(lambda l: "%s %s" % (LEVEL_ICON[l], l[:-1]))
            box.dataframe(top, hide_index=True, width="stretch", height=420, column_config={
                "Share": st.column_config.ProgressColumn("% of pages", format="%d%%", min_value=0, max_value=100),
                "Pages": st.column_config.NumberColumn(format="%d"),
                "How to fix": st.column_config.TextColumn(width="large")})
        else:
            box.success("No page-level issues found.")
    with right:
        depth = pd.to_numeric(df.get("Click Depth"), errors="coerce").dropna()
        if len(depth):  # a URL-list audit doesn't follow links: no click depth, so no box
            box = _card("Pages by click depth")
            d = depth.clip(upper=6).astype(int).map(lambda v: "6+" if v >= 6 else str(v)).value_counts().rename_axis("Clicks from homepage").reset_index(name="Pages")
            d["deep"] = d["Clicks from homepage"].map(lambda v: "4+ clicks" if v in ("4", "5", "6+") else "Within 3 clicks")
            chart = alt.Chart(d).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=26).encode(
                x=alt.X("Clicks from homepage:N", sort=["0", "1", "2", "3", "4", "5", "6+"], axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Pages:Q", axis=alt.Axis(grid=True, tickCount=4)),
                color=alt.Color("deep:N", scale=alt.Scale(domain=["Within 3 clicks", "4+ clicks"], range=[BLUE, WARNING]),
                                legend=alt.Legend(orient="top", title=None)),
                tooltip=["Clicks from homepage:N", alt.Tooltip("Pages:Q", format=",")])
            box.altair_chart(chart.properties(height=230), width="stretch")
        box2 = _card("Status codes")
        sc = df["Final Status"].astype(str).map(lambda v: v[0] + "xx" if v[:1] in "2345" and len(v) == 3 else "Error").value_counts()
        sc = sc.rename_axis("Code").reset_index(name="Pages")
        colours = {"2xx": GOOD, "3xx": BLUE, "4xx": CRITICAL, "5xx": SERIOUS, "Error": NEUTRAL}
        chart = alt.Chart(sc).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, size=18).encode(
            y=alt.Y("Code:N", sort=list(colours), title=None),
            x=alt.X("Pages:Q", axis=alt.Axis(grid=True, tickCount=4)),
            color=alt.Color("Code:N", scale=alt.Scale(domain=list(colours), range=list(colours.values())), legend=None),
            tooltip=["Code:N", alt.Tooltip("Pages:Q", format=",")])
        box2.altair_chart(chart.properties(height=130), width="stretch")
