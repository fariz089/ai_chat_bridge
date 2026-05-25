"""
Extension Server for AI Chat Bridge.
Receives POST from Chrome extension with captured cookies/localStorage.
Same architecture as multi_capture extension_server.py.

PATCH: accepts both `state` and `storage_state` keys for backward compat,
and saves bad payloads to debug/ for inspection when validation fails.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from flask import Flask, request, jsonify, make_response
except ImportError as e:
    raise ImportError(f"flask not installed. pip install flask>=3.0: {e}")

logger = logging.getLogger(__name__)

REQUIRED_COOKIES = {
    "chatgpt": ("__Secure-next-auth.session-token",),
    "grok": ("sso",),
}

VALID_PLATFORMS = set(REQUIRED_COOKIES.keys())


def _safe_label(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]


def _validate_state(state, platform: str) -> Optional[str]:
    if not isinstance(state, dict):
        return "state must be an object."
    cookies = state.get("cookies")
    if not isinstance(cookies, list):
        return "state.cookies must be an array."
    if not cookies:
        return "state.cookies is empty."
    names = set()
    for c in cookies:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            names.add(c["name"])
    # Reassemble chunked cookies (e.g. "token.0", "token.1" → "token")
    # Browsers split large cookies into numbered chunks; treat them as the base name.
    for name in list(names):
        m = re.match(r'^(.+)\.\d+$', name)
        if m:
            names.add(m.group(1))
    required = REQUIRED_COOKIES.get(platform, ())
    missing = [r for r in required if r not in names]
    if missing:
        got_preview = sorted(list(names))[:8]
        return (f"Required cookies missing for {platform}: {', '.join(missing)}. "
                f"Got {len(names)} cookies, sample: {got_preview}")
    return None


def _dump_debug_payload(sessions_dir: Path, platform: str, payload, reason: str):
    """When validation fails, drop the payload to disk so the user can share it."""
    try:
        debug_dir = sessions_dir.parent / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = debug_dir / f"push_fail_{platform or 'unknown'}_{ts}.json"
        # Redact cookie values so the dump is safe to share
        redacted = json.loads(json.dumps(payload)) if payload else {}
        try:
            state = redacted.get("state") or redacted.get("storage_state") or {}
            for c in (state.get("cookies") or []):
                if isinstance(c, dict) and "value" in c:
                    v = str(c["value"])
                    c["value"] = f"<redacted len={len(v)}>"
            for o in (state.get("origins") or []):
                for it in (o.get("localStorage") or []):
                    if isinstance(it, dict) and "value" in it:
                        v = str(it["value"])
                        it["value"] = f"<redacted len={len(v)}>"
        except Exception:
            pass
        out.write_text(json.dumps({"reason": reason, "payload": redacted},
                                  indent=2, ensure_ascii=False), encoding="utf-8")
        logger.warning(f"Bad push saved (redacted): {out}")
    except Exception as e:
        logger.debug(f"Couldn't write debug payload: {e}")


def make_app(sessions_dir: Path,
             on_session_saved: Optional[Callable[[str, str, Path], None]] = None) -> Flask:
    app = Flask(__name__)

    def _cors_headers(resp):
        origin = request.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or \
           origin.startswith("http://localhost") or \
           origin.startswith("http://127.0.0.1"):
            resp.headers["Access-Control-Allow-Origin"] = origin
        else:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "3600"
        return resp

    @app.after_request
    def after(resp):
        return _cors_headers(resp)

    @app.route("/extension/push", methods=["OPTIONS"])
    def push_options():
        return _cors_headers(make_response("", 204))

    @app.route("/extension/push", methods=["POST"])
    def push_session():
        try:
            payload = request.get_json(silent=True) or {}
        except Exception as e:
            return jsonify({"error": f"invalid json: {e}"}), 400

        platform = (payload.get("platform") or "").strip().lower()
        label = (payload.get("label") or "").strip()
        # Accept both modern (`state`) and legacy (`storage_state`) keys
        state = payload.get("state")
        if state is None:
            state = payload.get("storage_state")

        if platform not in VALID_PLATFORMS:
            _dump_debug_payload(sessions_dir, platform, payload, "invalid_platform")
            return jsonify({"error": f"platform '{platform}' invalid. Valid: {sorted(VALID_PLATFORMS)}"}), 400
        if not label:
            return jsonify({"error": "label is empty"}), 400
        err = _validate_state(state, platform)
        if err:
            _dump_debug_payload(sessions_dir, platform, payload, err)
            logger.warning(f"[{platform}] push rejected: {err}")
            return jsonify({"error": err}), 400

        clean_cookies = []
        _VALID_SS = {"Strict", "Lax", "None"}
        for c in state.get("cookies") or []:
            if not isinstance(c, dict): continue
            name, value = c.get("name"), c.get("value")
            if not name or value in (None, ""): continue
            # Normalize sameSite — Playwright only accepts Strict|Lax|None
            ss_raw = c.get("sameSite")
            ss_low = (str(ss_raw) if ss_raw is not None else "").lower()
            if ss_raw in _VALID_SS:
                ss = ss_raw
            elif ss_low in ("no_restriction", "none"):
                ss = "None"
            elif ss_low == "strict":
                ss = "Strict"
            else:
                ss = "Lax"
            domain = c.get("domain") or ""
            if not domain:
                continue  # Playwright rejects cookies without a domain
            secure = bool(c.get("secure", False))
            # sameSite "None" requires secure=true per spec; downgrade to Lax otherwise
            if ss == "None" and not secure:
                ss = "Lax"
            entry = {"name": str(name), "value": str(value),
                     "domain": domain, "path": c.get("path") or "/",
                     "httpOnly": bool(c.get("httpOnly", False)),
                     "secure": secure,
                     "sameSite": ss}
            # Playwright requires "expires" on every cookie:
            #   positive float = persistent cookie, -1 = session cookie
            exp_raw = c.get("expires")
            try:
                exp = float(exp_raw) if exp_raw is not None else -1
                entry["expires"] = exp if exp > 0 else -1
            except (TypeError, ValueError):
                entry["expires"] = -1
            clean_cookies.append(entry)

        # --- Reassemble chunked cookies ---
        # Browsers split large cookies into "name.0", "name.1", etc.
        # Merge them back into a single "name" cookie for Playwright.
        chunked: dict[str, list[tuple[int, dict]]] = {}
        non_chunked = []
        for c in clean_cookies:
            m = re.match(r'^(.+)\.(\d+)$', c["name"])
            if m:
                base, idx = m.group(1), int(m.group(2))
                chunked.setdefault(base, []).append((idx, c))
            else:
                non_chunked.append(c)
        for base, parts in chunked.items():
            parts.sort(key=lambda x: x[0])
            merged_value = "".join(p[1]["value"] for p in parts)
            # Use metadata from the first chunk
            merged = dict(parts[0][1])
            merged["name"] = base
            merged["value"] = merged_value
            # Only add merged if there isn't already a non-chunked cookie with same name
            if not any(c["name"] == base for c in non_chunked):
                non_chunked.append(merged)
        clean_cookies = non_chunked
        clean_origins = []
        for o in state.get("origins") or []:
            if not isinstance(o, dict): continue
            origin = o.get("origin")
            ls = o.get("localStorage") or []
            if not origin: continue
            valid_items = [{"name": str(it.get("name")), "value": str(it.get("value"))}
                           for it in ls if isinstance(it, dict) and it.get("name") and it.get("value") is not None]
            clean_origins.append({"origin": origin, "localStorage": valid_items})

        normalized = {"cookies": clean_cookies, "origins": clean_origins}

        safe = _safe_label(label)
        out_path = sessions_dir / f"{platform}_{safe}.json"
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            return jsonify({"error": f"save failed: {e}"}), 500

        if on_session_saved:
            try: on_session_saved(platform, label, out_path)
            except: pass

        logger.info(f"[{platform}] session saved: {out_path} ({len(clean_cookies)} cookies)")
        return jsonify({"ok": True, "platform": platform, "label": label,
                        "session_path": str(out_path),
                        "cookie_count": len(clean_cookies)})

    @app.route("/extension/health", methods=["GET"])
    def health():
        return jsonify({"ok": True, "service": "ai_chat_bridge_extension_server",
                        "supported_platforms": sorted(VALID_PLATFORMS)})

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({"service": "AI Chat Bridge Extension Server",
                        "endpoints": ["GET /extension/health", "POST /extension/push"]})

    return app


def run_server_in_thread(sessions_dir: Path, port: int = 5098,
                         host: str = "127.0.0.1",
                         on_session_saved=None) -> threading.Thread:
    app = make_app(sessions_dir, on_session_saved)

    def _run():
        try:
            import logging as _log
            _log.getLogger("werkzeug").setLevel(_log.WARNING)
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except Exception as e:
            logger.error(f"Extension server crashed: {e}")

    thread = threading.Thread(target=_run, daemon=True, name="ext-server")
    thread.start()
    logger.info(f"Extension server listening on http://{host}:{port}/extension/push")
    return thread