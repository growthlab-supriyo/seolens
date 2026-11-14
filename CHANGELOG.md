# Changelog

## Unreleased

- The log shows every step: one line per crawled page (status, redirect, block reason), per link and image check, per
  recheck, and a start/finish line with the time taken for each stage after the crawl. The app shows the newest 300 lines
  and what it is doing now, with a progress bar while busy links are rechecked.
- Rechecking links stops early when a site keeps answering 'too many requests' (10 in a row): the rest are listed as
  'Couldn't verify' instead of keeping the audit waiting.

## 0.4.0

- Nine advanced checks, inside the existing 11 sheets (no new tabs):
  - URL parameters: tracking parameters in internal links, duplicate parameter variants, parameter URLs in the sitemap, many
    variants of one path. Private-looking values (sessions, tokens, emails) are hidden in the report.
  - Canonical clusters: a filterable table on Technical SEO, with declared and resolved canonical, content similarity and conflict type.
  - Main content word count: total, main-content and boilerplate words, extraction method and confidence; the review threshold
    depends on the page type (contact, gallery, legal and utility pages aren't judged).
  - Indexability conflicts with evidence: noindex or canonicalised URLs in the sitemap, sitemap URLs blocked by robots.txt,
    canonical to a broken/redirecting/noindex page, several canonicals, noindex plus canonical, JavaScript removing noindex.
  - JavaScript comparison (installed version): title, description, H1, canonical, robots, words, schema and internal links,
    each Added/Removed/Modified/Unchanged; rendered-only, removed and changed links on Internal Links.
  - Pagination: page numbers, crawlable next links, loops, broken next links, page 2+ canonicalised to page 1.
  - URL structure: depth, length, parameters, repeated segments, case duplicates, trailing-slash consistency.
  - Internal anchor distribution: generic, empty and repetitive contextual anchors per linked page, menus and footers counted apart.
- New settings: JavaScript comparison Off / Selective / All, maximum pages to render (25–500, default 100), main-content
  threshold (100–1,000 words, default 300), URL length threshold (75–250, default 115). Also on the command line.
- Dashboard (Excel and app): parameter URLs, canonical conflicts, indexability conflicts and rendering differences.
- Fixed: the app's dashboard crashed on URL-list audits.

## 0.3.0

- New Excel report with 11 sheets: Dashboard (KPI cards, native issue-distribution and severity charts, top priorities, audit
  completeness), Action Plan (one task per fix, with Owner, Status drop-down, Target Date and Notes for you to fill in), Issue
  Details (one row per URL per rule, linked to its task), Page Audit, On-Page SEO, Technical SEO, Internal Links, Images & Speed,
  Schema & Social, Crawl Coverage and Raw & Audit Info, plus hidden raw-data sheets.
- Task priority (P0–P3) is separate from issue severity; IDs stay the same from run to run.
- 404/410 pages are reported as "Page not found", other 4xx as "Client error", each with how many pages link to it.
- Sites that don't send their intermediate SSL certificate are now audited (verified the way browsers do), and Site Health
  reports the incomplete chain. Invalid certificates are refused with a plain-language reason.
- Removed the Google Sheets help.

## 0.2.0

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
