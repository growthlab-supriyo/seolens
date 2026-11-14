"""The two app themes: Clean Pro (light) and Midnight (dark).

Streamlit offers one light and one dark custom theme; viewers switch with the sidebar switch (app.py), and a first visit
follows the device's light/dark setting. The same values are in .streamlit/config.toml for the online version
(tests/test_theme.py checks that the two agree); the installed app passes them as command-line options (cli.py).
"""

INTER = "Inter:https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
GROTESK = "Space Grotesk:https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap"
MONO = "JetBrains Mono:https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap"

# Shared by both themes
BASE = {
    "theme.font": INTER,
    "theme.baseRadius": "0.5rem",
    "theme.buttonRadius": "0.5rem",
    "theme.showWidgetBorder": True,
    "theme.showSidebarBorder": True,
}

CLEAN_PRO = {
    "primaryColor": "#3b46f1",
    "backgroundColor": "#ffffff",
    "secondaryBackgroundColor": "#f6f7f9",
    "textColor": "#111318",
    "borderColor": "#e5e7eb",
    "linkColor": "#3b46f1",
    "codeBackgroundColor": "#f6f7f9",
    "dataframeHeaderBackgroundColor": "#fafafb",
    "redColor": "#dc2626",
    "orangeColor": "#ea580c",
    "yellowColor": "#d97706",
    "blueColor": "#2563eb",
    "greenColor": "#16a34a",
    "violetColor": "#3b46f1",
    "headingFont": INTER,
    "chartCategoricalColors": ["#3b46f1", "#16a34a", "#d97706", "#dc2626", "#0891b2", "#9333ea", "#64748b"],
}

MIDNIGHT = {
    "primaryColor": "#22d3ee",
    "backgroundColor": "#0b1020",
    "secondaryBackgroundColor": "#121a33",
    "textColor": "#e8ecff",
    "borderColor": "#223058",
    "linkColor": "#7dd3fc",
    "codeBackgroundColor": "#0f1730",
    "dataframeHeaderBackgroundColor": "#0f1730",
    "redColor": "#ff4d6d",
    "orangeColor": "#ff9f43",
    "yellowColor": "#facc15",
    "blueColor": "#60a5fa",
    "greenColor": "#34d399",
    "violetColor": "#a855f7",
    "headingFont": GROTESK,
    "codeFont": MONO,
    "chartCategoricalColors": ["#22d3ee", "#a855f7", "#facc15", "#ff4d6d", "#34d399", "#ff9f43", "#8c96bd"],
}

# Severity colours for charts and badges (same meaning in both themes)
SEVERITY = {
    "light": {"Critical": "#dc2626", "High": "#ea580c", "Medium": "#d97706", "Low": "#2563eb"},
    "dark": {"Critical": "#ff4d6d", "High": "#ff9f43", "Medium": "#facc15", "Low": "#60a5fa"},
}


def options():
    """Every theme option as a flat {"theme.x": value, "theme.light.y": value, "theme.dark.y": value}."""
    out = dict(BASE)
    out.update({"theme.light." + k: v for k, v in CLEAN_PRO.items()})
    out.update({"theme.dark." + k: v for k, v in MIDNIGHT.items()})
    return out


def flags():
    """Command-line options for `streamlit run` (the installed app has no config.toml of its own)."""
    out = []
    for k, v in options().items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, list):
            v = "[" + ",".join('"%s"' % x for x in v) + "]"
        out += ["--" + k, str(v)]
    return out


def toml():
    """The [theme] sections of .streamlit/config.toml."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join('"%s"' % x for x in v) + "]"
        return '"%s"' % v
    lines = ["[theme]"] + ["%s = %s" % (k.split(".", 1)[1], val(v)) for k, v in BASE.items()]
    for name, t in (("light", CLEAN_PRO), ("dark", MIDNIGHT)):
        lines += ["", "[theme.%s]" % name] + ["%s = %s" % (k, val(v)) for k, v in t.items()]
    return "\n".join(lines) + "\n"
