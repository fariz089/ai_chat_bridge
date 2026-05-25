"""
ChatGPT (chatgpt.com) login capture.

Auth cookies:
  __Secure-next-auth.session-token — main session (JWT-like, very long)
  __Secure-next-auth.callback-url  — callback URL

Login detection: URL contains /c/ or / (main chat page) after login,
plus session-token cookie present.

Chat API (after login):
  POST https://chatgpt.com/backend-api/conversation
  Headers: Authorization: Bearer <accessToken from session>
  Body: { model, messages: [{role, content}], ... }
"""
from playwright.sync_api import Page
from .base import LoginCapture


class ChatGPTCapture(LoginCapture):
    PLATFORM = "chatgpt"
    LOGIN_URL = "https://chatgpt.com/"
    DIRECT_CDP = True  # Skip login/cookies — control browser directly via CDP
    POST_LOGIN_HINT = (
        "Login ke ChatGPT di window Chromium. "
        "Sistem auto-detect setelah redirect ke halaman chat. "
        "Atau klik 'Saya Sudah Login' di GUI."
    )
    REQUIRED_COOKIES = ("__Secure-next-auth.session-token",)
    COOKIE_DOMAINS = ("chatgpt.com", "openai.com")

    def is_logged_in(self, page: Page) -> bool:
        try:
            cookies = page.context.cookies()
            names = {c["name"] for c in cookies}
            if "__Secure-next-auth.session-token" in names:
                url = page.url or ""
                # After login, URL is chatgpt.com/ or chatgpt.com/c/...
                if "/auth" not in url and "login" not in url.lower():
                    return True
        except:
            pass
        return False
