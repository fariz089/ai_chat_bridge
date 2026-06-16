"""
Chat Engine — use captured sessions to chat with ChatGPT / Grok.

Uses Playwright browser automation with saved cookies to interact with
AI chat platforms.

v1.2 changes:
  - Session persistence: don't re-navigate on every send.
  - start_new_chat(): explicit reset.
  - Dedup downloaded media by URL.
  - Faster completion detection via Stop-button signal.
  - Better text extraction (read from whole bubble).

v1.2.1 — BridgeWorker:
  - Playwright's sync API is thread-bound. Calling pool.chat() from a
    different thread than the one that created the pool crashes with:
        greenlet.error: cannot switch to a different thread
  - BridgeWorker owns ChatEnginePool inside a dedicated long-lived
    thread. All ops are submitted through a queue so they always run
    on the same thread. Both the tkinter GUI (per-send worker threads)
    and the Flask API (per-request threads) call into it safely.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, BrowserContext

from platforms.base import CHROME_ARGS, DESKTOP_UA, _STEALTH_INIT_JS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Media classification
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".md", ".csv", ".json"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}


def classify_attachment(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS: return "image"
    if ext in VIDEO_EXTS: return "video"
    if ext in AUDIO_EXTS: return "audio"
    if ext in DOC_EXTS: return "document"
    return "other"


def _hash_url(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------


class ChatSession:
    """Manages a browser session for chatting with an AI platform."""

    def __init__(self, platform: str, session_path: Path, headless: bool = True,
                 media_dir: Optional[Path] = None, cdp_url: Optional[str] = None):
        self.platform = platform
        self.session_path = session_path
        self.headless = headless
        self.media_dir = media_dir or (session_path.parent.parent / "media")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.cdp_url = cdp_url   # e.g. "http://127.0.0.1:9222"
        self._pw = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._ready = False

        # v1.2 — track conversation state so we don't navigate on every send
        self._has_active_chat = False
        # How many messages have been sent in the current chat session
        self.message_count = 0

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    _MAX_COOKIE_VALUE = 3800  # CDP rejects cookie values > ~4096 bytes

    def _sanitize_storage_state(self) -> str:
        """Sanitize cookies for Playwright/CDP compatibility.

        Handles three known issues:
        1. Missing ``expires`` field (must be -1 for session cookies).
        2. ``__Host-`` prefixed cookies (CDP rejects ``domain``; drop it).
        3. Oversized cookie values (CDP limit ~4096; split into next-auth
           compatible ``.0``, ``.1`` chunks).
        """
        valid = {"Strict", "Lax", "None"}
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[{self.platform}] couldn't parse session, using as-is: {e}")
            return str(self.session_path)

        changed = 0
        final_cookies = []
        for c in raw.get("cookies", []) or []:
            ss = c.get("sameSite")
            if ss not in valid:
                low = (str(ss) if ss is not None else "").lower()
                if low in ("no_restriction", "none"): new = "None"
                elif low in ("strict",): new = "Strict"
                else: new = "Lax"
                if new != ss:
                    c["sameSite"] = new
                    changed += 1
            if "expires" in c and not isinstance(c["expires"], (int, float)):
                c.pop("expires", None)
                changed += 1
            # Playwright requires "expires" on every cookie; -1 = session cookie
            if "expires" not in c:
                c["expires"] = -1
                changed += 1
            # __Host- cookies: CDP/Playwright storage_state rejects these
            # regardless of format (domain or url).  They are CSRF tokens
            # that get regenerated server-side on page load, so safe to skip.
            name = c.get("name", "")
            if name.startswith("__Host-"):
                changed += 1
                continue
            # Oversized cookie values: CDP setCookies hard-rejects values
            # exceeding ~4096 bytes.  Split into numbered chunks (.0, .1, …)
            # which next-auth and similar frameworks reassemble automatically.
            value = c.get("value", "")
            if len(value) > self._MAX_COOKIE_VALUE:
                i = 0
                while value:
                    chunk = dict(c)
                    chunk["name"] = f"{name}.{i}"
                    chunk["value"] = value[:self._MAX_COOKIE_VALUE]
                    value = value[self._MAX_COOKIE_VALUE:]
                    final_cookies.append(chunk)
                    i += 1
                changed += 1
                logger.info(f"[{self.platform}] Split oversized cookie '{name}' into {i} chunks")
            else:
                final_cookies.append(c)

        raw["cookies"] = final_cookies

        if changed == 0:
            return str(self.session_path)
        sanitized = self.session_path.with_name(self.session_path.stem + ".sanitized.json")
        sanitized.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[{self.platform}] Sanitized {changed} cookie field(s) → {sanitized.name}")
        return str(sanitized)

    def start(self):
        """Launch browser and load session.

        If ``cdp_url`` was given (e.g. ``http://127.0.0.1:9222``), connect to
        an already-running Chrome instance instead of launching a new one.
        This avoids Cloudflare detection because the browser is a real Chrome
        with a real profile, not Playwright's bundled Chromium.
        """
        self._pw = sync_playwright().start()

        if self.cdp_url:
            # ── Connect to real Chrome via CDP ───────────────────────
            logger.info(f"[{self.platform}] Connecting to Chrome via CDP: {self.cdp_url}")
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            # Use the default (first) context that Chrome already has
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = self._browser.new_context()

            # Find or create a page with the right platform URL
            home = self._home_url()
            target_page = None
            for p in self._context.pages:
                if home and home.rstrip("/") in p.url:
                    target_page = p
                    break

            if target_page:
                self._page = target_page
                logger.info(f"[{self.platform}] Reusing existing tab: {target_page.url}")
            else:
                self._page = self._context.new_page()
                self._goto_home()

            self._ready = True
            logger.info(f"[{self.platform}] Chat session ready (CDP mode)")
            return

        # ── Standard mode: launch Playwright Chromium ────────────
        if not self.session_path.exists():
            raise FileNotFoundError(f"Session file not found: {self.session_path}")

        self._browser = self._pw.chromium.launch(
            headless=self.headless, args=CHROME_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        state_path = self._sanitize_storage_state()
        self._context = self._browser.new_context(
            user_agent=DESKTOP_UA, viewport={"width": 1366, "height": 900},
            locale="en-US", timezone_id="Asia/Jakarta",
            storage_state=state_path, accept_downloads=True,
        )
        try: self._context.add_init_script(_STEALTH_INIT_JS)
        except: pass

        self._page = self._context.new_page()
        self._goto_home()
        self._ready = True
        logger.info(f"[{self.platform}] Chat session ready")

    def _home_url(self) -> str:
        return {
            "chatgpt": "https://chatgpt.com/",
            "grok": "https://grok.com/",
            "gemini": "https://gemini.google.com/app",
            "aistudio": "https://ai.studio/apps/322b7da4-0861-4ffc-83e1-6e62054bdba1?fullscreenApplet=true",
        }.get(self.platform, "")

    def _goto_home(self):
        """Navigate to the platform's chat home (starts a fresh conversation)."""
        url = self._home_url()
        if not url:
            raise ValueError(f"Unknown platform: {self.platform}")
        logger.info(f"[{self.platform}] Navigating to {url}")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"[{self.platform}] Navigation warning: {e}")
        # Handle Cloudflare Turnstile challenge if present
        self._wait_for_cloudflare()

    def start_new_chat(self):
        """Reset to a fresh conversation by navigating to platform home."""
        if not self._ready:
            raise RuntimeError("Session not started")
        logger.info(f"[{self.platform}] Starting NEW chat (was at: {self.current_url()})")
        self._goto_home()
        self._has_active_chat = False
        self.message_count = 0

    def navigate_to_chat(self, url: str) -> list[dict]:
        """Navigate the browser to a specific chat URL and scrape existing messages.

        Returns a list of {"role": "user"|"assistant", "text": str} dicts.
        """
        if not self._ready:
            raise RuntimeError("Session not started")
        logger.info(f"[{self.platform}] Navigating to chat: {url}")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(2500)
        except Exception as e:
            logger.warning(f"[{self.platform}] Navigation warning: {e}")
        self._has_active_chat = True

        # ── DEBUG: log first few class names around user bubbles ──────────
        if self.platform == "grok":
            try:
                debug_info = self._page.evaluate("""() => {
                    // Find elements that are siblings/neighbors of .response-content-markdown
                    // and log their class names so we can identify user bubble classes
                    const aEl = document.querySelector('.response-content-markdown');
                    if (!aEl) return 'no .response-content-markdown found';

                    // Walk up 6 levels and dump class names + siblings
                    const info = [];
                    let el = aEl;
                    for (let i = 0; i < 8; i++) {
                        const p = el.parentElement;
                        if (!p) break;
                        const siblings = Array.from(p.children).map(c =>
                            c.className.toString().substring(0, 80)
                        );
                        info.push('level ' + i + ': parent.class=' +
                            p.className.toString().substring(0, 60) +
                            ' | siblings: ' + siblings.join(' // '));
                        el = p;
                    }
                    return info.join('\\n');
                }""")
                logger.info(f"[grok] DOM debug:\n{debug_info}")
            except Exception as e:
                logger.warning(f"[grok] DOM debug failed: {e}")

        messages = []
        try:
            if self.platform == "grok":
                messages = self._scrape_grok_history()
            elif self.platform == "chatgpt":
                messages = self._scrape_chatgpt_history()
        except Exception as e:
            logger.warning(f"[{self.platform}] History scrape failed: {e}")
        return messages

    def _scrape_grok_history(self) -> list[dict]:
        """Scrape all messages from the current Grok chat page.

        Uses layout position to discriminate roles:
        - User messages are right-aligned (centerX > 60% of viewport width)
        - Assistant messages are left-aligned / centered (contain .response-content-markdown)

        This is layout-based so it works regardless of Grok's class names.
        """
        page = self._page
        messages = []
        try:
            page.wait_for_timeout(1000)

            result = page.evaluate("""() => {
                const vw = window.innerWidth;
                const results = [];
                const seenTexts = new Set();

                // ── Collect ALL text-bearing leaf containers ──────────────────
                // Walk every element, skip invisible, skip tiny, collect text nodes
                // that are clearly "message bubbles" (have meaningful text, not
                // navigation/buttons/suggestions).

                // ── Strategy A: use .response-content-markdown as anchor for ASSISTANT
                //    and find user bubbles by right-alignment ──────────────────

                // 1. Gather all assistant blocks
                const aBlocks = Array.from(
                    document.querySelectorAll('.response-content-markdown')
                ).map(el => {
                    const r = el.getBoundingClientRect();
                    return {
                        role: 'assistant',
                        text: (el.innerText || '').trim(),
                        top: r.top + window.scrollY,
                        el: el
                    };
                }).filter(m => m.text.length > 0);

                // 2. Gather user bubbles:
                //    In Grok, user input is displayed in a rounded bubble that is
                //    RIGHT-aligned. We find ALL text-containing divs/spans whose
                //    bounding box center is in the right 45% of the viewport AND
                //    that do NOT contain a .response-content-markdown inside them.
                //    We also skip elements that are children of .response-content-markdown.

                const uBlocks = [];
                const allDivs = Array.from(document.querySelectorAll('div, p, span'));
                allDivs.forEach(el => {
                    // Skip if inside a .response-content-markdown
                    if (el.closest('.response-content-markdown')) return;
                    // Skip if it contains a .response-content-markdown
                    if (el.querySelector('.response-content-markdown')) return;
                    // Skip navigation, buttons, sidebar elements
                    if (el.closest('nav, aside, header, footer, button, [role="navigation"]')) return;
                    // Skip suggestion chips (usually short, appear below assistant msg)
                    // They are typically in a flex row together
                    if (el.closest('[class*="suggest"], [class*="follow"], [class*="chip"]')) return;

                    const text = (el.innerText || '').trim();
                    if (!text || text.length < 3) return;

                    // Must be a "leaf-ish" node — not have too many child divs
                    const childDivs = el.querySelectorAll('div').length;
                    if (childDivs > 8) return;

                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10) return;

                    // Check if right-aligned: center X > 60% of viewport
                    const centerX = r.left + r.width / 2;
                    if (centerX < vw * 0.55) return;

                    uBlocks.push({
                        role: 'user',
                        text: text,
                        top: r.top + window.scrollY,
                        el: el
                    });
                });

                // 3. Deduplicate user blocks by text (keep shallowest/earliest)
                const seenUser = new Set();
                const uUnique = [];
                // Sort by DOM depth (shallower = better) then by top
                uBlocks.sort((a, b) => {
                    const depthA = a.el.querySelectorAll('*').length;
                    const depthB = b.el.querySelectorAll('*').length;
                    if (depthA !== depthB) return depthA - depthB;
                    return a.top - b.top;
                });
                uBlocks.forEach(m => {
                    const key = m.text.substring(0, 100);
                    if (!seenUser.has(key)) {
                        seenUser.add(key);
                        uUnique.push(m);
                    }
                });

                // 4. Merge and sort by vertical position
                const all = [...aBlocks, ...uUnique];
                all.sort((a, b) => a.top - b.top);

                // 5. Final dedup pass (in case a user text appears inside asst text)
                const finalSeen = new Set();
                const final = [];
                all.forEach(m => {
                    const key = m.role + '|' + m.text.substring(0, 100);
                    if (!finalSeen.has(key)) {
                        finalSeen.add(key);
                        final.push({role: m.role, text: m.text});
                    }
                });

                return final;
            }""")

            if result:
                messages = result
                logger.info(f"[grok] scraped {len(messages)} messages "
                            f"(layout-based: "
                            f"{sum(1 for m in messages if m['role']=='user')} user, "
                            f"{sum(1 for m in messages if m['role']=='assistant')} assistant)")
                return messages

        except Exception as e:
            logger.warning(f"[grok] history JS eval failed: {e}")

        # ── Fallback: assistant-only ──────────────────────────────────────
        try:
            handles = page.query_selector_all('.response-content-markdown')
            for h in handles:
                text = (h.inner_text() or "").strip()
                if text:
                    messages.append({"role": "assistant", "text": text})
            logger.info(f"[grok] scraped {len(messages)} messages (fallback — assistant only)")
        except Exception as e:
            logger.warning(f"[grok] fallback scrape failed: {e}")

        return messages


    def _scrape_chatgpt_history(self) -> list[dict]:
        """Scrape all messages from the current ChatGPT chat page."""
        page = self._page
        messages = []
        try:
            page.wait_for_timeout(1000)
            result = page.evaluate("""() => {
                const msgs = [];
                // User turns
                document.querySelectorAll('[data-message-author-role="user"]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t) msgs.push({role: 'user', text: t,
                        top: el.getBoundingClientRect().top + window.scrollY});
                });
                // Assistant turns
                document.querySelectorAll('[data-message-author-role="assistant"]').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t) msgs.push({role: 'assistant', text: t,
                        top: el.getBoundingClientRect().top + window.scrollY});
                });
                msgs.sort((a, b) => a.top - b.top);
                return msgs.map(m => ({role: m.role, text: m.text}));
            }""")
            if result:
                messages = result
        except Exception as e:
            logger.warning(f"[chatgpt] history JS eval failed: {e}")
        logger.info(f"[chatgpt] scraped {len(messages)} messages from history")
        return messages

    def get_access_token(self) -> str:
        """Extract the access token from the page (ChatGPT: /api/auth/session)."""
        if self.platform == "chatgpt":
            try:
                result = self._page.evaluate("""async () => {
                    const r = await fetch('/api/auth/session');
                    const d = await r.json();
                    return d.accessToken || '';
                }""")
                return result or ""
            except Exception as e:
                logger.warning(f"[chatgpt] get_access_token failed: {e}")
        return ""

    def _wait_for_cloudflare(self, max_wait: int = 30):
        """Wait for Cloudflare Turnstile challenge to resolve (if present).

        Detects the challenge by looking for Turnstile iframes or the
        'Verifying...' text, then polls until the page navigates past it.
        In non-headless mode the user can click if needed; in headless it
        usually auto-resolves within a few seconds.
        """
        page = self._page
        try:
            # Quick check — is there a Cloudflare challenge on the page?
            cf_indicators = [
                'iframe[src*="challenges.cloudflare.com"]',
                'text="Verify you are human"',
                'text="Verifying"',
                '#challenge-running',
                '#challenge-stage',
            ]
            found = False
            for sel in cf_indicators:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        found = True
                        break
                except Exception:
                    continue

            if not found:
                return  # No challenge detected

            logger.info(f"[{self.platform}] Cloudflare challenge detected, waiting up to {max_wait}s…")

            # Try to click the Turnstile checkbox inside the iframe
            try:
                cf_frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                checkbox = cf_frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage')
                if checkbox.count() > 0:
                    checkbox.first.click(timeout=3000)
                    logger.info(f"[{self.platform}] Clicked Turnstile checkbox")
            except Exception:
                pass  # Auto-resolve or user will click

            # Wait for challenge to disappear
            deadline = time.time() + max_wait
            while time.time() < deadline:
                page.wait_for_timeout(2000)
                still_there = False
                for sel in cf_indicators:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            still_there = True
                            break
                    except Exception:
                        continue
                if not still_there:
                    logger.info(f"[{self.platform}] Cloudflare challenge cleared")
                    page.wait_for_timeout(2000)  # let page finish loading
                    return
            logger.warning(f"[{self.platform}] Cloudflare challenge still present after {max_wait}s")
        except Exception as e:
            logger.warning(f"[{self.platform}] Cloudflare wait error: {e}")

    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception:
            return ""

    def has_active_chat(self) -> bool:
        return self._has_active_chat

    # ------------------------------------------------------------------
    # Typing helpers
    # ------------------------------------------------------------------

    def _type_multiline(self, page, text: str, delay: int = 10) -> None:
        """Type text into the focused input WITHOUT submitting on newlines.

        On Grok / ChatGPT / AI Studio the prompt box submits when a bare
        Enter is pressed.  page.keyboard.type() turns every '\\n' in the
        text into a real Enter, which sends the message line-by-line and
        produces the "partial send" / "Request was interrupted" bug.

        Fix: type each line of text normally, and between lines insert a
        newline with Shift+Enter (a soft line break that does NOT submit).
        Carriage returns are stripped so '\\r\\n' doesn't double up.
        """
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.type(line, delay=delay)
            if i < len(lines) - 1:
                # Soft newline inside the textbox; does not send the message.
                page.keyboard.press("Shift+Enter")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, message: str, timeout: int = 120,
                     attachments: Optional[list[Path]] = None,
                     force_new_chat: bool = False,
                     imagine_opts: Optional[dict] = None) -> dict:
        """
        Send a message and get the AI response.

        Args:
            message: Text prompt to send.
            timeout: How long to wait for the response (seconds).
            attachments: Optional list of file paths to attach.
            force_new_chat: If True, navigate to home first to start a fresh chat.
            imagine_opts: If provided, use Grok Imagine mode instead of chat.
                          Keys: mode ("image"/"video"), resolution ("480p"/"720p"),
                          duration ("6s"/"10s"), aspect ("2:3"/"3:2"/"1:1"/"9:16"/"16:9")

        Returns: {
            "ok": bool, "response": str, "platform": str, "error": str|None,
            "elapsed_ms": int, "media": [...], "attachments_sent": [str],
            "chat_url": str, "is_new_chat": bool,
        }
        """
        if not self._ready:
            return {"ok": False, "response": "", "platform": self.platform,
                    "error": "Session not started", "elapsed_ms": 0,
                    "media": [], "attachments_sent": [],
                    "chat_url": "", "is_new_chat": False}

        attachments = attachments or []
        resolved: list[Path] = []
        for p in attachments:
            pp = Path(p)
            if not pp.exists():
                return {"ok": False, "response": "", "platform": self.platform,
                        "error": f"Attachment not found: {pp}", "elapsed_ms": 0,
                        "media": [], "attachments_sent": [],
                        "chat_url": "", "is_new_chat": False}
            resolved.append(pp)

        # Decide whether this is a new chat. We start fresh if:
        #   - caller forced it, OR
        #   - we don't yet have an active chat in this session
        is_new_chat = force_new_chat or not self._has_active_chat
        # For a forced new chat on a *text* request, always navigate to a
        # clean conversation — even if we think there's no active chat. The
        # browser may already be sitting on an old chat URL (e.g. reused
        # session), which previously caused replies to append to the wrong
        # conversation. Imagine mode handles its own navigation to /imagine,
        # so skip the home reset there.
        if force_new_chat and not (imagine_opts and self.platform == "grok"):
            try:
                self.start_new_chat()
            except Exception as e:
                logger.warning(f"[{self.platform}] start_new_chat failed: {e}")

        start = time.time()
        try:
            if imagine_opts and self.platform == "grok":
                result = self._imagine_grok(message, timeout, imagine_opts, resolved)
            elif self.platform == "chatgpt":
                result = self._chat_chatgpt(message, timeout, resolved)
            elif self.platform == "aistudio":
                result = self._chat_aistudio(message, timeout, resolved)
            elif self.platform == "gemini":
                result = self._chat_gemini(message, timeout, resolved)
            elif self.platform == "grok":
                result = self._chat_grok(message, timeout, resolved)
            else:
                return {"ok": False, "response": "",
                        "platform": self.platform,
                        "error": f"Unknown platform: {self.platform}",
                        "elapsed_ms": 0,
                        "media": [], "attachments_sent": [],
                        "chat_url": "", "is_new_chat": is_new_chat}

            # Mark that we now have an active chat
            self._has_active_chat = True
            self.message_count += 1
            elapsed = int((time.time() - start) * 1000)
            return {
                "ok": True, "response": result["text"],
                "platform": self.platform, "error": None,
                "elapsed_ms": elapsed,
                "media": result.get("media", []),
                "attachments_sent": [str(p) for p in resolved],
                "chat_url": self.current_url(),
                "is_new_chat": is_new_chat,
            }
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.exception(f"[{self.platform}] Chat failed")
            debug_paths = self._save_debug_artifacts(tag="chat_error")
            return {"ok": False, "response": "",
                    "platform": self.platform, "error": str(e),
                    "elapsed_ms": elapsed,
                    "media": [], "attachments_sent": [str(p) for p in resolved],
                    "chat_url": self.current_url(),
                    "is_new_chat": is_new_chat,
                    "debug": debug_paths}

    # ------------------------------------------------------------------
    # Attachment upload (shared)
    # ------------------------------------------------------------------

    def _attach_files(self, file_paths: list[Path]) -> bool:
        if not file_paths:
            return True
        page = self._page

        try:
            for attach_label in ["Attach files", "Attach", "Upload", "Add photos & files", "Add files"]:
                btn = page.query_selector(f'button[aria-label*="{attach_label}" i]')
                if btn:
                    try:
                        btn.click(timeout=2000)
                        page.wait_for_timeout(300)
                        for opt in ["Upload from computer", "Upload file", "From computer", "From device"]:
                            o = page.query_selector(f'text="{opt}"')
                            if o and o.is_visible():
                                page.keyboard.press("Escape")
                                break
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        selectors = ['input[type="file"][multiple]', 'input[type="file"]']
        input_el = None
        for sel in selectors:
            try:
                els = page.query_selector_all(sel)
                for el in els:
                    if el:
                        input_el = el
                        break
                if input_el:
                    break
            except Exception:
                continue

        if not input_el:
            raise RuntimeError("Couldn't find file <input> on the page.")

        try:
            input_el.set_input_files([str(p) for p in file_paths])
            logger.info(f"[{self.platform}] Attached {len(file_paths)} file(s)")
            page.wait_for_timeout(2000)
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    spinner = page.query_selector(
                        '[role="progressbar"], .uploading, [data-testid*="upload"][data-state="loading"]'
                    )
                    if not spinner:
                        break
                except Exception:
                    break
                page.wait_for_timeout(500)
            return True
        except Exception as e:
            raise RuntimeError(f"set_input_files failed: {e}")

    # ------------------------------------------------------------------
    # Media extraction (shared) — with URL dedup
    # ------------------------------------------------------------------

    def _extract_media_from_element(self, element_handle) -> list[dict]:
        """
        Find <img> and <video> tags inside element_handle, download each
        UNIQUE src once, return metadata.
        """
        media: list[dict] = []
        seen_urls: set[str] = set()

        # ---- Images ----
        try:
            imgs = element_handle.query_selector_all("img")
        except Exception:
            imgs = []
        for img in imgs:
            try:
                src = img.get_attribute("src") or ""
                alt = img.get_attribute("alt") or ""
                if not src or src.startswith("data:image/svg"):
                    continue
                if src in seen_urls:
                    continue
                # Skip tiny icons
                try:
                    box = img.bounding_box()
                    if box and (box.get("width", 0) < 80 or box.get("height", 0) < 80):
                        continue
                except Exception:
                    pass

                seen_urls.add(src)
                local = self._download_to_media(src, hint_ext=".png")
                if local:
                    media.append({"type": "image", "url": src,
                                  "local_path": str(local), "alt": alt or None})
            except Exception as e:
                logger.debug(f"[{self.platform}] img extract failed: {e}")

        # ---- Videos ----
        try:
            videos = element_handle.query_selector_all("video")
        except Exception:
            videos = []
        for vid in videos:
            try:
                src = vid.get_attribute("src") or ""
                if not src:
                    src_el = vid.query_selector("source")
                    if src_el:
                        src = src_el.get_attribute("src") or ""
                if not src or src in seen_urls:
                    continue
                seen_urls.add(src)
                local = self._download_to_media(src, hint_ext=".mp4")
                if local:
                    media.append({"type": "video", "url": src,
                                  "local_path": str(local), "alt": None})
            except Exception as e:
                logger.debug(f"[{self.platform}] video extract failed: {e}")

        if media:
            logger.info(f"[{self.platform}] Extracted {len(media)} unique media file(s)")
        return media

    def _download_to_media(self, url: str, hint_ext: str = "") -> Optional[Path]:
        if not url:
            return None
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            short = _hash_url(url)

            if url.startswith("data:"):
                try:
                    header, b64 = url.split(",", 1)
                    mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
                    ext = mimetypes.guess_extension(mime) or hint_ext or ".bin"
                    out = self.media_dir / f"{self.platform}_{ts}_{short}{ext}"
                    out.write_bytes(base64.b64decode(b64))
                    return out
                except Exception as e:
                    logger.debug(f"[{self.platform}] data URL decode failed: {e}")
                    return None

            if url.startswith("blob:"):
                try:
                    b64 = self._page.evaluate(
                        """async (u) => {
                            const r = await fetch(u);
                            const buf = await r.arrayBuffer();
                            let bin = '';
                            const bytes = new Uint8Array(buf);
                            for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
                            return btoa(bin);
                        }""", url,
                    )
                    ext = hint_ext or ".bin"
                    out = self.media_dir / f"{self.platform}_{ts}_{short}{ext}"
                    out.write_bytes(base64.b64decode(b64))
                    return out
                except Exception as e:
                    logger.debug(f"[{self.platform}] blob fetch failed: {e}")
                    return None

            # http(s)://
            try:
                resp = self._context.request.get(url, timeout=30_000)
                if resp.status >= 400:
                    logger.debug(f"[{self.platform}] download {resp.status} for {url[:80]}")
                    return None
                ext = hint_ext
                ct = resp.headers.get("content-type", "")
                guessed = mimetypes.guess_extension(ct.split(";")[0].strip()) if ct else None
                if guessed:
                    ext = guessed
                if not ext:
                    parsed = urlparse(url)
                    _, url_ext = os.path.splitext(parsed.path)
                    if url_ext: ext = url_ext
                if not ext: ext = ".bin"
                out = self.media_dir / f"{self.platform}_{ts}_{short}{ext}"
                out.write_bytes(resp.body())
                return out
            except Exception as e:
                logger.debug(f"[{self.platform}] http download failed: {e}")
                return None
        except Exception as e:
            logger.debug(f"[{self.platform}] _download_to_media failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Debug artifacts
    # ------------------------------------------------------------------

    def _save_debug_artifacts(self, tag: str = "error") -> dict:
        if not self._page:
            return {}
        try:
            base = Path(os.environ.get("DATA_DIR", "")) if os.environ.get("DATA_DIR") else None
            if base is None:
                base = self.session_path.parent.parent / "debug"
            base.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            stem = f"{self.platform}_{tag}_{ts}"
            html_path = base / f"{stem}.html"
            png_path = base / f"{stem}.png"

            current_url = ""
            try: current_url = self._page.url
            except: pass

            try:
                with open(html_path, "w", encoding="utf-8") as fh:
                    fh.write(f"<!-- platform: {self.platform} -->\n")
                    fh.write(f"<!-- url: {current_url} -->\n")
                    fh.write(f"<!-- timestamp: {ts} -->\n")
                    fh.write(self._page.content())
            except Exception as e:
                logger.debug(f"[{self.platform}] HTML dump failed: {e}")
                html_path = None

            try:
                self._page.screenshot(path=str(png_path), full_page=False)
            except Exception as e:
                logger.debug(f"[{self.platform}] screenshot failed: {e}")
                png_path = None

            logger.info(f"[{self.platform}] Debug artifacts saved: html={html_path}, png={png_path}")
            return {"html": str(html_path) if html_path else None,
                    "png": str(png_path) if png_path else None,
                    "url": current_url}
        except Exception as e:
            logger.debug(f"[{self.platform}] couldn't save debug artifacts: {e}")
            return {}

    # ------------------------------------------------------------------
    # ChatGPT
    # ------------------------------------------------------------------

    def _is_chatgpt_streaming(self) -> bool:
        """Return True while ChatGPT is generating a response."""
        try:
            stop = self._page.query_selector('button[aria-label*="Stop" i]')
            if stop and stop.is_visible():
                return True
        except Exception:
            pass
        return False

    def _chat_chatgpt(self, message: str, timeout: int,
                      attachments: list[Path]) -> dict:
        page = self._page
        input_sel = 'textarea[id="prompt-textarea"], div[id="prompt-textarea"]'
        page.wait_for_selector(input_sel, timeout=15_000)
        page.wait_for_timeout(800)

        if attachments:
            self._attach_files(attachments)
            logger.info(f"[chatgpt] phase=attached count={len(attachments)}")

        input_el = page.query_selector(input_sel)
        if input_el:
            input_el.click()
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(200)
            self._type_multiline(page, message, delay=10)
            page.wait_for_timeout(400)

        existing_msgs = page.query_selector_all('[data-message-author-role="assistant"]')
        existing_count = len(existing_msgs)  # kept for backward compat logging

        # Count existing assistant turns BEFORE sending — critical for detecting new ones
        turn_selector = '[data-testid^="conversation-turn-"][data-turn="assistant"]'
        existing_turns = page.query_selector_all(turn_selector)
        existing_turn_count = len(existing_turns)
        logger.info(f"[chatgpt] Baseline: {existing_turn_count} assistant turns, {existing_count} assistant msgs")

        send_btn = page.query_selector('button[data-testid="send-button"], button[aria-label*="Send"]')
        if send_btn and send_btn.is_enabled():
            send_btn.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_timeout(1500)

        # ---- Wait for response ----
        deadline = time.time() + timeout
        last_text = ""
        latest_handle = None
        text_seen = False
        text_stable_ticks = 0

        # Selectors for text content inside an assistant turn
        text_selectors = [
            '[data-message-author-role="assistant"]',
            'div.markdown',
            'div[class*="markdown"]',
        ]

        has_images = False
        latest_turn = None

        while time.time() < deadline:
            page.wait_for_timeout(1000)

            # Check if a new assistant turn appeared
            turns = page.query_selector_all(turn_selector)
            if len(turns) <= existing_turn_count:
                continue

            latest_turn = turns[-1]

            # Try to get text from the new turn
            current_text = ""
            latest_handle = None
            for sel in text_selectors:
                try:
                    text_el = latest_turn.query_selector(sel)
                    if text_el:
                        current_text = (text_el.inner_text() or "").strip()
                        if current_text:
                            latest_handle = text_el
                            break
                except Exception:
                    pass

            # If no text element found, use the turn itself as handle
            if latest_handle is None:
                latest_handle = latest_turn
                try:
                    current_text = (latest_turn.inner_text() or "").strip()
                    # Remove "ChatGPT said:" prefix
                    for prefix in ("ChatGPT said:", "ChatGPT said"):
                        if current_text.startswith(prefix):
                            current_text = current_text[len(prefix):].strip()
                except Exception:
                    current_text = ""

            if current_text:
                text_seen = True
            if current_text == last_text:
                text_stable_ticks += 1
            else:
                text_stable_ticks = 0
                last_text = current_text

            # Check for images in the turn (for image-only responses)
            has_images = False
            try:
                has_images = latest_turn.evaluate("""el => {
                    const imgs = el.querySelectorAll('img');
                    for (const img of imgs) {
                        const r = img.getBoundingClientRect();
                        if (r.width >= 80 && r.height >= 80) return true;
                    }
                    return false;
                }""")
            except Exception:
                pass

            # Fast completion signal
            is_streaming = self._is_chatgpt_streaming()
            if not is_streaming:
                if text_seen or has_images:
                    # Give one more poll to catch final tokens
                    page.wait_for_timeout(500)
                    try:
                        if latest_handle:
                            final_text = (latest_handle.inner_text() or "").strip()
                            for prefix in ("ChatGPT said:", "ChatGPT said"):
                                if final_text.startswith(prefix):
                                    final_text = final_text[len(prefix):].strip()
                            last_text = final_text or last_text
                    except Exception:
                        pass
                    break

            # Fallback: text stable for several polls AND not streaming.
            # Raised from 3 to 5 and gated on streaming-off so long JSON
            # replies are never read half-finished (old cut-off bug).
            if (text_stable_ticks >= 5 and (last_text or has_images)
                    and not self._is_chatgpt_streaming()):
                break

        # For image-only responses, set a placeholder text if empty
        if not last_text and latest_handle:
            try:
                # Try getting alt text from images as fallback
                alt_text = latest_turn.evaluate("""el => {
                    const imgs = el.querySelectorAll('img');
                    for (const img of imgs) {
                        if (img.alt && img.alt.length > 5 && !img.alt.includes('Profile'))
                            return img.alt;
                    }
                    return '';
                }""") if latest_turn else ""
                if alt_text:
                    last_text = f"[Image: {alt_text}]"
            except Exception:
                pass

        if not last_text and not has_images:
            # Save debug artifacts to diagnose what's on the page
            debug = self._save_debug_artifacts(tag="chatgpt_no_response")
            logger.error(f"[chatgpt] No response. Debug: {debug}. URL: {self.current_url()}")
            raise TimeoutError("No response received from ChatGPT within timeout")

        if not last_text:
            last_text = "[Image generated]"

        # ---- Extract media ----
        media: list[dict] = []
        if latest_handle:
            # Wait for images to render — ChatGPT image generation can take
            # 10-30s after text streaming finishes.  We poll for <img> tags
            # inside the assistant bubble that are large enough to be generated
            # content (not icons).  Give up after 60s of no new images.
            page.wait_for_timeout(800)

            # ---- DIAGNOSTIC: dump DOM structure of assistant bubble ----
            try:
                dom_info = latest_handle.evaluate("""el => {
                    const info = {
                        tagName: el.tagName,
                        className: el.className,
                        childCount: el.children.length,
                        innerHTML_length: el.innerHTML.length,
                        imgs: [],
                        all_tags: []
                    };
                    // All unique tag names
                    const tags = new Set();
                    el.querySelectorAll('*').forEach(c => tags.add(c.tagName.toLowerCase()));
                    info.all_tags = [...tags].sort();
                    // All img details
                    el.querySelectorAll('img').forEach(img => {
                        const r = img.getBoundingClientRect();
                        info.imgs.push({
                            src: (img.src || '').substring(0, 120),
                            alt: img.alt || '',
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                            naturalWidth: img.naturalWidth,
                            naturalHeight: img.naturalHeight,
                            className: img.className,
                            parentTag: img.parentElement ? img.parentElement.tagName : 'none',
                            parentClass: img.parentElement ? img.parentElement.className.substring(0, 80) : ''
                        });
                    });
                    return info;
                }""")
                logger.info(f"[chatgpt] DOM diagnostic — bubble: tag={dom_info['tagName']}, "
                            f"class={dom_info.get('className','')[:80]}, "
                            f"children={dom_info['childCount']}, "
                            f"innerHTML_len={dom_info['innerHTML_length']}, "
                            f"tags={dom_info['all_tags']}")
                for i, img in enumerate(dom_info.get('imgs', [])):
                    logger.info(f"[chatgpt] DOM img[{i}]: {img}")
                if not dom_info.get('imgs'):
                    logger.info("[chatgpt] DOM diagnostic — NO <img> tags inside assistant bubble!")
            except Exception as e:
                logger.warning(f"[chatgpt] DOM diagnostic failed: {e}")

            # ---- Also check OUTSIDE the bubble — maybe images are siblings ----
            try:
                page_imgs = page.evaluate("""() => {
                    const results = [];
                    // Check all assistant messages and their parents
                    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
                    const lastMsg = msgs[msgs.length - 1];
                    if (!lastMsg) return results;

                    // Check siblings and parent containers
                    let container = lastMsg.parentElement;
                    for (let i = 0; i < 3 && container; i++) {
                        container.querySelectorAll('img').forEach(img => {
                            const r = img.getBoundingClientRect();
                            if (r.width < 80 || r.height < 80) return;
                            results.push({
                                src: (img.src || '').substring(0, 120),
                                width: Math.round(r.width),
                                height: Math.round(r.height),
                                distFromMsg: container.tagName + '(' + i + ' levels up)',
                                className: img.className.substring(0, 60)
                            });
                        });
                        container = container.parentElement;
                    }
                    return results;
                }""")
                if page_imgs:
                    logger.info(f"[chatgpt] Found {len(page_imgs)} large img(s) near assistant bubble:")
                    for i, img in enumerate(page_imgs):
                        logger.info(f"[chatgpt]   nearby_img[{i}]: {img}")
            except Exception as e:
                logger.warning(f"[chatgpt] nearby img scan failed: {e}")

            def _count_real_imgs(handle):
                """Count non-icon images (>80px) inside the element."""
                try:
                    return handle.evaluate("""el => {
                        let count = 0;
                        el.querySelectorAll('img').forEach(img => {
                            const src = img.src || '';
                            if (!src || src.startsWith('data:image/svg')) return;
                            const r = img.getBoundingClientRect();
                            if (r.width >= 80 && r.height >= 80) count++;
                        });
                        return count;
                    }""")
                except Exception:
                    return 0

            # Check if response text hints at image generation
            text_lower = last_text.lower()
            might_have_image = any(kw in text_lower for kw in (
                "image", "gambar", "here", "berikut", "generated", "created",
                "illustration", "picture", "photo", "drawing", "ini dia",
                "silakan", "hasil", "membuat",
            ))

            # Also check if there's a loading/generating indicator
            try:
                has_img_placeholder = page.evaluate("""() => {
                    const last = document.querySelectorAll('[data-message-author-role="assistant"]');
                    if (!last.length) return false;
                    const el = last[last.length - 1];
                    // Check for image loading spinners, progress bars, or DALL-E containers
                    return !!(el.querySelector('[class*="spinner"]') ||
                              el.querySelector('[class*="loading"]') ||
                              el.querySelector('[class*="progress"]') ||
                              el.querySelector('[class*="dall"]') ||
                              el.querySelector('[class*="image-gen"]') ||
                              el.querySelector('img[src*="blob:"]') ||
                              el.querySelector('button:has-text("Edit")'));
                }""")
            except Exception:
                has_img_placeholder = False

            if might_have_image or has_img_placeholder or _count_real_imgs(latest_handle) > 0:
                logger.info(f"[chatgpt] Image likely in response — waiting for render...")
                img_deadline = time.time() + 60  # wait up to 60s for images
                last_img_count = _count_real_imgs(latest_handle)
                stable_img_ticks = 0

                while time.time() < img_deadline:
                    page.wait_for_timeout(2000)
                    current_img_count = _count_real_imgs(latest_handle)

                    if current_img_count > 0 and current_img_count == last_img_count:
                        stable_img_ticks += 1
                        if stable_img_ticks >= 2:  # stable for ~4s
                            logger.info(f"[chatgpt] {current_img_count} image(s) rendered.")
                            break
                    else:
                        stable_img_ticks = 0
                        last_img_count = current_img_count

                    # Check if streaming/generating is still active
                    if not self._is_chatgpt_streaming() and current_img_count == 0 and stable_img_ticks >= 5:
                        break  # No images coming

                # Extra wait for image to fully load
                page.wait_for_timeout(1500)

                # Re-read text in case it changed during image generation
                try:
                    last_text = (latest_handle.inner_text() or "").strip() or last_text
                except Exception:
                    pass

            # Extract media from the full turn container
            extract_from = latest_turn if latest_turn else latest_handle
            try:
                media = self._extract_media_from_element(extract_from)
                logger.info(f"[chatgpt] Media extraction result: {len(media)} item(s)")
                for i, m in enumerate(media):
                    logger.info(f"[chatgpt]   media[{i}]: type={m.get('type')}, "
                                f"url={str(m.get('url',''))[:100]}, "
                                f"local={m.get('local_path','')}")
            except Exception as e:
                logger.warning(f"[chatgpt] media extract failed: {e}")

            # If no media found, use page.evaluate to fetch image bytes directly
            # (more reliable in CDP mode where context.request may not have cookies)
            if not media and extract_from:
                try:
                    img_data_list = page.evaluate("""async (el) => {
                        const results = [];
                        const imgs = (el || document).querySelectorAll('img');
                        for (const img of imgs) {
                            const src = img.src || '';
                            if (!src || src.startsWith('data:image/svg')) continue;
                            const r = img.getBoundingClientRect();
                            if (r.width < 80 || r.height < 80) continue;
                            try {
                                const resp = await fetch(src, {credentials: 'include'});
                                if (!resp.ok) continue;
                                const buf = await resp.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let bin = '';
                                for (let i = 0; i < bytes.byteLength; i++)
                                    bin += String.fromCharCode(bytes[i]);
                                const ct = resp.headers.get('content-type') || 'image/png';
                                results.push({
                                    b64: btoa(bin),
                                    contentType: ct,
                                    alt: img.alt || '',
                                    width: Math.round(r.width),
                                    height: Math.round(r.height),
                                    src: src.substring(0, 150)
                                });
                            } catch(e) {
                                // fetch failed, try canvas approach
                                try {
                                    const canvas = document.createElement('canvas');
                                    canvas.width = img.naturalWidth || r.width;
                                    canvas.height = img.naturalHeight || r.height;
                                    const ctx = canvas.getContext('2d');
                                    ctx.drawImage(img, 0, 0);
                                    const dataUrl = canvas.toDataURL('image/png');
                                    const b64 = dataUrl.split(',')[1];
                                    if (b64 && b64.length > 1000) {
                                        results.push({
                                            b64: b64,
                                            contentType: 'image/png',
                                            alt: img.alt || '',
                                            width: canvas.width,
                                            height: canvas.height,
                                            src: src.substring(0, 150)
                                        });
                                    }
                                } catch(e2) {}
                            }
                        }
                        return results;
                    }""", extract_from)

                    for i, img_data in enumerate(img_data_list or []):
                        try:
                            import base64 as b64mod
                            raw = b64mod.b64decode(img_data["b64"])
                            ct = img_data.get("contentType", "image/png")
                            ext = ".png"
                            if "jpeg" in ct or "jpg" in ct: ext = ".jpg"
                            elif "webp" in ct: ext = ".webp"
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            out = self.media_dir / f"chatgpt_{ts}_img{i}{ext}"
                            self.media_dir.mkdir(parents=True, exist_ok=True)
                            out.write_bytes(raw)
                            media.append({
                                "type": "image", "url": img_data.get("src", ""),
                                "local_path": str(out),
                                "alt": img_data.get("alt") or None
                            })
                            logger.info(f"[chatgpt] Fetched image via browser: {out} "
                                        f"({len(raw)} bytes, {img_data.get('width')}x{img_data.get('height')})")
                        except Exception as e:
                            logger.warning(f"[chatgpt] Failed to save fetched image: {e}")

                except Exception as e:
                    logger.warning(f"[chatgpt] Browser-fetch image extraction failed: {e}")

            # Last resort: screenshot the image elements
            if not media and extract_from:
                try:
                    img_elements = extract_from.query_selector_all("img")
                    for i, img_el in enumerate(img_elements):
                        try:
                            box = img_el.bounding_box()
                            if not box or box.get("width", 0) < 80 or box.get("height", 0) < 80:
                                continue
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            out = self.media_dir / f"chatgpt_{ts}_screenshot{i}.png"
                            self.media_dir.mkdir(parents=True, exist_ok=True)
                            img_el.screenshot(path=str(out))
                            alt = img_el.get_attribute("alt") or ""
                            media.append({
                                "type": "image", "url": "(screenshot)",
                                "local_path": str(out), "alt": alt or None
                            })
                            logger.info(f"[chatgpt] Screenshot captured: {out}")
                        except Exception as e:
                            logger.debug(f"[chatgpt] img screenshot failed: {e}")
                except Exception as e:
                    logger.warning(f"[chatgpt] Screenshot fallback failed: {e}")

        return {"text": last_text, "media": media}

    # ------------------------------------------------------------------
    # AI Studio (Fakefluencer applet) — generic web-app automation
    # ------------------------------------------------------------------

    def _is_aistudio_streaming(self) -> bool:
        """Best-effort 'still generating' signal for the AI Studio applet.

        We look for a visible Stop button, a disabled Send button, or a
        spinner/loading element. If none of these are reliable on the page,
        the caller falls back to text-stability detection.
        """
        page = self._page
        try:
            for sel in ('button[aria-label*="Stop" i]',
                        'button:has-text("Stop")',
                        '[role="progressbar"]',
                        '.loading, .spinner, [aria-busy="true"]'):
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return True
        except Exception:
            pass
        return False

    def _chat_aistudio(self, message: str, timeout: int,
                       attachments: list[Path]) -> dict:
        """Drive the AI Studio applet like a human: attach (if any), type,
        send, then wait for the reply to FULLY settle before returning.

        This deliberately mirrors the robust completion logic used for
        ChatGPT so the storyboard JSON is never read half-finished.
        """
        page = self._page
        # The applet is a custom React UI. Its prompt box is a textarea or a
        # contenteditable div; try the common shapes in order.
        input_sel = ('textarea, div[contenteditable="true"], '
                     'input[type="text"][placeholder]')
        page.wait_for_selector(input_sel, timeout=20_000)
        page.wait_for_timeout(800)

        if attachments:
            try:
                self._attach_files(attachments)
                logger.info(f"[aistudio] phase=attached count={len(attachments)}")
            except Exception as e:
                logger.warning(f"[aistudio] attach failed (continuing): {e}")

        # Snapshot existing text so we can detect the NEW reply only.
        def _page_text() -> str:
            try:
                return (page.inner_text("body") or "").strip()
            except Exception:
                return ""
        before_text = _page_text()

        input_el = page.query_selector(input_sel)
        if not input_el:
            raise RuntimeError("AI Studio: prompt input not found.")
        input_el.click()
        page.wait_for_timeout(200)
        try:
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
        except Exception:
            pass
        page.wait_for_timeout(150)
        self._type_multiline(page, message, delay=8)
        page.wait_for_timeout(300)

        # Send: prefer a real Send button; fall back to Enter ONCE.
        sent = False
        for sel in ('button[aria-label*="Send" i]',
                    'button:has-text("Send")',
                    'button[type="submit"]'):
            btn = page.query_selector(sel)
            if btn and btn.is_enabled() and btn.is_visible():
                btn.click()
                sent = True
                break
        if not sent:
            page.keyboard.press("Enter")

        # ---- Wait for the reply to settle ----
        # Strategy: wait until (a) new text has appeared, AND (b) it stops
        # changing for several consecutive polls AND streaming flag is off.
        # We NEVER press Enter again here — that was the old bug that cut
        # replies short and produced broken JSON.
        page.wait_for_timeout(1500)
        deadline = time.time() + timeout
        last_text = ""
        stable_ticks = 0
        new_seen = False

        while time.time() < deadline:
            page.wait_for_timeout(1000)
            now = _page_text()
            # The "reply" is whatever got appended after our prompt.
            delta = now[len(before_text):] if now.startswith(before_text) else now
            delta = delta.strip()

            if delta and delta != before_text:
                new_seen = True

            if delta == last_text:
                stable_ticks += 1
            else:
                stable_ticks = 0
                last_text = delta

            streaming = self._is_aistudio_streaming()

            # Done when: we have new text, it's been stable a few polls,
            # and nothing indicates it's still generating.
            if new_seen and last_text and stable_ticks >= 3 and not streaming:
                break
            # Hard stable fallback even if a streaming signal never appeared.
            if new_seen and last_text and stable_ticks >= 6:
                break

        if not last_text:
            debug = self._save_debug_artifacts(tag="aistudio_no_response")
            logger.error(f"[aistudio] No response. Debug: {debug}. URL: {self.current_url()}")
            raise TimeoutError("No response received from AI Studio within timeout")

        # Extract the image the applet produced (Gemini / Nano Banana output).
        # Pick the single largest intrinsic-resolution image and download it so
        # callers get a real local_path. The old code recorded every <img> src
        # (icons/avatars included) and never downloaded, so nothing got saved.
        media: list[dict] = []
        try:
            best = page.evaluate("""() => {
                let best = null;
                document.querySelectorAll('img').forEach(img => {
                    const src = img.src || '';
                    if (!src || !src.startsWith('http')) return;
                    const r = img.getBoundingClientRect();
                    if (r.width < 200 || r.height < 200) return;
                    const score = (img.naturalWidth||0) * (img.naturalHeight||0);
                    if (!best || score > best.score) best = {src, score};
                });
                return best ? best.src : '';
            }""")
            if best:
                local = self._download_to_media(best, ".png")
                media.append({
                    "type": "image", "url": best,
                    "local_path": str(local) if local else "",
                })
        except Exception as e:
            logger.warning(f"[aistudio] media harvest failed: {e}")

        return {"text": last_text, "media": media}

    # ------------------------------------------------------------------
    # Gemini (web app — gemini.google.com, uses the user's Pro login)
    # ------------------------------------------------------------------

    def _chat_gemini(self, message: str, timeout: int,
                     attachments: list[Path]) -> dict:
        """Drive the Gemini web app like a human: attach (if any), type, send,
        wait for the reply to settle, then harvest text AND any generated image.

        This is the SAME method for both jobs:
          * script step  -> the prompt asks for JSON, no image is produced
          * image step   -> the prompt asks for a photoreal image, Gemini's
                            built-in image generation ("Nano Banana") returns
                            one, which we download.

        NOTE: gemini.google.com's DOM is not public/stable. The selectors below
        are best-effort and may need tweaking against the live UI; everything
        degrades gracefully (logs + returns what it found) instead of crashing.
        """
        page = self._page

        # Gemini's prompt box is a contenteditable rich-textarea.
        input_sel = ('div[contenteditable="true"], rich-textarea div[contenteditable="true"], '
                     'textarea')
        page.wait_for_selector(input_sel, timeout=25_000)
        page.wait_for_timeout(800)

        # Dismiss the occasional "Got it" / ToS / cookie prompts.
        for txt in ("Got it", "I agree", "Accept all", "No thanks", "Dismiss"):
            try:
                b = page.query_selector(f'button:has-text("{txt}")')
                if b and b.is_visible():
                    b.click()
                    page.wait_for_timeout(300)
            except Exception:
                pass

        if attachments:
            try:
                # Gemini uses the OS native file picker — NOT a hidden <input>.
                # Playwright's expect_file_chooser() intercepts the OS dialog
                # before it opens and feeds it our paths directly.
                #
                # Step 1: Find and click the "+" button.
                plus_btn = None
                for sel in (
                    'button[aria-label*="Upload" i]',
                    'button[aria-label*="Add" i]',
                    'button[aria-label*="More" i]',
                    'button[data-test-id*="plus" i]',
                    # Last resort: first button whose text/content looks like "+"
                    'button:has-text("+")',
                ):
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            plus_btn = el
                            break
                    except Exception:
                        continue

                if not plus_btn:
                    # Try by position: the "+" is left of the Tools button
                    plus_btn = page.evaluate_handle(
                        """() => {
                            const btns = [...document.querySelectorAll('button')];
                            return btns.find(b =>
                                (b.textContent || '').trim() === '+' ||
                                (b.getAttribute('aria-label') || '').match(/add|upload|attach|plus/i)
                            ) || null;
                        }"""
                    )

                if plus_btn:
                    plus_btn.click()
                    page.wait_for_timeout(600)
                else:
                    logger.warning("[gemini] '+' button not found — trying attach without menu")

                # Step 2: Click "Upload files" menu item.
                # Use expect_file_chooser so Playwright intercepts the OS dialog.
                upload_clicked = False
                for item_text in ("Upload files", "Upload file", "Upload", "From device", "From computer"):
                    for sel in (
                        f'[role="menuitem"]:has-text("{item_text}")',
                        f'li:has-text("{item_text}")',
                        f'button:has-text("{item_text}")',
                        f'text="{item_text}"',
                    ):
                        try:
                            item = page.query_selector(sel)
                            if item and item.is_visible():
                                # Intercept file chooser BEFORE clicking the item
                                with page.expect_file_chooser(timeout=5000) as fc_info:
                                    item.click()
                                fc = fc_info.value
                                fc.set_files([str(p) for p in attachments])
                                upload_clicked = True
                                logger.info(f"[gemini] Attached {len(attachments)} file(s) via file chooser")
                                break
                        except Exception:
                            continue
                    if upload_clicked:
                        break

                if not upload_clicked:
                    # Fallback: maybe clicking "+" directly opens file chooser (some Gemini builds)
                    logger.warning("[gemini] Upload menu item not found — trying direct file chooser on '+'")
                    if plus_btn:
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            plus_btn.click()
                        fc_info.value.set_files([str(p) for p in attachments])
                        logger.info(f"[gemini] Attached {len(attachments)} file(s) via direct '+' file chooser")

                logger.info(f"[gemini] phase=attached count={len(attachments)}")
                page.wait_for_timeout(2000)  # wait for upload progress
            except Exception as e:
                logger.warning(f"[gemini] attach failed (continuing): {e}")

        # Count existing response turns BEFORE sending to detect new reply.
        def _count_response_turns() -> int:
            try:
                return page.evaluate("""() => {
                    const els = document.querySelectorAll(
                        'model-response, message-content, [data-response-id], ' +
                        '.response-content, .model-response-text'
                    );
                    return els.length;
                }""")
            except Exception:
                return 0

        def _get_last_response_text() -> str:
            """Extract text ONLY from the last model response bubble."""
            try:
                return page.evaluate("""() => {
                    const candidates = [
                        ...document.querySelectorAll(
                            'model-response, message-content, [data-response-id]'
                        )
                    ];
                    if (candidates.length > 0) {
                        const last = candidates[candidates.length - 1];
                        return (last.innerText || last.textContent || '').trim();
                    }
                    // Fallback: last large text block in the response area
                    const blocks = [...document.querySelectorAll(
                        '.response-content, .model-response-text, ' +
                        'div[class*="response"], div[class*="message"][class*="model"]'
                    )];
                    if (blocks.length > 0) {
                        const last = blocks[blocks.length - 1];
                        return (last.innerText || last.textContent || '').trim();
                    }
                    return '';
                }""")
            except Exception:
                return ""

        turns_before = _count_response_turns()
        logger.info(f"[gemini] turns before send: {turns_before}")

        input_el = page.query_selector(input_sel)
        if not input_el:
            raise RuntimeError("Gemini: prompt input not found.")
        input_el.click()
        page.wait_for_timeout(200)
        try:
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
        except Exception:
            pass
        page.wait_for_timeout(150)
        self._type_multiline(page, message, delay=6)
        page.wait_for_timeout(300)

        # Send: prefer the explicit Send button; fall back to Enter ONCE.
        sent = False
        for sel in ('button[aria-label*="Send" i]',
                    'button[aria-label*="Submit" i]',
                    'button:has-text("Send")'):
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_enabled() and btn.is_visible():
                    btn.click()
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            page.keyboard.press("Enter")

        # ---- Wait for the reply to settle ----
        effective_timeout = max(timeout, 240)
        page.wait_for_timeout(2000)
        deadline = time.time() + effective_timeout
        last_text = ""
        stable_ticks = 0
        new_seen = False

        def _is_generating() -> bool:
            try:
                return page.evaluate("""() => {
                    const stop = document.querySelector(
                        'button[aria-label*="Stop" i], button[aria-label*="Cancel" i]');
                    if (stop) {
                        const r = stop.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    const body = (document.body.innerText || '');
                    if (/generating|creating image|membuat gambar/i.test(body)) return true;
                    return false;
                }""")
            except Exception:
                return False

        def _has_generated_image() -> bool:
            """True once the generated still's <img> (blob/http, >=100px) has
            actually rendered inside a <generated-image>/<single-image>."""
            try:
                return page.evaluate("""() => {
                    const els = document.querySelectorAll(
                        'generated-image img, single-image img');
                    for (const img of els) {
                        const w = img.naturalWidth || img.getBoundingClientRect().width;
                        const h = img.naturalHeight || img.getBoundingClientRect().height;
                        if (img.complete && w >= 100 && h >= 100 &&
                            (img.currentSrc || img.src)) return true;
                    }
                    return false;
                }""")
            except Exception:
                return False

        # Does this prompt expect an image? If so, don't settle on text alone —
        # wait for the still to appear (Gemini often returns little/no text for
        # pure image generations, so last_text stays empty).
        expect_image = bool(re.search(
            r"image|photo|gambar|foto|photoreal|vertical|render",
            message, re.I))

        while time.time() < deadline:
            page.wait_for_timeout(1000)
            # Detect new reply turn
            current_turns = _count_response_turns()
            if current_turns > turns_before:
                new_seen = True

            # Extract ONLY the last response bubble — never the full body
            reply_text = _get_last_response_text()
            if not reply_text and new_seen:
                reply_text = last_text  # keep last good value during page nav

            if reply_text == last_text:
                stable_ticks += 1
            else:
                stable_ticks = 0
                last_text = reply_text

            img_ready = _has_generated_image()

            # If we're expecting an image, the image MUST be present (and
            # generation finished) before we break — regardless of text.
            if expect_image:
                if img_ready and not _is_generating():
                    break
                # Safety net: if generation clearly stopped and we still have
                # text but no image after a while, don't hang forever.
                if new_seen and last_text and stable_ticks >= 12 and not _is_generating():
                    break
            else:
                if new_seen and last_text and stable_ticks >= 3 and not _is_generating():
                    break
                if new_seen and last_text and stable_ticks >= 8:
                    break

        # ---- Harvest a generated image from Gemini ----
        # Strategy:
        # 1. Find the best candidate <img> in the last response bubble
        # 2. Try downloading via browser fetch (uses session cookies) first
        # 3. Fall back to Playwright context request
        # 4. Fall back to canvas/blob extraction
        media: list[dict] = []
        try:
            # Gemini renders the GENERATED still inside a <generated-image> /
            # <single-image> custom element, and its <img> src is a blob: URL
            # (e.g. blob:https://gemini.google.com/...). User-uploaded ref
            # photos, by contrast, live in <user-query-file-preview> as small
            # 80x80 lh3.googleusercontent.com thumbnails. We must pick the
            # FORMER and ignore the latter — ranking purely by pixel area is
            # not enough because a 512x452 thumbnail can outscore nothing.
            #
            # So: (1) restrict the scope to the generated-image containers
            # first, (2) only fall back to a generic scan if none exist, and
            # (3) explicitly drop user-upload preview thumbnails.
            candidates = page.evaluate("""() => {
                const imgs = [];
                document.querySelectorAll('img').forEach(img => {
                    const src = img.currentSrc || img.src || '';
                    if (!src) return;
                    // Skip the user's uploaded reference thumbnails.
                    if (/preview-image/.test((img.className || '') + '')) return;
                    if (img.closest('user-query-file-preview')) return;
                    const r = img.getBoundingClientRect();
                    const w = img.naturalWidth || r.width;
                    const h = img.naturalHeight || r.height;
                    if (w < 100 || h < 100) return;
                    imgs.push({
                        src,
                        scheme: (src.split(':')[0] || ''),
                        score: w * h,
                        w, h,
                        complete: img.complete,
                        inGenerated: !!img.closest('generated-image, single-image'),
                    });
                });
                // If the generated still is present, keep ONLY those — never
                // let a stray thumbnail win.
                const generated = imgs.filter(i => i.inGenerated);
                const pool = generated.length ? generated : imgs;
                pool.sort((a, b) => b.score - a.score);
                return pool.slice(0, 5);
            }""")

            logger.info(
                "[gemini] image candidates: "
                + str([(c.get("scheme"), c.get("w"), c.get("h"),
                        c.get("inGenerated"), c["src"][:60])
                       for c in (candidates or [])]))

            for cand in (candidates or []):
                src = cand.get("src", "")
                if not src:
                    continue

                local = None

                # blob: URLs are bound to the live document and frequently get
                # revoked the moment rendering completes, so a later fetch(blob)
                # fails silently. The reliable path is to draw the already-
                # rendered <img> onto a <canvas> and export PNG pixels directly —
                # this works even after the blob handle is gone. We locate the
                # img element by its src and read it in-page.
                if src.startswith("blob:"):
                    try:
                        b64 = page.evaluate("""async (url) => {
                            // find the matching, fully-loaded image element
                            const imgs = [...document.querySelectorAll(
                                'generated-image img, single-image img, img')];
                            let img = imgs.find(i =>
                                (i.currentSrc || i.src) === url) ||
                                imgs.find(i => i.closest('generated-image, single-image'));
                            if (!img) return null;
                            // ensure decoded
                            try { if (img.decode) await img.decode(); } catch(e) {}
                            const w = img.naturalWidth, h = img.naturalHeight;
                            if (!w || !h) return null;
                            const c = document.createElement('canvas');
                            c.width = w; c.height = h;
                            const ctx = c.getContext('2d');
                            ctx.drawImage(img, 0, 0, w, h);
                            try {
                                return c.toDataURL('image/png').split(',')[1] || null;
                            } catch(e) { return null; }  // tainted canvas
                        }""", src)
                        if b64:
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            short = _hash_url(src)
                            out = self.media_dir / f"gemini_{ts}_{short}.png"
                            out.write_bytes(base64.b64decode(b64))
                            local = out
                            logger.info(f"[gemini] Downloaded blob image via canvas: {out}")
                    except Exception as e_canvas:
                        logger.debug(f"[gemini] canvas extract failed: {e_canvas}")
                    # Fallback: try a direct blob fetch (works if still alive).
                    if not local:
                        local = self._download_to_media(src, ".png")
                        if local:
                            logger.info(f"[gemini] Downloaded blob image via fetch: {local}")
                elif src.startswith("data:"):
                    local = self._download_to_media(src, ".png")
                    if local:
                        logger.info(f"[gemini] Downloaded data: image: {local}")
                else:
                    # http(s) (e.g. lh3.googleusercontent.com full-size):
                    # in-page fetch first (carries the Google session), then
                    # Playwright context request as fallback.
                    try:
                        b64 = page.evaluate("""async (url) => {
                            try {
                                const resp = await fetch(url);
                                if (!resp.ok) return null;
                                const buf = await resp.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let bin = '';
                                for (let i = 0; i < bytes.byteLength; i++)
                                    bin += String.fromCharCode(bytes[i]);
                                return btoa(bin);
                            } catch(e) { return null; }
                        }""", src)
                        if b64:
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            short = _hash_url(src)
                            out = self.media_dir / f"gemini_{ts}_{short}.png"
                            out.write_bytes(base64.b64decode(b64))
                            local = out
                            logger.info(f"[gemini] Downloaded image via browser fetch: {out}")
                    except Exception as e_fetch:
                        logger.debug(f"[gemini] browser fetch failed: {e_fetch}")

                    if not local:
                        local = self._download_to_media(src, ".png")
                        if local:
                            logger.info(f"[gemini] Downloaded image via context request: {local}")

                if local and Path(local).exists() and Path(local).stat().st_size > 1000:
                    media.append({"type": "image", "url": src,
                                  "local_path": str(local)})
                    logger.info(f"[gemini] Image harvested OK ({Path(local).stat().st_size} bytes)")
                    break  # got one good image, stop
                elif local:
                    logger.debug(f"[gemini] Downloaded file too small, skipping")

            if not media:
                logger.warning("[gemini] No image harvested from response")
        except Exception as e:
            logger.warning(f"[gemini] image harvest failed: {e}")

        if not last_text and not media:
            debug = self._save_debug_artifacts(tag="gemini_no_response")
            logger.error(f"[gemini] No response. Debug: {debug}. URL: {self.current_url()}")
            raise TimeoutError("No response received from Gemini within timeout")

        return {"text": last_text, "media": media}

    # ------------------------------------------------------------------
    # Grok
    # ------------------------------------------------------------------

    def _dismiss_grok_popups(self):
        page = self._page
        dismissed = []
        try:
            for txt in ["Reject All", "Accept All Cookies", "Accept All"]:
                btn = page.query_selector(f'button:has-text("{txt}")')
                if btn and btn.is_visible():
                    btn.click()
                    dismissed.append(f"cookie:{txt}")
                    page.wait_for_timeout(500)
                    break
        except Exception:
            pass
        try:
            for txt in ["Save", "Continue", "Confirm"]:
                btn = page.query_selector(f'button:has-text("{txt}")')
                if btn and btn.is_visible() and btn.is_enabled():
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    if "submit" in aria or "send" in aria:
                        continue
                    btn.click()
                    dismissed.append(f"modal:{txt}")
                    page.wait_for_timeout(500)
                    break
        except Exception:
            pass
        try:
            btn = page.query_selector('button:has-text("Dismiss")')
            if btn and btn.is_visible():
                btn.click()
                dismissed.append("dismiss")
                page.wait_for_timeout(300)
        except Exception:
            pass

        # ---- Close any open dropdown/menu/overlay that intercepts clicks ----
        # The upload menu ("Upload a file / Recent / Skills / Add connector")
        # and similar floating menus sit on top of the page and swallow pointer
        # events, which makes input_el.click() time out with
        # "<html ...> intercepts pointer events". Press Escape to close them.
        try:
            overlay = page.query_selector(
                '[role="menu"]:visible, [role="dialog"]:visible, '
                '[role="listbox"]:visible, [data-state="open"][role="menu"]'
            )
            if overlay:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                # Second Escape in case the first only closed a submenu.
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                dismissed.append("overlay:escape")
        except Exception:
            pass

        if dismissed:
            logger.info(f"[grok] dismissed popups: {dismissed}")
        return dismissed

    def _is_grok_streaming(self) -> bool:
        """Return True while Grok is generating (Stop button visible OR Submit disabled)."""
        try:
            # Stop button visible during streaming
            stop = self._page.query_selector('button[aria-label*="Stop" i]')
            if stop and stop.is_visible():
                return True
            # Some Grok UIs swap the Submit button with a Stop icon — check whether
            # the regular Submit button is currently enabled. If it's disabled
            # while we expect a response, we're still streaming.
            submit = self._page.query_selector('button[aria-label="Submit" i]')
            if submit and not submit.is_enabled():
                return True
        except Exception:
            pass
        return False

    def _chat_grok(self, message: str, timeout: int,
                   attachments: list[Path]) -> dict:
        """Send message to Grok and extract response (text + media)."""
        page = self._page
        self._dump_phase("01_navigated")

        self._dismiss_grok_popups()
        self._dump_phase("01b_popups_cleared")

        existing_responses = page.query_selector_all('.response-content-markdown')
        existing_count = len(existing_responses)
        logger.info(f"[grok] phase=baseline existing_responses={existing_count}")

        # ---- Find input ----
        input_selectors = [
            'div[contenteditable="true"]',
            'textarea[placeholder*="Ask" i]',
            'textarea[placeholder*="What" i]',
            'textarea',
        ]
        input_el = None
        used_sel = None
        for sel in input_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    input_el = el
                    used_sel = sel
                    break
            except Exception:
                continue
        if not input_el:
            self._dump_phase("02a_no_input")
            raise RuntimeError("Couldn't find Grok input")
        logger.info(f"[grok] phase=input_found selector={used_sel}")
        self._dump_phase("02_input_found")

        if attachments:
            try:
                self._attach_files(attachments)
                logger.info(f"[grok] phase=attached count={len(attachments)}")
                self._dump_phase("02b_attached")
            except Exception as e:
                self._dump_phase("02b_attach_failed")
                raise RuntimeError(f"Grok attachment failed: {e}")

        # ---- Type ----
        try:
            # Make sure no floating menu/overlay is intercepting clicks before
            # we focus the input. The upload dropdown ("Upload a file / Recent
            # / Skills") is the usual culprit behind the 30s click timeout.
            self._dismiss_grok_popups()
            try:
                # Short timeout so a lingering overlay fails fast instead of
                # blocking for the full 30s default. We recover below.
                input_el.click(timeout=4000)
            except Exception:
                # Click was intercepted (overlay on top). Force-close menus and
                # focus the input directly via JS, then carry on.
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                self._dismiss_grok_popups()
                try:
                    input_el.click(timeout=4000)
                except Exception:
                    # Last resort: focus through the DOM without a pointer click.
                    try:
                        input_el.evaluate("el => el.focus()")
                    except Exception:
                        raise
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(150)
            self._type_multiline(page, message, delay=10)
            page.wait_for_timeout(400)
        except Exception as e:
            self._dump_phase("03a_type_error")
            raise RuntimeError(f"Failed typing message: {e}")
        logger.info(f"[grok] phase=typed chars={len(message)}")
        self._dump_phase("03_typed")

        # ---- Send ----
        send_selectors = [
            'button[aria-label="Submit" i]',
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ]
        sent_by = None
        for sel in send_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=4000)
                    sent_by = sel
                    break
            except Exception:
                continue
        if not sent_by:
            try:
                page.keyboard.press("Enter")
                sent_by = "keyboard:Enter"
            except Exception as e:
                self._dump_phase("04a_send_failed")
                raise RuntimeError(f"Couldn't send: {e}")
        logger.info(f"[grok] phase=sent via={sent_by}")
        self._dump_phase("04_sent")

        # ---- Wait for response — use streaming flag as primary signal ----
        page.wait_for_timeout(1500)
        deadline = time.time() + timeout
        last_text = ""
        seen_response = False
        last_log_at = 0
        latest_handle = None
        # We poll quickly while we wait for first text; once we have text and
        # the streaming flag goes off, we exit (no long stable wait).
        poll_interval_ms = 1000

        while time.time() < deadline:
            page.wait_for_timeout(poll_interval_ms)

            try:
                self._dismiss_grok_popups()
            except Exception:
                pass

            try:
                responses = page.query_selector_all('.response-content-markdown')
            except Exception as e:
                logger.debug(f"[grok] query failed: {e}")
                continue

            now_count = len(responses)
            current_text = ""

            if now_count > existing_count:
                latest_handle = responses[-1]
                # Read text from the response markdown
                try:
                    current_text = (latest_handle.inner_text() or "").strip()
                except Exception:
                    current_text = ""
                # If markdown is empty but the bubble has text, read the bubble
                if not current_text:
                    try:
                        bubble_text = page.evaluate(
                            """el => {
                                const b = el.closest('.message-bubble') || el.parentElement;
                                return b ? b.innerText : '';
                            }""",
                            latest_handle,
                        )
                        current_text = (bubble_text or "").strip()
                    except Exception:
                        pass

            # Unique image src count in the bubble (instead of raw <img> count)
            unique_imgs = 0
            if latest_handle:
                try:
                    unique_imgs = page.evaluate(
                        """el => {
                            const b = el.closest('.message-bubble') || el.parentElement || el;
                            const imgs = b.querySelectorAll('img');
                            const srcs = new Set();
                            imgs.forEach(i => {
                                const s = i.getAttribute('src') || '';
                                if (!s || s.startsWith('data:image/svg')) return;
                                const r = i.getBoundingClientRect();
                                if (r.width < 80 || r.height < 80) return;  // skip icons
                                srcs.add(s);
                            });
                            return srcs.size;
                        }""",
                        latest_handle,
                    )
                except Exception:
                    pass

            if time.time() - last_log_at > 4:
                logger.info(
                    f"[grok] phase=polling response_count={now_count} "
                    f"(baseline={existing_count}) latest_chars={len(current_text)} "
                    f"unique_imgs={unique_imgs} streaming={self._is_grok_streaming()}"
                )
                last_log_at = time.time()

            if current_text and not seen_response:
                seen_response = True
                logger.info(f"[grok] phase=first_growth latest_chars={len(current_text)}")
                self._dump_phase("05_first_growth")

            if current_text:
                last_text = current_text

            # ---- Completion check ----
            # Done when: streaming flag is False AND we have either text or images.
            # The streaming flag is the most accurate signal — when Grok finishes
            # generating (image rendered + text done), the Stop button disappears
            # and Submit re-enables. We give one extra short poll to capture any
            # final tokens.
            if (seen_response or unique_imgs > 0) and not self._is_grok_streaming():
                page.wait_for_timeout(700)
                # Final read
                if latest_handle:
                    try:
                        final_text = (latest_handle.inner_text() or "").strip()
                        if final_text:
                            last_text = final_text
                        else:
                            bubble_text = page.evaluate(
                                """el => {
                                    const b = el.closest('.message-bubble') || el.parentElement;
                                    return b ? b.innerText : '';
                                }""",
                                latest_handle,
                            )
                            if (bubble_text or "").strip():
                                last_text = (bubble_text or "").strip()
                    except Exception:
                        pass
                logger.info(
                    f"[grok] phase=stable response_chars={len(last_text)} "
                    f"unique_imgs={unique_imgs}"
                )
                self._dump_phase("06_stable")
                break

        if not last_text and not latest_handle:
            self._dump_phase("07_timeout")
            raise TimeoutError(
                f"Grok: no response detected within {timeout}s. "
                f"Baseline response count={existing_count}. "
                f"See debug/ folder for screenshots."
            )

        if not last_text:
            last_text = ""

        # ---- Extract media (dedup by URL inside the helper) ----
        media: list[dict] = []
        if latest_handle:
            page.wait_for_timeout(500)
            try:
                try:
                    bubble = latest_handle.evaluate_handle(
                        "el => el.closest('.message-bubble') || el.parentElement || el"
                    )
                except Exception:
                    bubble = latest_handle
                media = self._extract_media_from_element(bubble)
            except Exception as e:
                logger.warning(f"[grok] media extract failed: {e}")

        return {"text": last_text, "media": media}

    # ------------------------------------------------------------------
    # Grok Imagine (Image / Video generation)
    # ------------------------------------------------------------------

    def _imagine_grok(self, prompt: str, timeout: int,
                      imagine_opts: dict,
                      attachments: list[Path] = None) -> dict:
        """
        Use Grok's Imagine feature to generate images or videos.

        imagine_opts keys:
          mode       : "image" | "video"   (default "image")
          resolution : "480p" | "720p"     (default "720p", video only)
          duration   : "6s" | "10s"        (default "6s", video only)
          aspect     : "2:3" | "3:2" | "1:1" | "9:16" | "16:9"  (default "9:16")
        """
        page = self._page
        mode = imagine_opts.get("mode", "image")
        resolution = imagine_opts.get("resolution", "720p")
        duration = imagine_opts.get("duration", "6s")
        aspect = imagine_opts.get("aspect", "9:16")
        attachments = attachments or []

        self._dump_phase("imagine_01_start")
        self._dismiss_grok_popups()

        # ---- Navigate to Imagine page ----
        # IMPORTANT: go to /imagine directly, NOT /imagine/templates/...
        current_url = page.url or ""
        needs_navigate = (
            "/imagine" not in current_url.lower()
            or "/imagine/templates" in current_url.lower()
        )
        if needs_navigate:
            logger.info("[grok-imagine] Navigating to Imagine page")
            try:
                page.goto("https://grok.com/imagine", wait_until="domcontentloaded",
                          timeout=30_000)
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"[grok-imagine] Nav warning: {e}")

        self._dismiss_grok_popups()

        # ---- Close any template modal that might be open ----
        # Grok opens template modals with data-analytics-name="template-modal"
        # and a Close button with aria-label="Close"
        for attempt in range(3):
            try:
                # Check for template modal
                modal = page.query_selector('[data-analytics-name="template-modal"]')
                if modal and modal.is_visible():
                    close_btn = page.query_selector(
                        '[data-analytics-name="template-modal"] button[aria-label="Close"]'
                    )
                    if not close_btn or not close_btn.is_visible():
                        # Broader search: any close button in a dialog
                        close_btn = page.query_selector(
                            '[role="dialog"] button[aria-label="Close"]'
                        )
                    if close_btn and close_btn.is_visible():
                        close_btn.click()
                        page.wait_for_timeout(800)
                        logger.info("[grok-imagine] Closed template modal")
                        continue
                # Also check for radix dialog overlays
                overlay = page.query_selector(
                    '[data-state="open"].fixed.inset-0'
                )
                if overlay and overlay.is_visible():
                    # Press Escape to close
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(800)
                    logger.info("[grok-imagine] Closed overlay via Escape")
                    continue
                break
            except Exception as e:
                logger.debug(f"[grok-imagine] Modal dismiss attempt {attempt}: {e}")
                break

        self._dismiss_grok_popups()
        self._dump_phase("imagine_02_on_imagine_page")

        # ---- Helper: click a toolbar button by text ----
        # Helper to click toolbar buttons. From DevTools analysis:
        # - Toolbar buttons: BUTTON with class containing "font-medium", width ~60-90px
        # - Template labels: SPAN with class "font-normal text-white", width 176px
        # - Toolbar buttons are inside div.inline-flex containers
        # - Template labels are inside absolute-positioned spans
        def _click_toolbar_button(text: str) -> bool:
            """Click a toolbar button by exact text match.
            Only targets buttons inside the imagine toolbar (class font-medium)."""
            js_code = """(text) => {
                // Target: <button class="flex items-center py-1 px-3 relative text-xs font-medium ...">
                // These are the toolbar mode/option buttons
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const cls = btn.className || '';
                    // Toolbar buttons have font-medium class
                    if (!cls.includes('font-medium')) continue;
                    const t = btn.textContent.trim();
                    if (t === text) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }"""
            try:
                return bool(page.evaluate(js_code, text))
            except Exception as e:
                logger.debug(f"[grok-imagine] _click_toolbar_button({text}) failed: {e}")
                return False

        def _click_aspect_trigger() -> str | None:
            """Click the current aspect ratio button to open picker.
            Returns the current aspect text if found."""
            js_code = """() => {
                // Aspect button: <button class="flex items-center gap-1.5 px-3 text-xs font-medium ring-1 ...">
                const aspects = ['2:3', '3:2', '1:1', '9:16', '16:9'];
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const cls = btn.className || '';
                    if (!cls.includes('font-medium') || !cls.includes('ring-1')) continue;
                    const t = btn.textContent.trim();
                    if (aspects.includes(t)) {
                        btn.click();
                        return t;
                    }
                }
                return null;
            }"""
            try:
                return page.evaluate(js_code)
            except Exception:
                return None

        # ---- Select Image or Video mode ----
        logger.info(f"[grok-imagine] Selecting mode: {mode}")
        mode_text = "Image" if mode == "image" else "Video"
        if _click_toolbar_button(mode_text):
            page.wait_for_timeout(1000)
            logger.info(f"[grok-imagine] Clicked toolbar mode: {mode_text}")
        else:
            logger.warning(f"[grok-imagine] Could not find toolbar button for: {mode_text}")

        self._dump_phase("imagine_03_mode_selected")

        # ---- Quality selection (applies to both image and video) ----
        # Wait for toolbar to update after mode switch
        page.wait_for_timeout(1000)

        res_primary = "Quality" if resolution == "720p" else "Speed"
        res_fallback = "720p" if resolution == "720p" else "480p"
        logger.info(f"[grok-imagine] Setting quality: {resolution}")
        if _click_toolbar_button(res_primary):
            page.wait_for_timeout(500)
            logger.info(f"[grok-imagine] Clicked quality: {res_primary}")
        elif _click_toolbar_button(res_fallback):
            page.wait_for_timeout(500)
            logger.info(f"[grok-imagine] Clicked quality: {res_fallback}")
        else:
            logger.warning(f"[grok-imagine] Could not click quality button")

        # ---- Video-specific: duration ----
        if mode == "video":
            logger.info(f"[grok-imagine] Setting duration: {duration}")
            if _click_toolbar_button(duration):
                page.wait_for_timeout(500)
                logger.info(f"[grok-imagine] Clicked duration: {duration}")

        self._dump_phase("imagine_04_video_opts")

        # ---- Aspect ratio ----
        logger.info(f"[grok-imagine] Setting aspect ratio: {aspect}")
        try:
            current = _click_aspect_trigger()
            if current:
                # Wait for popover to fully render
                page.wait_for_timeout(1000)
                logger.info(f"[grok-imagine] Opened aspect picker (current: {current})")

                if current == aspect:
                    # Already the right aspect, close the picker
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                    logger.info(f"[grok-imagine] Aspect already correct: {aspect}")
                else:
                    # Click the desired aspect ratio in the popover
                    # Use JS to find and click the right option
                    clicked = page.evaluate("""(target) => {
                        // Radix popover items - look for the aspect text
                        // in any clickable element that appeared recently (popover)
                        const candidates = [];
                        document.querySelectorAll('button, div[role="menuitem"], div[role="option"], div, span').forEach(el => {
                            const r = el.getBoundingClientRect();
                            if (r.width < 20 || r.height < 15) return;
                            if (r.width > 300) return;  // too wide to be a popover item
                            const t = el.textContent.trim();
                            // Match "9:16" in text like "9:16" or "9:16 Vertical"
                            if (t === target || t.startsWith(target + ' ') || t.startsWith(target + '\\n')) {
                                candidates.push({el, w: r.width, t});
                            }
                        });
                        if (candidates.length === 0) return null;
                        // Click smallest matching element (most specific)
                        candidates.sort((a, b) => a.w - b.w);
                        candidates[0].el.click();
                        return candidates[0].t;
                    }""", aspect)

                    if clicked:
                        page.wait_for_timeout(500)
                        logger.info(f"[grok-imagine] Clicked aspect option: {clicked}")
                    else:
                        # Fallback: try Playwright selectors
                        for sel in [
                            f'button:has-text("{aspect}")',
                            f'[role="menuitem"]:has-text("{aspect}")',
                            f'[role="option"]:has-text("{aspect}")',
                        ]:
                            try:
                                opts = page.query_selector_all(sel)
                                for opt in opts:
                                    if opt.is_visible():
                                        opt.click()
                                        clicked = True
                                        break
                                if clicked:
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            page.keyboard.press("Escape")
                            logger.warning(f"[grok-imagine] Could not select aspect: {aspect}")
                        else:
                            page.wait_for_timeout(500)
                            logger.info(f"[grok-imagine] Selected aspect via selector: {aspect}")
            else:
                logger.warning("[grok-imagine] No aspect ratio trigger button found")
        except Exception as e:
            logger.warning(f"[grok-imagine] Aspect ratio warning: {e}")

        self._dump_phase("imagine_05_aspect_set")

        # ---- Upload attachments if any ----
        if attachments:
            try:
                self._attach_files(attachments)
                logger.info(f"[grok-imagine] Attached {len(attachments)} file(s)")
                self._dump_phase("imagine_05b_attached")
            except Exception as e:
                logger.warning(f"[grok-imagine] Attachment failed: {e}")
                self._dump_phase("imagine_05b_attach_failed")

        # ---- Find input and type prompt ----
        # Grok Imagine uses data-testid="chat-input" with a TipTap editor inside
        input_el = None
        # First try: the contenteditable div inside chat-input
        try:
            container = page.query_selector('[data-testid="chat-input"]')
            if container:
                ce = container.query_selector('div[contenteditable="true"]')
                if ce and ce.is_visible():
                    input_el = ce
                    logger.info("[grok-imagine] Found TipTap editor inside chat-input")
        except Exception:
            pass
        # Fallback selectors
        if not input_el:
            for sel in [
                'div[contenteditable="true"]',
                'textarea[placeholder*="imagine" i]',
                'textarea',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        input_el = el
                        logger.info(f"[grok-imagine] Found input via fallback: {sel}")
                        break
                except Exception:
                    continue
        if not input_el:
            self._dump_phase("imagine_06_no_input")
            raise RuntimeError("Couldn't find Grok Imagine input field")

        try:
            input_el.click()
            page.wait_for_timeout(300)
            # For contenteditable divs, select all and delete
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(150)
            self._type_multiline(page, prompt, delay=10)
            page.wait_for_timeout(400)
        except Exception as e:
            raise RuntimeError(f"Failed typing Imagine prompt: {e}")

        self._dump_phase("imagine_06_typed")

        # ---- Send ----
        send_selectors = [
            'button[aria-label="Submit"]',
            'button[aria-label="Submit" i]',
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ]
        sent = False
        for sel in send_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible() and btn.is_enabled():
                    btn.click()
                    sent = True
                    logger.info(f"[grok-imagine] Sent via: {sel}")
                    break
            except Exception:
                continue
        if not sent:
            page.keyboard.press("Enter")
            logger.info("[grok-imagine] Sent via Enter key")

        self._dump_phase("imagine_07_sent")

        # ---- Wait for generation ----
        effective_timeout = timeout
        if mode == "video":
            effective_timeout = max(timeout, 300)

        # Remember URL before send
        pre_send_url = page.url or ""
        page.wait_for_timeout(3000)
        deadline = time.time() + effective_timeout
        last_log = 0
        result_media = []
        url_changed = False

        while time.time() < deadline:
            page.wait_for_timeout(2000)

            try:
                self._dismiss_grok_popups()
            except Exception:
                pass

            current_url = page.url or ""

            # Detect URL change
            if not url_changed and current_url != pre_send_url:
                url_changed = True
                logger.info(f"[grok-imagine] URL changed to: {current_url[:80]}")
                page.wait_for_timeout(3000)

            # Also detect results page by Back button or download button
            if not url_changed:
                try:
                    has_results_indicator = page.evaluate("""() => {
                        // Check for Back button (← arrow on results page)
                        const back = document.querySelector('[aria-label="Back"]');
                        if (back) return 'back';
                        // Check for download button (results page has download icon)
                        const dl = document.querySelector('[aria-label="Download"], [aria-label="Save"]');
                        if (dl) return 'download';
                        // Check for "Make video" button (image results page)
                        const mv = document.querySelector('button');
                        if (mv) {
                            const all = document.querySelectorAll('button');
                            for (const b of all) {
                                if (b.textContent.includes('Make video')) return 'makevideo';
                            }
                        }
                        return null;
                    }""")
                    if has_results_indicator:
                        url_changed = True
                        logger.info(f"[grok-imagine] Results page detected via: {has_results_indicator}")
                except Exception:
                    pass

            if time.time() - last_log > 5:
                logger.info(
                    f"[grok-imagine] Waiting... url_changed={url_changed} "
                    f"url={current_url[:60]} remaining={int(deadline - time.time())}s"
                )
                last_log = time.time()
                self._dump_phase("imagine_08_waiting")

            # Try to extract media once we detect results page
            if url_changed:
                try:
                    # --- Video extraction ---
                    if mode == "video":
                        # Check if still generating (progress bar visible)
                        is_still_generating = page.evaluate("""() => {
                            const all = document.body.innerText || '';
                            if (all.includes('Generating') && all.includes('%')) return true;
                            if (all.includes('Cancel')) {
                                // Check if Cancel button is near a percentage
                                const buttons = document.querySelectorAll('button');
                                for (const b of buttons) {
                                    if (b.textContent.trim() === 'Cancel' && b.getBoundingClientRect().width > 30) return true;
                                }
                            }
                            return false;
                        }""")

                        if is_still_generating:
                            # Still generating, don't extract yet
                            continue

                        video_srcs = page.evaluate("""() => {
                            const srcs = [];
                            document.querySelectorAll('video').forEach(v => {
                                const src = v.src || v.currentSrc || '';
                                if (src) srcs.push(src);
                                v.querySelectorAll('source').forEach(s => {
                                    if (s.src) srcs.push(s.src);
                                });
                            });
                            return [...new Set(srcs)];
                        }""")
                        for src in (video_srcs or []):
                            if src and src not in [m.get("url") for m in result_media]:
                                local = self._download_to_media(src, ".mp4")
                                result_media.append({
                                    "type": "video", "url": src,
                                    "local_path": str(local) if local else "",
                                })

                    # --- Image extraction (only for image mode) ---
                    else:
                        # Grok shows a BLURRED low-res placeholder while the
                        # image is still rendering. The old code grabbed the
                        # first <img> >=200px and broke immediately, so it
                        # frequently saved that blur (the noisy ~70KB stills).
                        # Now we (a) wait for a completion indicator and
                        # (b) only accept an image whose *intrinsic* resolution
                        # is full-size, choosing the largest candidate.
                        info = page.evaluate("""() => {
                            const done = !!document.querySelector(
                                '[aria-label="Download"], [aria-label="Save"]') ||
                                Array.from(document.querySelectorAll('button'))
                                     .some(b => (b.textContent||'').includes('Make video'));
                            const imgs = [];
                            document.querySelectorAll('img').forEach(img => {
                                const r = img.getBoundingClientRect();
                                const src = img.src || '';
                                if (!src || src.startsWith('data:') || src.startsWith('blob:')) return;
                                if (r.width < 200 || r.height < 200) return;
                                imgs.push({
                                    src,
                                    nw: img.naturalWidth || 0,
                                    nh: img.naturalHeight || 0,
                                    complete: img.complete === true,
                                });
                            });
                            return {done, imgs};
                        }""")
                        cand = (info or {}).get("imgs", []) or []
                        done = (info or {}).get("done", False)
                        # Finished frames are intrinsically large; the blurred
                        # placeholder is a small image stretched to display size.
                        ready = [c for c in cand
                                 if c.get("complete") and c.get("nw", 0) >= 512
                                 and c.get("nh", 0) >= 512]
                        if ready and done:
                            ready.sort(key=lambda c: c["nw"] * c["nh"], reverse=True)
                            best = ready[0]["src"]
                            if best not in [m.get("url") for m in result_media]:
                                local = self._download_to_media(best, ".png")
                                result_media.append({
                                    "type": "image", "url": best,
                                    "local_path": str(local) if local else "",
                                })
                        # else: still rendering -> keep polling, don't grab blur.
                except Exception as e:
                    logger.warning(f"[grok-imagine] Media extract error: {e}")

                if result_media:
                    logger.info(
                        f"[grok-imagine] Done! Found {len(result_media)} media file(s)"
                    )
                    self._dump_phase("imagine_10_done")
                    break

        # Final attempt: if we have a URL change but nothing downloaded yet,
        # wait a bit longer and take the single highest-resolution image
        # (or the video). Picking by intrinsic size avoids grabbing a UI icon
        # or a leftover blurred preview.
        if not result_media:
            try:
                page.wait_for_timeout(5000)
                picked = page.evaluate("""() => {
                    let best = null;
                    document.querySelectorAll('img').forEach(img => {
                        const r = img.getBoundingClientRect();
                        const src = img.src || '';
                        if (!src || src.startsWith('data:') || src.startsWith('blob:')) return;
                        if (r.width < 150 || r.height < 150) return;
                        const score = (img.naturalWidth||0) * (img.naturalHeight||0);
                        if (!best || score > best.score) best = {src, score};
                    });
                    let vsrc = '';
                    const v = document.querySelector('video');
                    if (v) vsrc = v.src || v.currentSrc || '';
                    return {img: best ? best.src : '', video: vsrc};
                }""")
                picked = picked or {}
                if mode == "video" and picked.get("video"):
                    local = self._download_to_media(picked["video"], ".mp4")
                    result_media.append({
                        "type": "video", "url": picked["video"],
                        "local_path": str(local) if local else "",
                    })
                elif picked.get("img"):
                    local = self._download_to_media(picked["img"], ".png")
                    result_media.append({
                        "type": "image", "url": picked["img"],
                        "local_path": str(local) if local else "",
                    })
            except Exception as e:
                logger.warning(f"[grok-imagine] Final media attempt failed: {e}")

        response_text = ""
        try:
            responses = page.query_selector_all('.response-content-markdown')
            if responses:
                response_text = (responses[-1].inner_text() or "").strip()
        except Exception:
            pass

        if not result_media and not response_text:
            self._dump_phase("imagine_09_timeout")
            raise TimeoutError(
                f"Grok Imagine: no output detected within {effective_timeout}s. "
                f"Mode={mode}. See debug/ folder."
            )

        self._dump_phase("imagine_10_done")
        # Close any open result/preview modal so the next scene (or the user)
        # isn't blocked by a stuck fullscreen image popup.
        self._close_imagine_preview()
        return {"text": response_text, "media": result_media}

    def _close_imagine_preview(self):
        """Dismiss the fullscreen image/video preview modal that Grok opens
        after a generation completes. Tries the Close (X) button first, then
        Escape, then a click on the dark backdrop."""
        page = self._page
        for _ in range(3):
            try:
                closed = page.evaluate("""() => {
                    // 1) Explicit close buttons (X / aria-label Close)
                    const btns = Array.from(document.querySelectorAll(
                        'button[aria-label="Close"], button[aria-label*="close" i], ' +
                        '[role="dialog"] button'));
                    for (const b of btns) {
                        const r = b.getBoundingClientRect();
                        // top-right corner X of an open modal
                        if (r.width > 0 && r.top < 160 && r.left > window.innerWidth * 0.6) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if closed:
                    page.wait_for_timeout(400)
                    continue
            except Exception:
                pass
            # Fallback: Escape closes radix/headless dialogs.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                pass
            # Stop once no dialog/overlay remains visible.
            try:
                still = page.evaluate("""() => {
                    const d = document.querySelector('[role="dialog"], [data-state="open"].fixed.inset-0');
                    return !!(d && d.getBoundingClientRect().width > 0);
                }""")
                if not still:
                    break
            except Exception:
                break

    def _dump_phase(self, tag: str):
        if not os.environ.get("DEBUG_PHASES"):
            return
        try:
            base = Path(os.environ.get("DATA_DIR", "")) if os.environ.get("DATA_DIR") else None
            if base is None:
                base = self.session_path.parent.parent / "debug"
            base.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%H%M%S")
            self._page.screenshot(
                path=str(base / f"{self.platform}_phase_{tag}_{ts}.png"),
                full_page=False,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        except: pass
        try:
            if self._pw:
                self._pw.stop()
        except: pass
        self._ready = False
        self._has_active_chat = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# ChatEnginePool
# ---------------------------------------------------------------------------


class ChatEnginePool:
    """
    Manages multiple chat sessions across platforms.
    Reuses sessions when possible, creates new ones on demand.

    A "session" here = one running browser instance. We keep it open
    across multiple chat() calls so subsequent messages continue the
    same Grok / ChatGPT conversation.
    """

    def __init__(self, sessions_dir: Path, headless: bool = True,
                 media_dir: Optional[Path] = None, cdp_url: Optional[str] = None):
        self.sessions_dir = sessions_dir
        self.headless = headless
        self.media_dir = media_dir or (sessions_dir.parent / "media")
        self.cdp_url = cdp_url
        self._sessions: dict[str, ChatSession] = {}

    def _key(self, platform: str, label: str) -> str:
        return f"{platform}_{label}"

    def get_session(self, platform: str, label: str = "default",
                    create: bool = True) -> Optional[ChatSession]:
        """Get or create a chat session for the given platform/label."""
        key = self._key(platform, label)
        existing = self._sessions.get(key)
        if existing and existing._ready:
            return existing

        if not create:
            return None

        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        session_path = self.sessions_dir / f"{platform}_{safe_label}.json"

        if not self.cdp_url and not session_path.exists():
            raise FileNotFoundError(
                f"No session file for {platform}/{label}. "
                f"Capture session first using the GUI or Chrome extension."
            )

        session = ChatSession(platform, session_path, self.headless,
                              media_dir=self.media_dir, cdp_url=self.cdp_url)
        session.start()
        self._sessions[key] = session
        return session

    def chat(self, platform: str, message: str,
             label: str = "default", timeout: int = 120,
             attachments: Optional[list] = None,
             force_new_chat: bool = False,
             imagine_opts: Optional[dict] = None) -> dict:
        """
        Send a message to a platform and get the response.

        Args:
            attachments: optional list of file paths (str or Path) to upload.
            force_new_chat: if True, reset the chat first.
            imagine_opts: if provided, use Grok Imagine mode.
        """
        session = self.get_session(platform, label)
        att_paths = [Path(p) for p in (attachments or [])]
        return session.send_message(
            message, timeout=timeout, attachments=att_paths,
            force_new_chat=force_new_chat, imagine_opts=imagine_opts,
        )

    def start_new_chat(self, platform: str, label: str = "default") -> bool:
        """Explicitly reset the conversation for a (platform, label)."""
        session = self.get_session(platform, label, create=False)
        if session is None:
            return False
        session.start_new_chat()
        return True

    def navigate_to_chat(self, platform: str, url: str, label: str = "default") -> list[dict]:
        """Navigate the platform browser to a specific chat URL and return scraped history."""
        session = self.get_session(platform, label)
        if session is None:
            return []
        return session.navigate_to_chat(url)

    def get_access_token(self, platform: str, label: str = "default") -> str:
        """Extract the access token from the logged-in browser page."""
        session = self.get_session(platform, label, create=False)
        if session is None:
            return ""
        return session.get_access_token()

    def list_active_sessions(self) -> list[dict]:
        """List browser sessions that are currently open."""
        out = []
        for key, s in self._sessions.items():
            if s._ready:
                out.append({
                    "key": key, "platform": s.platform,
                    "url": s.current_url(),
                    "has_active_chat": s.has_active_chat(),
                    "message_count": s.message_count,
                })
        return out

    def list_available_sessions(self) -> list[dict]:
        """List session FILES on disk (captured logins)."""
        result = []
        if not self.sessions_dir.exists():
            return result
        for f in self.sessions_dir.glob("*.json"):
            parts = f.stem.split("_", 1)
            if len(parts) == 2:
                platform, label = parts
                if platform in ("chatgpt", "grok"):
                    result.append({
                        "platform": platform, "label": label,
                        "path": str(f), "size": f.stat().st_size,
                    })
        return result

    def close_session(self, platform: str, label: str = "default") -> bool:
        """Close just one browser session."""
        key = self._key(platform, label)
        session = self._sessions.pop(key, None)
        if session:
            session.close()
            return True
        return False

    def close_all(self):
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()


# ---------------------------------------------------------------------------
# BridgeWorker — owns the pool on a single thread
# ---------------------------------------------------------------------------


class BridgeWorker:
    """
    Owns a ChatEnginePool inside a dedicated background thread.

    Why this exists
    ---------------
    Playwright's sync_api creates greenlets bound to the thread that
    started sync_playwright(). Using Page / Browser / Context objects
    from a different thread raises:
        greenlet.error: cannot switch to a different thread
    Both the tkinter GUI (each Send spawns a new worker thread) and the
    Flask API (each request gets its own thread) need pool access from
    arbitrary threads. BridgeWorker serializes all pool ops onto ONE
    long-lived background thread so the pool always runs in the same
    thread context.

    Usage
    -----
        worker = BridgeWorker(sessions_dir, media_dir=media_dir)
        worker.start(headless=False)
        result = worker.chat("grok", "hello")    # blocks until response
        worker.close_all_sessions()              # frees browsers (keeps worker)
        worker.shutdown()                        # at app exit
    """

    _SHUTDOWN = object()

    def __init__(self, sessions_dir: Path, media_dir: Optional[Path] = None):
        self.sessions_dir = sessions_dir
        self.media_dir = media_dir
        self._headless = True
        self._cdp_url: Optional[str] = None
        self._queue: queue.Queue = queue.Queue()
        self._pool: Optional[ChatEnginePool] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._start_lock = threading.Lock()
        # Snapshot of pool state, refreshed after every op.
        # Read by GUI's status indicator without blocking on the queue.
        self._status_lock = threading.Lock()
        self._status_snapshot: dict = {"active": [], "pool_alive": False}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, headless: bool = True):
        """Spawn the worker thread. Idempotent — calling twice is harmless."""
        with self._start_lock:
            if self._started:
                # Already running. If headless setting differs, switch.
                if bool(headless) != self._headless:
                    self.set_headless(bool(headless))
                return
            self._headless = bool(headless)
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name="bridge-worker"
            )
            self._thread.start()
            self._started = True

    def shutdown(self, wait: bool = True, timeout: float = 10):
        """Signal the worker to close everything and exit."""
        if not self._started:
            return
        try:
            self._queue.put_nowait(self._SHUTDOWN)
        except Exception:
            pass
        if wait and self._thread:
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass
        self._started = False

    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal worker loop
    # ------------------------------------------------------------------

    def _ensure_pool(self):
        if self._pool is None:
            self._pool = ChatEnginePool(
                self.sessions_dir, headless=self._headless,
                media_dir=self.media_dir, cdp_url=self._cdp_url,
            )

    def _refresh_status_snapshot(self):
        active: list = []
        pool_alive = self._pool is not None
        if self._pool is not None:
            try:
                active = self._pool.list_active_sessions()
            except Exception:
                active = []
        with self._status_lock:
            self._status_snapshot = {"active": active, "pool_alive": pool_alive}

    def _run_loop(self):
        logger.info("BridgeWorker thread started")
        try:
            while True:
                cmd = self._queue.get()
                if cmd is self._SHUTDOWN:
                    break
                op, args, kwargs, holder, done = cmd
                try:
                    self._ensure_pool()
                    holder["result"] = op(self._pool, *args, **kwargs)
                except Exception as e:
                    holder["error"] = e
                    logger.exception("BridgeWorker op failed")
                finally:
                    try:
                        self._refresh_status_snapshot()
                    except Exception:
                        pass
                    done.set()
        finally:
            if self._pool is not None:
                try:
                    self._pool.close_all()
                except Exception:
                    pass
                self._pool = None
            self._refresh_status_snapshot()
            logger.info("BridgeWorker thread stopped")

    def _execute(self, op, *args, queue_timeout: float = 300, **kwargs):
        if not self.is_running():
            raise RuntimeError("BridgeWorker is not running — call .start() first")
        holder: dict = {}
        done = threading.Event()
        self._queue.put((op, args, kwargs, holder, done))
        if not done.wait(timeout=queue_timeout):
            raise TimeoutError(
                f"BridgeWorker did not respond within {queue_timeout}s "
                f"(queue may be backed up)"
            )
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    # ------------------------------------------------------------------
    # Public API — mirrors ChatEnginePool methods, thread-safe
    # ------------------------------------------------------------------

    def chat(self, platform: str, message: str, label: str = "default",
             timeout: int = 180, attachments: Optional[list] = None,
             force_new_chat: bool = False,
             imagine_opts: Optional[dict] = None) -> dict:
        """Send a chat message. Blocks until the response is ready."""
        def op(pool):
            return pool.chat(
                platform, message, label=label, timeout=timeout,
                attachments=attachments, force_new_chat=force_new_chat,
                imagine_opts=imagine_opts,
            )
        # Allow extra headroom over the inner timeout for browser startup, etc.
        return self._execute(op, queue_timeout=timeout + 60)

    def start_new_chat(self, platform: str, label: str = "default") -> bool:
        """Force a new conversation for (platform, label). Returns True if reset."""
        def op(pool):
            return pool.start_new_chat(platform, label)
        return self._execute(op, queue_timeout=60)

    def navigate_to_chat(self, platform: str, url: str, label: str = "default") -> list[dict]:
        """Navigate the platform browser to a specific existing chat URL.
        Returns the scraped message history as a list of dicts."""
        def op(pool):
            return pool.navigate_to_chat(platform, url, label)
        result = self._execute(op, queue_timeout=60)
        return result if isinstance(result, list) else []

    def get_access_token(self, platform: str, label: str = "default") -> str:
        """Extract the access token from the running browser (ChatGPT only)."""
        def op(pool):
            return pool.get_access_token(platform, label)
        result = self._execute(op, queue_timeout=30)
        return result or ""

    def close_session(self, platform: str, label: str = "default") -> bool:
        """Close one specific browser session."""
        def op(pool):
            return pool.close_session(platform, label)
        return self._execute(op, queue_timeout=60)

    def close_all_sessions(self) -> bool:
        """Close all browser sessions (frees RAM). Worker thread keeps running."""
        def op(pool):
            pool.close_all()
            return True
        return self._execute(op, queue_timeout=120)

    def abort(self) -> None:
        """Force-stop everything immediately, even mid-operation.

        Unlike close_all_sessions (which is queued behind the running op and
        therefore cannot interrupt a stuck chat()), abort() closes the
        browsers directly from the *calling* thread. Closing the browser makes
        any in-flight Playwright call on the worker thread raise at once, so a
        hung typing/click returns instead of blocking until timeout.

        Safe to call from the GUI/cancel thread. After abort() the worker is
        shut down; create a fresh BridgeWorker for the next request.
        """
        # 1) Close the live pool's browsers out-of-band to unblock the worker.
        pool = self._pool
        if pool is not None:
            try:
                pool.close_all()
            except Exception:
                logger.exception("abort: pool.close_all failed")
        # 2) Tell the worker thread to exit (don't wait — it may be unwinding).
        try:
            self.shutdown(wait=False)
        except Exception:
            pass

    def list_active_sessions(self) -> list:
        """Read-only snapshot of currently open browser sessions. Non-blocking."""
        with self._status_lock:
            return list(self._status_snapshot.get("active", []))

    def list_available_sessions(self) -> list:
        """Read session FILES from disk — safe to call from any thread."""
        result = []
        if not self.sessions_dir.exists():
            return result
        for f in self.sessions_dir.glob("*.json"):
            parts = f.stem.split("_", 1)
            if len(parts) == 2:
                platform, label = parts
                if platform in ("chatgpt", "grok"):
                    result.append({
                        "platform": platform, "label": label,
                        "path": str(f), "size": f.stat().st_size,
                    })
        return result

    def set_headless(self, headless: bool) -> bool:
        """Switch headless mode. Closes any open browsers; pool is rebuilt
        with the new setting on the next chat call."""
        new = bool(headless)

        def op(pool):
            try:
                pool.close_all()
            except Exception:
                pass
            # Mutate worker state from inside the worker thread
            self._pool = None
            self._headless = new
            return True
        return self._execute(op, queue_timeout=60)

    def set_cdp_url(self, cdp_url: Optional[str]) -> bool:
        """Switch to CDP mode (connect to real Chrome) or back to standard.

        Args:
            cdp_url: e.g. "http://127.0.0.1:9222", or None to disable.
        """
        def op(pool):
            try:
                pool.close_all()
            except Exception:
                pass
            self._pool = None
            self._cdp_url = cdp_url or None
            return True
        return self._execute(op, queue_timeout=60)

    # Context manager helpers
    def __enter__(self):
        self.start(self._headless)
        return self

    def __exit__(self, *args):
        self.shutdown()