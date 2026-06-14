"""
Google AI Studio applet (ai.studio / aistudio.google.com) login capture.

This points at a specific published applet (the FakeFluencer generator).
Auth is a normal Google session, so we control the browser directly via
CDP (same approach as ChatGPT) rather than juggling cookies ourselves.

Login detection: we're on an ai.studio / aistudio.google.com URL and the
applet UI (a prompt textarea / contenteditable) is present.
"""
from playwright.sync_api import Page
from .base import LoginCapture


class AIStudioCapture(LoginCapture):
    PLATFORM = "aistudio"
    LOGIN_URL = ("https://ai.studio/apps/322b7da4-0861-4ffc-83e1-6e62054bdba1"
                 "?fullscreenApplet=true")
    DIRECT_CDP = True  # Use the live browser session via CDP, like ChatGPT
    POST_LOGIN_HINT = (
        "Login ke Google / AI Studio di window Chromium, lalu buka applet "
        "FakeFluencer. Sistem auto-detect saat UI applet muncul. "
        "Atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ()  # rely on direct CDP + UI presence, not cookies
    COOKIE_DOMAINS = ("ai.studio", "aistudio.google.com", "google.com")

    def is_logged_in(self, page: Page) -> bool:
        try:
            url = (page.url or "").lower()
            on_site = ("ai.studio" in url) or ("aistudio.google.com" in url)
            if not on_site:
                return False
            # Not parked on a Google sign-in page
            if "accounts.google.com" in url or "signin" in url:
                return False
            # Applet UI present?
            ui = page.query_selector(
                'textarea, div[contenteditable="true"], '
                'input[type="text"][placeholder]')
            return ui is not None
        except Exception:
            return False
