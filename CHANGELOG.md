# Changelog

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
