"""
Grok (grok.com) login capture.

Grok uses X/Twitter SSO. Auth cookies live on grok.com domain after
the OAuth redirect completes.

Key cookies:
  sso              — SSO session token
  sso-rw           — SSO read-write token (sometimes present)
  _ga / _gid       — analytics (not required but present)

Login detection: URL on grok.com without /login or /sso path,
plus sso cookie present.

Chat API (after login):
  POST https://grok.com/rest/app-chat/conversations/new
  Body: { message, modelSlug, ... }
  Uses cookies for auth (no Bearer token needed).
"""
from playwright.sync_api import Page
from .base import LoginCapture


class GrokCapture(LoginCapture):
    PLATFORM = "grok"
    LOGIN_URL = "https://grok.com/"
    POST_LOGIN_HINT = (
        "Login ke Grok (pakai akun X/Twitter) di window Chromium. "
        "Sistem auto-detect setelah redirect ke halaman chat. "
        "Atau klik 'Saya Sudah Login' di GUI."
    )
    # Grok uses X SSO; after redirect the sso cookie is set
    REQUIRED_COOKIES = ("sso",)
    COOKIE_DOMAINS = ("grok.com", "x.com", "twitter.com")

    def is_logged_in(self, page: Page) -> bool:
        try:
            cookies = page.context.cookies()
            names = {c["name"] for c in cookies}
            if "sso" in names:
                url = page.url or ""
                if "login" not in url.lower() and "/sso" not in url:
                    return True
        except:
            pass

        # Fallback: check if we're on grok.com main page with chat UI
        try:
            url = page.url or ""
            if "grok.com" in url and "/login" not in url:
                # Check for chat textarea as indicator
                textarea = page.query_selector('textarea, [contenteditable="true"]')
                if textarea:
                    return True
        except:
            pass

        return False
