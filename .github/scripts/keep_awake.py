"""Open a Streamlit Community Cloud app and wake it up if it has gone to sleep.

Free Streamlit apps sleep after 12 hours without visitors; a scheduled GitHub Action runs this
every few hours so the shared link always opens straight into the app.

Usage: APP_URL=https://<app>.streamlit.app python keep_awake.py
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

URL = os.environ["APP_URL"]
WAKE_BUTTON = "Yes, get this app back up!"
ASLEEP_TEXT = "has gone to sleep"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(8_000)
        button = page.get_by_role("button", name=WAKE_BUTTON)
        if button.count():
            print(f"{URL} was asleep - waking it up")
            button.click()
        else:
            print(f"{URL} is awake")
        # Wait (up to ~4 minutes) until the sleep screen is gone and the app has had time to boot.
        deadline = time.time() + 240
        while time.time() < deadline:
            page.wait_for_timeout(15_000)
            if ASLEEP_TEXT not in page.content():
                page.wait_for_timeout(20_000)  # keep the session open briefly so it counts as a visit
                print("App is up")
                browser.close()
                return 0
            page.reload(wait_until="domcontentloaded")
        print("App still shows the sleep screen after 4 minutes", file=sys.stderr)
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
