"""Issue categories (themes), shared by the dashboard and the Excel report. No heavy imports: the report must not need pandas."""


THEMES = {
    "Crawlability & indexing": ["Page fetch failed", "Page not returning 200", "Page is noindex", "Redirect chain", "URL redirects",
                                "Bot-check page", "Search results page is indexable", "isn't indexable", "canonical", "Canonical",
                                "parameter", "Parameter", "Paginated"],
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
