# Changelog

## Unreleased

- Optional CrUX API key: real-user Core Web Vitals (LCP, INP, CLS) per page and for the whole site.
- Reports show only what the audit checked: a URL-list audit has no link-graph columns or Internal Linking sheet; a folder
  audit has no site-wide checks; empty sheets and columns are left out; no "N/A" anywhere.
- Images sheet lists only images over 200 KB or without alt text, with the size in the Heavy column.
- Links sheet: each problem link once, with the pages it is on; links other sites refuse to check are listed apart.
- Internal Linking › Folders lists only real folders (2+ pages); a page is no longer counted as its own folder.
- About sheet: one item per line. Times follow the user's time zone in the app and the report.
- Removed: author, published/modified dates and date checks, JavaScript-rendering risk, script count, performance risk
  and image width/height.
- The URL box appears only for "List of URLs". JavaScript rendering stays off unless turned on, installed version only.
- "Open in Google Sheets" help next to the Excel download.

## 0.1.0 (first public release)

- Crawl up to 10,000 URLs: whole site, a folder or a URL list; every XML sitemap and sitemap index.
- About 60 checks per page and 35 site-wide checks, each issue with a priority and a how-to-fix note.
- Adaptive crawl speed (AIMD), Retry-After and robots.txt Crawl-delay respected; page analysis on several CPU cores.
- Server errors are rechecked slowly at the end of a crawl; intermittent ones are reported separately.
- Honest crawling: named User-Agent; Cloudflare challenges, CAPTCHAs, 403 and 429 detected and reported, never bypassed.
- Authorized access for sites you own (extra headers, HTTP login), sent only to the audited site.
- Pause, resume (also after a restart) and finish now; every crawl saved in SQLite.
- Streamlit dashboard with an Overview, Issues, Pages, Response codes, Blocked & failed, Broken links and Site health.
- Excel report: Summary with the top 10 actions, Action Plan, Issue URLs, Pages, Response Codes, Links, Images,
  Internal Linking, Site Health, Sitemap & Discovery, About This Audit, All Data; plus a site architecture map.
- Optional JavaScript rendering with Playwright.
