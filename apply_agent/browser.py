"""Run in your real browser, headed, at human pace.

Preferred: start your everyday Chrome with remote debugging and attach to it, so the agent
uses your real profile, fingerprint, GPU, time zone and cookies:

    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --remote-debugging-port=9222 --user-data-dir="$HOME/.apply-agent-chrome"
    # then: apply-agent apply <url> --cdp http://localhost:9222

(Recent Chrome refuses remote debugging on the *default* profile directory, so point
--user-data-dir at a dedicated, persistent profile that you also browse with normally.)

Alternative: let Playwright launch installed Chrome headed on a persistent profile dir.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright


@contextmanager
def open_page(*, cdp_url: str | None = None, profile_dir: str | None = None,
              channel: str = "chrome", headless: bool = False,
              executable_path: str | None = None) -> Iterator[Page]:
    with sync_playwright() as pw:
        if cdp_url:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                yield page
            finally:
                page.close()  # leave your browser itself running
            return
        kwargs: dict = {"headless": headless, "viewport": None}
        if executable_path:
            kwargs["executable_path"] = executable_path
        elif channel:
            kwargs["channel"] = channel
        if profile_dir:
            context = pw.chromium.launch_persistent_context(profile_dir, **kwargs)
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = pw.chromium.launch(**kwargs)
            context = browser.new_context(viewport=None)
            page = context.new_page()
        try:
            yield page
        finally:
            context.close()
