"""
Gemini web app (gemini.google.com) login capture.

Uses the user's normal Google session — the same logged-in Gemini (incl. a
Gemini Pro/Advanced subscription) they already use in the browser. Auth is a
standard Google session, so we drive the live browser via CDP (like ChatGPT /
AI Studio) instead of juggling cookies ourselves.

Login detection: we're on a gemini.google.com URL, NOT parked on a Google
sign-in page, and the "Ask Gemini" prompt box is present.
"""
from playwright.sync_api import Page
from .base import LoginCapture


class GeminiCapture(LoginCapture):
    PLATFORM = "gemini"
    LOGIN_URL = "https://gemini.google.com/app"
    DIRECT_CDP = True  # use the live Chrome session, like ChatGPT
    POST_LOGIN_HINT = (
        "Login ke Gemini (akun Google-mu) di window Chromium/Chrome, lalu buka "
        "gemini.google.com. Sistem auto-detect saat kotak 'Ask Gemini' muncul. "
        "Atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ()  # rely on direct CDP + UI presence, not cookies
    COOKIE_DOMAINS = ("gemini.google.com", "google.com")

    def is_logged_in(self, page: Page) -> bool:
        try:
            url = (page.url or "").lower()
            if "gemini.google.com" not in url:
                return False
            # Not parked on a Google sign-in / consent page.
            if "accounts.google.com" in url or "signin" in url:
                return False
            # Prompt box present?
            ui = page.query_selector(
                'div[contenteditable="true"], rich-textarea, textarea')
            return ui is not None
        except Exception:
            return False
