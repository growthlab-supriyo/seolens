"""The online app's .streamlit/config.toml has the same two themes as the installed app (seolens/theme.py)."""
from pathlib import Path

from seolens.theme import CLEAN_PRO, MIDNIGHT, flags, toml


def test_config_matches_theme_module():
    cfg = (Path(__file__).resolve().parent.parent / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert toml() in cfg, "regenerate .streamlit/config.toml from seolens.theme.toml()"


def test_flags_cover_both_themes():
    f = flags()
    assert "--theme.light.primaryColor" in f and "--theme.dark.primaryColor" in f
    assert CLEAN_PRO["backgroundColor"] in f and MIDNIGHT["backgroundColor"] in f
