"""Start file for the free online demo on Streamlit Community Cloud.

Demo mode caps every crawl at 500 URLs, turns JavaScript rendering off, always respects robots.txt and hides the
authorized-access fields. To run SEO Lens with no limits, install it on your own computer: pipx install seolens
"""
import os
import runpy

os.environ.setdefault("SEOLENS_DEMO", "1")  # the crawler process inherits it, so the limits apply there too
runpy.run_module("seolens.app", run_name="__main__")
