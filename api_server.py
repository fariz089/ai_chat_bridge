"""
API Server — OpenAI-compatible /v1/chat/completions endpoint.

v1.2 conversation continuity:
  - The pool keeps the browser open between requests, so subsequent
    chat completions land in the SAME Grok / ChatGPT conversation
    (the platform's own memory carries prior turns).
  - We auto-detect "new conversation" vs "continuation":
      * If incoming messages have NO assistant role → new conversation
        (navigate to home, send full prompt with system instructions)
      * Otherwise → continuation (just type the latest user message)
  - Clients can override with `bridge_options.new_chat: true|false` in
    the request body. When new_chat is forced, we navigate to home first.
  - New endpoint: POST /v1/new_chat to explicitly reset a session.

How to use with OpenClaw / any OpenAI-style client:
  - Just send normal /v1/chat/completions requests. Continuation is
    automatic — every request after the first one in a conversation
    will reuse the existing Grok chat.
  - To start a NEW conversation, either:
      * Drop assistant messages from your history (send only system+user), OR
      * Add `"bridge_options": {"new_chat": true}` to the request body, OR
      * Call POST /v1/new_chat with {"model": "grok"} first

Multimodal:
  - Vision-style content arrays accepted (see /v1/chat/completions docstring).
  - Generated images/videos are saved under ./media/ and served from /media/<file>.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import time
import uuid
import argparse
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS

from chat_engine import BridgeWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
MEDIA_DIR = BASE_DIR / "media"
UPLOADS_DIR = BASE_DIR / "uploads"

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("API_KEY", "")
# All Playwright work runs inside this worker's dedicated thread, so Flask
# request threads can safely call its methods.
bridge: BridgeWorker = None
PUBLIC_BASE_URL = ""


def init_bridge(headless: bool = True):
    global bridge
    bridge = BridgeWorker(SESSIONS_DIR, media_dir=MEDIA_DIR)
    bridge.start(headless=headless)


def check_auth():
    if not API_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token == API_KEY:
            return None
    return jsonify({"error": {"message": "Invalid API key", "type": "auth_error"}}), 401


def parse_model(model_str: str) -> tuple[str, str]:
    if ":" in model_str:
        parts = model_str.split(":", 1)
        return parts[0].lower(), parts[1]
    return model_str.lower(), "default"


# =====================================================================
# Multimodal content helpers
# =====================================================================

def _resolve_url_to_file(url: str) -> Path | None:
    """Materialize a URL (http(s), data:, file://) to a local file."""
    if not url:
        return None
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        short = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]

        if url.startswith("data:"):
            try:
                header, b64 = url.split(",", 1)
                mime = header.split(";")[0].replace("data:", "") or "application/octet-stream"
                ext = mimetypes.guess_extension(mime) or ".bin"
                out = UPLOADS_DIR / f"api_{ts}_{short}{ext}"
                out.write_bytes(base64.b64decode(b64))
                return out
            except Exception as e:
                logger.warning(f"data URL decode failed: {e}")
                return None

        if url.startswith("file://"):
            parsed = urllib.parse.urlparse(url)
            local_path = urllib.request.url2pathname(parsed.path)
            p = Path(local_path)
            if p.exists() and p.is_file():
                return p
            logger.warning(f"file URL not found: {url}")
            return None

        if url.startswith("http://") or url.startswith("https://"):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ai-chat-bridge/1.2"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    ct = resp.headers.get("Content-Type", "")
                    ext = mimetypes.guess_extension(ct.split(";")[0].strip()) if ct else None
                    if not ext:
                        parsed = urllib.parse.urlparse(url)
                        _, url_ext = os.path.splitext(parsed.path)
                        ext = url_ext or ".bin"
                    out = UPLOADS_DIR / f"api_{ts}_{short}{ext}"
                    out.write_bytes(data)
                    return out
            except Exception as e:
                logger.warning(f"http download failed for {url[:80]}: {e}")
                return None

        logger.warning(f"Unsupported URL scheme: {url[:60]}")
        return None
    except Exception as e:
        logger.warning(f"_resolve_url_to_file: {e}")
        return None


def _parse_message_content(content) -> tuple[str, list[Path]]:
    """Parse OpenAI content (string or list of parts) into (text, attachments)."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []

    texts: list[str] = []
    paths: list[Path] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str):
                    texts.append(part)
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                texts.append(part.get("text", ""))
            elif ptype in ("image_url", "input_image"):
                iu = part.get("image_url") or {}
                url = iu if isinstance(iu, str) else iu.get("url", "")
                p = _resolve_url_to_file(url)
                if p:
                    paths.append(p)
            elif ptype in ("input_file", "file"):
                url = part.get("file_url") or part.get("url") or part.get("file_data")
                if not url and isinstance(part.get("file"), dict):
                    f = part["file"]
                    url = f.get("file_url") or f.get("file_data") or f.get("url")
                if url:
                    p = _resolve_url_to_file(url)
                    if p:
                        paths.append(p)
    return ("\n".join(t for t in texts if t)).strip(), paths


def _media_to_public_url(local_path: str) -> str:
    try:
        p = Path(local_path)
        if not p.exists():
            return local_path
        try:
            rel = p.resolve().relative_to(MEDIA_DIR.resolve())
            base = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else ""
            return f"{base}/media/{rel.as_posix()}"
        except ValueError:
            return local_path
    except Exception:
        return local_path


def _format_response_with_media(text: str, media: list[dict]) -> str:
    if not media:
        return text
    parts = [text] if text else []
    parts.append("")
    for m in media:
        public = _media_to_public_url(m.get("local_path", ""))
        alt = m.get("alt") or m.get("type", "media")
        if m.get("type") == "image":
            parts.append(f"![{alt}]({public})")
        elif m.get("type") == "video":
            parts.append(f"[video: {alt}]({public})")
        else:
            parts.append(f"[file: {alt}]({public})")
    return "\n".join(parts)


# =====================================================================
# Continuation detection
# =====================================================================

def _detect_new_chat(messages: list[dict], bridge_options: dict) -> tuple[bool, str]:
    """
    Decide whether this incoming chat completion is a NEW conversation
    or a CONTINUATION of an existing one.

    Returns (is_new_chat, reason).

    Precedence:
      1. Explicit `bridge_options.new_chat` overrides everything.
      2. Heuristic: if there are no `assistant` messages in the history,
         it's the first turn → new conversation.
      3. Otherwise: continuation.
    """
    if isinstance(bridge_options, dict) and "new_chat" in bridge_options:
        val = bridge_options.get("new_chat")
        if isinstance(val, bool):
            return val, "explicit bridge_options.new_chat"
        if isinstance(val, str) and val.lower() in ("true", "false"):
            return val.lower() == "true", "explicit bridge_options.new_chat (str)"

    has_assistant = any(m.get("role") == "assistant" for m in messages)
    if not has_assistant:
        return True, "no prior assistant turns → first message"
    return False, "prior assistant turns present → continuation"


# =====================================================================
# OpenAI-compatible endpoints
# =====================================================================

@app.route("/v1/models", methods=["GET"])
def list_models():
    auth_err = check_auth()
    if auth_err:
        return auth_err

    sessions = bridge.list_available_sessions() if bridge else []
    models = []
    for s in sessions:
        model_id = f"{s['platform']}:{s['label']}" if s['label'] != 'default' else s['platform']
        models.append({"id": model_id, "object": "model",
                       "created": int(time.time()),
                       "owned_by": "ai-chat-bridge", "permission": []})

    for base in ["chatgpt", "grok"]:
        if not any(m["id"] == base or m["id"].startswith(f"{base}:") for m in models):
            models.append({"id": base, "object": "model",
                           "created": int(time.time()),
                           "owned_by": "ai-chat-bridge", "permission": []})

    return jsonify({"object": "list", "data": models})


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """
    OpenAI-compatible chat completions with multimodal + conversation continuity.

    Standard fields work as expected. The non-standard `bridge_options` field
    controls bridge behavior:
      {
        "model": "grok",
        "messages": [...],
        "bridge_options": {
          "new_chat": true,         // force start a fresh chat
          "echo_full_prompt": false  // (default false) if true, even on
                                     //   continuation we re-send full history
        }
      }
    """
    auth_err = check_auth()
    if auth_err:
        return auth_err

    body = request.get_json(silent=True) or {}
    model_str = body.get("model", "chatgpt")
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    timeout = body.get("timeout", 180)
    bridge_options = body.get("bridge_options") or {}

    if not messages:
        return jsonify({"error": {"message": "messages array required",
                                  "type": "invalid_request"}}), 400

    platform, label = parse_model(model_str)
    if platform not in ("chatgpt", "grok"):
        return jsonify({"error": {
            "message": f"Unknown model '{model_str}'. Use: chatgpt, grok, chatgpt:<label>, grok:<label>",
            "type": "invalid_request"
        }}), 400

    is_new_chat, reason = _detect_new_chat(messages, bridge_options)
    echo_full_prompt = bool(bridge_options.get("echo_full_prompt", False))
    logger.info(f"[API] {platform}:{label} — new_chat={is_new_chat} ({reason})")

    # Parse the messages, partitioning into system/history/last-user
    system_parts: list[str] = []
    history_parts: list[str] = []
    last_user_text = ""
    last_user_attachments: list[Path] = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text, attachments = _parse_message_content(content)
        is_last_user = (role == "user" and i == len(messages) - 1)

        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "user":
            if is_last_user:
                last_user_text = text
                last_user_attachments = attachments
            else:
                if text:
                    history_parts.append(f"[Previous user]: {text}")
        elif role == "assistant":
            if text:
                history_parts.append(f"[Previous AI response]: {text}")

    # Compose the prompt that we will actually type into the browser.
    # On NEW chat: include system + history + user (because Grok/ChatGPT
    #              don't yet have any context).
    # On CONTINUATION: just the user message — Grok/ChatGPT already
    #                  remember the prior turns from the live browser session.
    if is_new_chat or echo_full_prompt:
        final_message = ""
        if system_parts:
            final_message += "[System instructions]: " + " ".join(system_parts) + "\n\n"
        if history_parts and echo_full_prompt:
            # Only re-send history if explicitly asked (avoid doubling context)
            final_message += "\n".join(history_parts) + "\n\n"
        final_message += last_user_text
    else:
        # Continuation — just the new user message
        final_message = last_user_text

    logger.info(
        f"[API] {platform}:{label} — text={len(final_message)} chars, "
        f"attachments={len(last_user_attachments)}"
    )

    try:
        # BridgeWorker serializes ops internally — no external lock needed.
        result = bridge.chat(
            platform, final_message,
            label=label, timeout=timeout,
            attachments=last_user_attachments,
            force_new_chat=is_new_chat,
        )
    except FileNotFoundError as e:
        return jsonify({"error": {
            "message": str(e), "type": "invalid_request",
            "hint": "Capture a session first using the GUI or Chrome extension"
        }}), 404
    except Exception as e:
        logger.exception("Chat failed")
        return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500

    if not result.get("ok"):
        return jsonify({"error": {
            "message": result.get("error", "Unknown error"),
            "type": "server_error"
        }}), 502

    response_text = result.get("response", "")
    media = result.get("media", []) or []
    enriched_text = _format_response_with_media(response_text, media)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    media_out = [{
        "type": m.get("type"),
        "url": _media_to_public_url(m.get("local_path", "")),
        "local_path": m.get("local_path"),
        "source_url": m.get("url"),
        "alt": m.get("alt"),
    } for m in media]

    # Bridge metadata for diagnostics
    bridge_info = {
        "new_chat": is_new_chat,
        "reason": reason,
        "chat_url": result.get("chat_url", ""),
        "elapsed_ms": result.get("elapsed_ms"),
    }

    if stream:
        def generate():
            chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model_str,
                "choices": [{"index": 0,
                             "delta": {"role": "assistant", "content": enriched_text},
                             "finish_reason": None}],
                "media": media_out, "bridge": bridge_info,
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            final_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model_str,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache"})

    return jsonify({
        "id": completion_id, "object": "chat.completion",
        "created": created, "model": model_str,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": enriched_text},
            "finish_reason": "stop",
        }],
        "media": media_out,
        "bridge": bridge_info,
        "usage": {
            "prompt_tokens": len(final_message.split()),
            "completion_tokens": len(response_text.split()),
            "total_tokens": len(final_message.split()) + len(response_text.split()),
        },
    })


# =====================================================================
# Session control endpoints
# =====================================================================

@app.route("/v1/new_chat", methods=["POST"])
def force_new_chat():
    """
    Explicitly start a new conversation for a given (model, label).

    Body:  {"model": "grok"}  or  {"model": "grok:premium"}
    Returns whether a session existed and was reset.
    """
    auth_err = check_auth()
    if auth_err:
        return auth_err
    body = request.get_json(silent=True) or {}
    model_str = body.get("model", "")
    if not model_str:
        return jsonify({"error": {"message": "model required",
                                  "type": "invalid_request"}}), 400
    platform, label = parse_model(model_str)
    if platform not in ("chatgpt", "grok"):
        return jsonify({"error": {"message": f"Unknown model '{model_str}'",
                                  "type": "invalid_request"}}), 400
    # (BridgeWorker serializes internally)
    try:
        ok = bridge.start_new_chat(platform, label)
    except Exception as e:
        return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500
    return jsonify({"ok": True, "reset": ok, "platform": platform, "label": label})


@app.route("/v1/close_session", methods=["POST"])
def close_session_endpoint():
    """
    Close the browser for a (model, label). Frees memory; next request
    will re-launch the browser.
    """
    auth_err = check_auth()
    if auth_err:
        return auth_err
    body = request.get_json(silent=True) or {}
    model_str = body.get("model", "")
    if not model_str:
        return jsonify({"error": {"message": "model required"}}), 400
    platform, label = parse_model(model_str)
    # (BridgeWorker serializes internally)
    closed = bridge.close_session(platform, label)
    return jsonify({"ok": True, "closed": closed, "platform": platform, "label": label})


@app.route("/v1/active_sessions", methods=["GET"])
def list_active():
    """List browser sessions currently open (with their URLs)."""
    auth_err = check_auth()
    if auth_err:
        return auth_err
    return jsonify({"sessions": bridge.list_active_sessions() if bridge else []})


@app.route("/media/<path:filename>", methods=["GET"])
def serve_media(filename):
    target = MEDIA_DIR / filename
    try:
        target.resolve().relative_to(MEDIA_DIR.resolve())
    except Exception:
        return jsonify({"error": "invalid path"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/v1/sessions", methods=["GET"])
def list_sessions():
    auth_err = check_auth()
    if auth_err:
        return auth_err
    sessions = bridge.list_available_sessions() if bridge else []
    return jsonify({"sessions": sessions})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True, "service": "ai-chat-bridge", "version": "1.2.1",
        "features": ["text", "image-out", "video-out", "image-in", "pdf-in",
                     "video-in", "file-in", "conversation-continuity"],
        "sessions_dir": str(SESSIONS_DIR), "media_dir": str(MEDIA_DIR),
        "sessions": bridge.list_available_sessions() if bridge else [],
        "active": bridge.list_active_sessions() if bridge else [],
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "AI Chat Bridge — OpenAI-compatible API (multimodal + continuity)",
        "docs": {
            "base_url": "http://localhost:5100/v1",
            "endpoints": {
                "GET  /v1/models": "List available models",
                "POST /v1/chat/completions": "Send chat (text + attachments)",
                "POST /v1/new_chat": "Force-reset conversation for a model",
                "POST /v1/close_session": "Close a browser session",
                "GET  /v1/active_sessions": "List currently open browsers",
                "GET  /v1/sessions": "List captured login sessions",
                "GET  /media/<file>": "Fetch generated images / videos",
                "GET  /health": "Health check",
            },
            "model_format": "<platform> or <platform>:<label>",
            "examples": ["chatgpt", "grok", "chatgpt:work", "grok:premium"],
            "conversation_continuity": (
                "Subsequent requests with the same model reuse the same Grok/ChatGPT "
                "conversation. New conversation auto-detected when messages have no "
                "prior assistant turns. Force new chat with bridge_options.new_chat=true."
            ),
        },
    })


def main():
    parser = argparse.ArgumentParser(description="AI Chat Bridge API Server")
    parser.add_argument("--port", type=int, default=5100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--headless", default="true")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--public-base-url", default="")
    args = parser.parse_args()

    global API_KEY, PUBLIC_BASE_URL
    if args.api_key:
        API_KEY = args.api_key
    if args.public_base_url:
        PUBLIC_BASE_URL = args.public_base_url.rstrip("/")
    else:
        adv_host = "localhost" if args.host in ("0.0.0.0", "127.0.0.1") else args.host
        PUBLIC_BASE_URL = f"http://{adv_host}:{args.port}"

    headless = args.headless.lower() in ("true", "1", "yes")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    init_bridge(headless=headless)

    logger.info(f"Starting AI Chat Bridge API v1.2.1 on http://{args.host}:{args.port}")
    logger.info(f"OpenAI-compatible base URL: http://{args.host}:{args.port}/v1")
    logger.info(f"Public base URL (for /media): {PUBLIC_BASE_URL}")
    logger.info(f"Sessions dir: {SESSIONS_DIR}")
    logger.info(f"Media dir:    {MEDIA_DIR}")
    logger.info(f"Headless: {headless}")
    logger.info(f"Auth: {'enabled' if API_KEY else 'disabled (any key accepted)'}")
    logger.info(
        "Conversation continuity: ENABLED — sequential requests reuse the same "
        "Grok/ChatGPT chat. Use bridge_options.new_chat=true to force reset."
    )

    sessions = bridge.list_available_sessions()
    if sessions:
        logger.info(f"Available login sessions: {len(sessions)}")
        for s in sessions:
            logger.info(f"  - {s['platform']}:{s['label']}")
    else:
        logger.warning("No sessions found. Capture sessions first using the GUI.")

    import atexit
    atexit.register(lambda: bridge.shutdown(wait=True, timeout=5))

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    finally:
        bridge.shutdown(wait=True, timeout=5)


if __name__ == "__main__":
    main()
