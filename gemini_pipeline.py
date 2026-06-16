"""
gemini_pipeline.py
──────────────────
"One model" path for the FakeFluencer generator: the SAME provider (Google
Gemini) writes the storyboard JSON *and* renders every scene still, instead of
splitting script (ChatGPT/Grok) from images (Grok Imagine).

Why this exists
---------------
The user asked for script + image "jadi satu" (combined). Gemini's image
models ("Nano Banana") are multimodal: they accept reference images as input
and return generated images as output via the SAME generateContent endpoint
that also returns text. So we can:

  1. generate_script()      -> attach model+product photos, get strict JSON
                               (scenes + brand/product/size read off the label)
  2. generate_scene_image() -> attach the SAME photos + the scene prompt,
                               get back a composited photoreal still

Everything here uses only the stdlib (urllib) so it works in environments that
only have `requests` or nothing extra installed.

Important runtime notes
-----------------------
* Image generation requires a PAID Gemini API key (billing enabled). The free
  tier quota for the image models is 0 — text/script may work on free tier but
  the stills will fail without billing.
* Model IDs below are the current (2026) Nano Banana family. They are PREVIEW
  strings and Google renames them periodically — if a call 404s on the model,
  update the constants here.
      - gemini-3.1-flash-image-preview  → "Nano Banana 2"  (fast, cheaper) ← default
      - gemini-3-pro-image-preview      → "Nano Banana Pro" (4K, best text)
      - gemini-2.5-flash-image          → original Nano Banana
* responseModalities MUST include both TEXT and IMAGE or the request can fail.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ── Configurable model IDs (update if Google renames the previews) ──────
GEMINI_TEXT_MODEL = "gemini-2.5-flash"               # script / JSON (reads photos)
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image-preview"  # Nano Banana 2 (stills)
GEMINI_IMAGE_SIZE = "2K"                             # "1K" | "2K" | "4K" (K uppercase!)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
         "png": "image/png", "webp": "image/webp"}


def _image_part(path: str | Path) -> dict:
    """Read an image file into a Gemini inline_data part (base64)."""
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    mime = _MIME.get(ext, "image/jpeg")
    data = base64.b64encode(p.read_bytes()).decode()
    return {"inline_data": {"mime_type": mime, "data": data}}


def _post(model: str, body: dict, api_key: str, timeout: int = 120) -> dict:
    """POST to {model}:generateContent and return the parsed JSON response.
    Raises RuntimeError with a readable message on HTTP / network errors."""
    url = f"{_BASE}/{model}:generateContent"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:400]
        except Exception:
            detail = ""
        raise RuntimeError(f"Gemini HTTP {e.code}: {detail or e.reason}")
    except Exception as e:
        raise RuntimeError(f"Gemini request failed: {e}")


def _parts_from_response(resp: dict) -> list:
    try:
        return resp["candidates"][0]["content"]["parts"] or []
    except (KeyError, IndexError, TypeError):
        return []


# ──────────────────────────────────────────────────────────────────────
# 1. SCRIPT  (text/JSON, with the product photo attached so Gemini can
#    read the brand / variant / size straight off the label)
# ──────────────────────────────────────────────────────────────────────

def generate_script(*, api_key: str, prompt_text: str,
                    ref_image_paths: list[str | Path],
                    timeout: int = 120) -> str:
    """Return Gemini's raw reply text for the storyboard brief.

    The caller parses it with fakefluencer_generator.parse_script_json().
    Reference photos (model + product) are attached so the JSON's
    brand/product_name/size_ml fields reflect the actual product.
    """
    if not api_key:
        raise RuntimeError("Gemini API key kosong.")

    parts: list = [{"text": prompt_text}]
    for p in (ref_image_paths or []):
        try:
            parts.append(_image_part(p))
        except Exception:
            pass  # skip unreadable image, keep going

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.9},
    }
    resp = _post(GEMINI_TEXT_MODEL, body, api_key, timeout=timeout)
    text = "".join(part.get("text", "")
                   for part in _parts_from_response(resp)
                   if isinstance(part, dict) and "text" in part).strip()
    if not text:
        raise RuntimeError("Gemini tidak mengembalikan teks skrip.")
    return text


# ──────────────────────────────────────────────────────────────────────
# 2. SCENE IMAGE  (multimodal: same photos in, generated still out)
# ──────────────────────────────────────────────────────────────────────

def generate_scene_image(*, api_key: str, prompt_text: str,
                         ref_image_paths: list[str | Path],
                         aspect: str, out_path: str | Path,
                         image_model: Optional[str] = None,
                         timeout: int = 180) -> Path:
    """Generate ONE composited still and write it to out_path. Returns the
    Path on success, raises RuntimeError on failure (caller falls back to the
    raw uploaded photo)."""
    if not api_key:
        raise RuntimeError("Gemini API key kosong.")

    model = image_model or GEMINI_IMAGE_MODEL
    parts: list = [{"text": prompt_text}]
    for p in (ref_image_paths or []):
        try:
            parts.append(_image_part(p))
        except Exception:
            pass

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            # Both modalities are required for the Nano Banana models.
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect or "9:16",
                "imageSize": GEMINI_IMAGE_SIZE,
            },
        },
    }
    resp = _post(model, body, api_key, timeout=timeout)

    for part in _parts_from_response(resp):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(inline["data"]))
            return out

    # No image came back — surface any text the model returned (often a refusal
    # or a "billing required" style note) to make debugging obvious.
    txt = "".join(p.get("text", "") for p in _parts_from_response(resp)
                  if isinstance(p, dict) and "text" in p).strip()
    raise RuntimeError(
        "Gemini tidak mengembalikan gambar"
        + (f" (balasan: {txt[:120]})" if txt else
           " — cek apakah billing API aktif untuk model gambar."))


def quick_key_check(api_key: str) -> Optional[str]:
    """Cheap validation: returns None if the key can reach a text model,
    else an error string. Used to fail fast before a long run."""
    if not api_key:
        return "API key kosong."
    try:
        body = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 1}}
        _post(GEMINI_TEXT_MODEL, body, api_key, timeout=30)
        return None
    except Exception as e:
        return str(e)
