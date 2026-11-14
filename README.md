# 🔎 SEO Lens

**A free, open-source SEO crawler and site audit tool.** It crawls up to 10,000 URLs per run and gives you a health dashboard, an Excel report and a site architecture map. It runs on your own computer, so there's no account, no subscription and no tracking.

Made by [Supriyo Mallick](https://www.linkedin.com/in/s-mallick) for SEOs, freelancers and small teams who can't justify a paid crawler licence.

**Try it online:** [seolens.streamlit.app](https://seolens.streamlit.app) (online: up to 500 URLs per crawl) · **Install it:** `pipx install seolens` (up to 10,000 URLs) · [PyPI](https://pypi.org/project/seolens/)

## Install

You need Python 3.10 or newer.

```bash
pipx install seolens
seolens
```

The app opens in your browser at `http://localhost:8501`. (`pip install seolens` works too.)

**Optional JavaScript rendering**, for sites built with React, Vue and similar frameworks:

```bash
pipx install "seolens[js]"
playwright install chromium
```

JavaScript rendering uses Microsoft Edge or Google Chrome if one is installed, and Chromium otherwise.

**From source:**

```bash
git clone https://github.com/growthlab-supriyo/seolens && cd seolens
pip install -e ".[dev]"
seolens
```

## What it checks

| Area | Checks |
|---|---|
| Crawlability & indexing | Status codes, redirect chains and loops, noindex, X-Robots-Tag, canonicals (missing, conflicting, broken, chained), robots.txt, XML sitemaps and sitemap indexes |
| On-page | Titles, meta descriptions, H1–H3, heading levels, title vs H1, viewport, word count |
| Content | Thin pages, exact duplicates, near-duplicates (MinHash), placeholder text, date consistency |
| Internal linking | Inlinks and outlinks, anchors, broken links, click depth, orphan pages, Link Score (PageRank-style) |
| Markup | JSON-LD, Microdata, RDFa, missing schema properties, Open Graph, Twitter cards |
| International | hreflang (return links, codes, targets), `html lang`, og:locale |
| Performance signals | Heavy images, render-blocking scripts, third-party scripts, page weight |
| Advanced technical | URL parameters (tracking links, duplicate variants, private values hidden), canonical clusters, indexability conflicts with evidence, pagination, URL structure, main-content vs total words, internal anchor distribution, and (installed version) raw HTML vs JavaScript-rendered comparison of tags, words, schema and internal links |

**Dashboard:** a filter bar that applies to every tab (URL, folder, status, indexable, severity, issue, theme, page type, click depth, word count, sitemap, plus a filter on any column), then Overview, Action plan, Issues, Pages, Links, Canonicals and parameters, Coverage and site health, and Site map (the interactive architecture map, downloadable as PNG). The tables are the Excel report's own tables. Every table downloads as CSV or Excel, and **Filtered report** builds the full workbook for just the filtered pages. Two themes: Clean Pro (light) and Midnight (dark). The **Excel report** has 11 sheets: a Dashboard (KPI cards, issue-distribution and severity charts, top priorities, audit completeness), an Action Plan to assign and track fixes, Issue Details (one row per URL per rule), Page Audit, On-Page SEO, Technical SEO, Internal Links, Images & Speed, Schema & Social, Crawl Coverage and Raw & Audit Info, plus hidden raw-data sheets.

## Fast, and polite

- **Adaptive speed (AIMD).** It starts at 10 pages at a time on Medium. It speeds up while the site answers quickly and slows down gradually only when the site *stays* slow. A few slow pages don't change anything.
- When a site says it is overloaded (HTTP 429 or 503), the crawler halves its speed and waits for the `Retry-After` time the site asks for.
- It follows robots.txt, including `Crawl-delay`.
- **Pause and resume.** Every queued URL and crawled page is saved to SQLite as the crawl runs. You can pause a crawl, close the app, and resume it later. **Finish now** builds the report from the pages crawled so far.

## Honest crawling: what it does with Cloudflare and firewalls

SEO Lens **does not bypass** Cloudflare, CAPTCHAs or other bot protection, and it never disguises itself as a person's browser. It sends `SEOLens/<version>` in its User-Agent so site owners can see who is crawling them. What it does instead:

- **Detects and labels** each blocked page: Cloudflare challenge, bot check or CAPTCHA, 403, rate limiting (429), server busy (503), timeouts and connection errors. Blocked pages appear in the **Blocked & failed** tab and the Excel report's Crawl Coverage, never silently missing, so an incomplete crawl can't pass for a complete audit.
- **Doesn't hammer a blocked page.** A challenge page is not retried. A 403 is retried once, and only if the site was serving pages normally until then.
- **Authorized access for sites you own.** In Cloudflare, add a WAF custom rule that skips security checks for a secret request header (or for the `SEOLens` User-Agent). Then add that header under *Authorized access* in the app. HTTP Basic login is supported too. Credentials are sent **only** to the site being audited, never to external links, images or redirects to other hosts. They are stored in the local database on your computer.

## Command line

```bash
seolens crawl example.com --max 2000 --speed Medium --render off
seolens crawl --list urls.txt
```

## Where your data lives

The database and reports are stored in `~/.seolens/` (`C:\Users\<you>\.seolens\` on Windows). To use another folder, set `SEOLENS_HOME`. Nothing is sent anywhere else.

## Limits

- 10,000 URLs per crawl.
- Performance checks are signals from the HTML, not measured Core Web Vitals. PageSpeed Insights integration is planned.
- Word count is approximate on pages with a lot of interface text. The report says how the main content was found and how confident that is.
- Canonicals, sitemaps, pagination markup, URL length and anchor repetition are signals: conflicts are listed for review, not as proof of what Google indexes or picks as canonical.
- JavaScript comparison needs the optional browser (installed version only). Selective mode renders the start page and pages that look incomplete without JavaScript, up to a limit (100 by default); every other page is marked "Not measured", never assumed equal.
- URLs with ?parameters are only crawled when you turn it on; otherwise they are analysed from internal links and the sitemap. Parameters are never stripped or merged.

## Development

```bash
pip install -e ".[dev]"
pytest -q tests
```

The tests run a small local website with redirects, a 404, a slow page, a rate-limited page, a Cloudflare-style challenge and a protected page.

| File | What it does |
|---|---|
| `seolens/crawler.py` | Discovery, fetch loop, blocked-page classification, saving and resuming |
| `seolens/throttle.py` | The adaptive concurrency controller |
| `seolens/analyze.py`, `checks.py`, `sitehealth.py` | Per-page analysis, cross-page checks, site-wide checks |
| `seolens/report_sheets.py`, `report_dashboard.py`, `report_data.py`, `report_style.py` | The 11-sheet Excel report |
| `seolens/report.py` | Page columns, health score and the site architecture map |
| `seolens/store.py` | SQLite (WAL) |
| `seolens/worker.py` | Runs the crawl in its own process |
| `seolens/app.py`, `dashboard.py`, `theme.py` | Streamlit app: filters, the 7 dashboard tabs, exports, the Clean Pro and Midnight themes |
| `seolens/snapshot.py` | Saves what each report is built from, for the dashboard and filtered reports |

**Online version:** `streamlit_app.py` runs SEO Lens in online mode (`SEOLENS_ONLINE=1`: 500 URLs per crawl, no JavaScript rendering,
robots.txt always respected, no login fields) on Streamlit Community Cloud. `deploy/docker/Dockerfile` runs the same online version in Docker.

MIT licence.
