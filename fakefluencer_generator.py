"""
fakefluencer_generator.py
─────────────────────────
Builds a FakeFluencer asset pack (Scene_1/, Scene_2/, ... + .manifest)
that is byte-compatible with the batch_zip importer in ai_chat_bridge.py.

Pipeline
--------
1. User picks a MODE, uploads model/product photos, sets background,
   image format (aspect), number of scenes, voice profile + emotional tone.
2. build_chatgpt_prompt() turns that into ONE prompt for ChatGPT.
3. ChatGPT replies with a strict JSON block (one entry per scene).
4. parse_script_json() extracts it.
5. assemble_zip() writes Scene_N/{image.png, prompt.txt} + .manifest.

Voice consistency
------------------
Grok Imagine synthesises the voice from the prompt text, so identical
audio across renders is impossible to *guarantee*. We get as close as
possible by writing the SAME voice-spec block (profile + tone + delivery
rules), word-for-word, into every scene's prompt.txt. Only the spoken
dialogue line changes between scenes. This is the part the original
Toner_MS_Glow pack did inconsistently.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────────────────
# 0. PRODUCT IMAGE ANALYSIS  — auto-read brand + size from label
# ──────────────────────────────────────────────────────────────────────

def extract_product_info_from_image(
    product_image_path: str | Path,
    *,
    anthropic_api_key: str = "",
) -> dict:
    """Send the product photo to Claude Vision and extract brand + size.

    Returns a dict:
        {
            "brand":       str,   # e.g. "MS Glow"          (empty if not found)
            "name":        str,   # e.g. "White Cell DNA Toner"
            "full_name":   str,   # "MS Glow White Cell DNA Toner"
            "size_ml":     float | None,   # 50.0, 100.0, …  or None
            "size_label":  str,   # "50 ml" / "100 ml" / "" (for display)
            "source":      str,   # "vision" | "fallback"
            "raw_response": str,  # full Claude reply for debugging
        }

    If the API call fails or the key is missing, returns a fallback dict
    with empty strings and size_ml=None so callers can ask the user.

    The caller must pass either the ANTHROPIC_API_KEY env variable or
    supply it directly.  We do NOT import anthropic-sdk here so this
    file stays dependency-free for environments that install only requests.
    """
    import os

    api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    _FALLBACK = {
        "brand": "", "name": "", "full_name": "",
        "size_ml": None, "size_label": "",
        "source": "fallback", "raw_response": "",
    }

    if not api_key:
        return {**_FALLBACK, "raw_response": "No API key provided."}

    # Read + base64-encode the image
    try:
        img_path = Path(product_image_path)
        ext = img_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
        img_b64 = base64.b64encode(img_path.read_bytes()).decode()
    except Exception as exc:
        return {**_FALLBACK, "raw_response": f"Image read error: {exc}"}

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 256,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": img_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Look at the product label in this image. "
                        "Reply ONLY with a valid JSON object — no backticks, no commentary:\n"
                        "{\n"
                        '  "brand": "<brand / manufacturer name, e.g. MS Glow>",\n'
                        '  "product_name": "<product line + variant, e.g. White Cell DNA Toner>",\n'
                        '  "size_ml": <numeric ml value as a number, e.g. 50 — or null if not visible>\n'
                        "}\n"
                        "If you cannot read the brand, use empty string. "
                        "If size is written as g/oz, convert to ml equivalent best you can "
                        "or leave null. Do NOT include units in size_ml — numbers only."
                    ),
                },
            ],
        }],
    }

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        raw = data["content"][0]["text"].strip()
    except Exception as exc:
        return {**_FALLBACK, "raw_response": f"API error: {exc}"}

    # Parse the JSON Claude returned
    try:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        vision = json.loads(cleaned[start: end + 1])
    except Exception:
        return {**_FALLBACK, "raw_response": raw}

    brand       = str(vision.get("brand") or "").strip()
    prod_name   = str(vision.get("product_name") or "").strip()
    size_raw    = vision.get("size_ml")
    size_ml: Optional[float] = None
    try:
        if size_raw is not None:
            size_ml = float(size_raw)
    except (TypeError, ValueError):
        pass

    # Build full name: "MS Glow White Cell DNA Toner" or just product name
    full_name = f"{brand} {prod_name}".strip() if brand else prod_name

    size_label = f"{int(size_ml) if size_ml and size_ml == int(size_ml) else size_ml} ml" \
                 if size_ml else ""

    return {
        "brand": brand,
        "name": prod_name,
        "full_name": full_name,
        "size_ml": size_ml,
        "size_label": size_label,
        "source": "vision",
        "raw_response": raw,
    }


# Size → realistic physical scale guidance for Grok Imagine
# Keys are upper bounds in ml; the last entry is the catch-all.
_SIZE_SCALE_MAP = [
    (30,   "very small bottle about 3–4 cm tall, fits in a closed fist, "
           "fingertips overlap behind it"),
    (60,   "small travel-size bottle roughly the length of the model's palm "
           "(≈9–10 cm), held in one hand with fingers and thumb wrapping around it "
           "and fingertips nearly meeting"),
    (120,  "medium bottle about 12–14 cm tall, one hand can hold it but the "
           "fingers do not fully wrap around — thumb and index finger roughly "
           "parallel"),
    (250,  "standard bottle about 16–18 cm tall, comfortably held with one hand "
           "but clearly larger than the palm"),
    (500,  "large pump bottle about 20–22 cm tall, requires a firm one-hand grip "
           "or is set on a surface"),
    (9999, "big bottle, clearly larger than the hand, likely resting on a surface"),
]


def _scale_description(size_ml: Optional[float]) -> str:
    """Return a plain-English scale sentence for the given volume."""
    if size_ml is None:
        # No size info — use a safe generic anchor
        return (
            "The product is a standard small cosmetic bottle. "
            "Scale it so it fits naturally in one hand — the bottle height "
            "should be roughly equal to the model's palm length. "
            "Keep proportions realistic; do NOT make the bottle oversized."
        )
    for limit, description in _SIZE_SCALE_MAP:
        if size_ml <= limit:
            return (
                f"This is a {int(size_ml) if size_ml == int(size_ml) else size_ml} ml product "
                f"({description}). "
                f"Scale the bottle accordingly — keep proportions realistic "
                f"relative to the model's hand and face."
            )
    return (
        f"This is a {size_ml} ml product (very large container). "
        "Scale the bottle so it looks realistic relative to the model's hand."
    )


# ──────────────────────────────────────────────────────────────────────
# 1. CATALOGUES  (mirror the web UI dropdowns in your screenshots)
# ──────────────────────────────────────────────────────────────────────

# What each mode requires the user to upload.
#   "model"   -> a person/face photo is required
#   "product" -> a product photo is required
MODES: dict[str, dict] = {
    "ugc": {
        "label": "Mode UGC",
        "needs": ("model", "product"),
        "camera": "Handheld iPhone front camera, slight natural shake, candid framing.",
        "style": "Authentic user-generated content, looks natural, optimised for TikTok/Reels.",
    },
    "pov_hand": {
        "label": "POV Hand Review",
        "needs": ("product",),
        "camera": "First-person POV, product held in hand, natural hand physics, close framing.",
        "style": "Immersive first-person unboxing / review perspective.",
    },
    "mirror": {
        "label": "Mirror Check",
        "needs": ("model", "product"),
        "camera": "Mirror selfie framing, phone visible, aesthetic studio lighting.",
        "style": "Aesthetic mirror-selfie fashion / lifestyle promo, premium studio light.",
    },
    "basemodel": {
        "label": "Basemodel Creator",
        "needs": ("model",),
        "camera": "Clean studio multi-angle character sheet, neutral background.",
        "style": "Consistent digital character base for repeatable content.",
    },
}

# Voice profiles -> a stable, fully-specified description that we paste
# verbatim into every scene. The KEY is what the GUI stores; the VALUE
# is what Grok actually reads.
VOICE_PROFILES: dict[str, str] = {
    "ayu_wanita":  "Female Indonesian voice, warm mid-range timbre, youthful, clear diction (profile: Ayu).",
    "bima_pria":   "Male Indonesian voice, calm mid-low timbre, friendly, clear diction (profile: Bima).",
    "sari_wanita": "Female Indonesian voice, bright energetic timbre, expressive (profile: Sari).",
    "dimas_pria":  "Male Indonesian voice, deep confident timbre, measured pace (profile: Dimas).",
}

# Emotional tone -> a delivery instruction. Matches your "NADA EMOSIONAL" list.
EMOTIONAL_TONES: dict[str, str] = {
    "profesional": "Tone: professional, composed, trustworthy.",
    "antusias":    "Tone: enthusiastic, upbeat, excited but natural.",
    "meyakinkan":  "Tone: persuasive, confident, reassuring.",
    "santai":      "Tone: relaxed, casual, conversational.",
    "edukatif":    "Tone: educational, clear, explanatory.",
    "humor":       "Tone: light, playful, humorous.",
    "inspiratif":  "Tone: inspiring, warm, motivating.",
    "serius":      "Tone: serious, focused, no-nonsense.",
    "sedih":       "Tone: soft, melancholic, gentle.",
    "marah":       "Tone: firm, intense, assertive.",
    "ketakutan":   "Tone: tense, urgent, anxious.",
    "mencekam":    "Tone: suspenseful, dramatic, low and tense.",
    "tertawa":     "Tone: cheerful, laughing, joyful.",
}

# Image format / aspect -> passthrough to Grok Imagine. These strings
# match the aspect picker the engine already understands.
ASPECTS = ("9:16", "2:3", "1:1", "3:2", "16:9")

BUILDER_NAME = "FakeFluencer"
BUILDER_VENDOR = "Inaten Digital"


# ──────────────────────────────────────────────────────────────────────
# 2. THE LOCKED VOICE-SPEC BLOCK  (the key to consistency)
# ──────────────────────────────────────────────────────────────────────

def voice_spec_block(voice_key: str, tone_key: str) -> str:
    """The identical block that goes into EVERY scene's prompt.txt.

    Keeping this byte-identical across scenes is what makes the synthesized
    voice sound as consistent as Grok Imagine allows.
    """
    voice = VOICE_PROFILES.get(voice_key, VOICE_PROFILES["ayu_wanita"])
    tone = EMOTIONAL_TONES.get(tone_key, EMOTIONAL_TONES["antusias"])
    return (
        "VOICE-SPEC (KEEP IDENTICAL ACROSS ALL SCENES):\n"
        f"  {voice}\n"
        f"  {tone}\n"
        "  Delivery: same speaker, same pacing and pitch in every scene; "
        "do NOT change accent, gender, or vocal age between scenes; "
        "natural breathing, conversational rhythm, no robotic cadence."
    )


# ──────────────────────────────────────────────────────────────────────
# 3. BUILD THE PROMPT WE SEND TO CHATGPT
# ──────────────────────────────────────────────────────────────────────

def build_chatgpt_prompt(
    *,
    mode: str,
    num_scenes: int,
    background: str,
    voice_key: str,
    tone_key: str,
    aspect: str,
    product_name: str = "",
    extra_notes: str = "",
) -> str:
    """Produce the single instruction we type into ChatGPT.

    We ask ChatGPT ONLY for the creative script (per-scene spoken lines +
    a one-line scene action). Everything deterministic (camera, voice spec,
    environment, aspect) we add ourselves in assemble_zip so it can never
    drift. ChatGPT must answer with strict JSON.
    """
    m = MODES.get(mode, MODES["ugc"])
    needs = m["needs"]
    voice = VOICE_PROFILES.get(voice_key, VOICE_PROFILES["ayu_wanita"])
    tone = EMOTIONAL_TONES.get(tone_key, EMOTIONAL_TONES["antusias"])

    role_lines = [
        f"You are a short-form video scriptwriter for {m['label']} content.",
        f"Style: {m['style']}",
        f"Background/environment for every scene: {background}",
        f"Speaker voice: {voice}",
        f"Emotional {tone}",
        f"Target format/aspect: {aspect}.",
    ]
    if product_name:
        role_lines.append(f"Product being featured: {product_name}.")
    if "product" in needs:
        role_lines.append(
            "IMPORTANT: a product photo is attached. Read the brand, the "
            "product name/variant, and the size in ml DIRECTLY off the label "
            "in that photo, and report them in the JSON fields brand / "
            "product_name / size_ml. If a value is not legible, use an empty "
            "string (or null for size_ml) — do not guess.")
    if extra_notes:
        role_lines.append(f"Extra direction: {extra_notes}")

    # Structure guidance MUST match the requested scene count. The old text
    # always demanded "Scene 1 = HOOK, last = CTA", which implicitly forced a
    # 3-beat ad even when num_scenes==1 — so the model returned 3 scenes.
    if num_scenes <= 1:
        structure_clause = (
            "Produce EXACTLY 1 scene object — a single, self-contained scene "
            'with role "CTA" that hooks, shows the product, and ends with a '
            "call to action in one short beat. Do NOT split it into multiple "
            "scenes under any circumstances; the scenes array must have length 1."
        )
    else:
        structure_clause = (
            f"Produce EXACTLY {num_scenes} scene objects — no more, no fewer. "
            "Scene 1 role must be HOOK; the final scene role must be CTA; "
            "any middle scenes are BODY."
        )

    schema = (
        "Reply with ONE valid JSON object and NOTHING else — no markdown, "
        "no backticks, no commentary. Schema:\n"
        "{\n"
        '  "brand": "<brand/manufacturer on the product label, e.g. MS Glow; \\"\\" if none/illegible>",\n'
        '  "product_name": "<product line + variant in Indonesian, e.g. White Cell DNA Toner>",\n'
        '  "size_ml": <numeric ml from the label as a number, e.g. 50 — or null if not visible>,\n'
        '  "scenes": [\n'
        '    { "role": "HOOK|BODY|CTA",\n'
        '      "spoken": "<the exact words the model says, in casual Indonesian>",\n'
        '      "action": "<one short line describing what is shown on screen>" }\n'
        "  ]\n"
        "}\n"
        + structure_clause + " "
        "Each 'spoken' line must be 1-2 sentences, natural spoken Indonesian, "
        "and must be consistent in personality with the same single speaker. "
        "Do not include emojis. Do not include the brand more than necessary."
    )

    return "\n".join(role_lines) + "\n\n" + schema


def build_scene_image_prompt(
    *,
    mode: str,
    scene_action: str,
    spoken: str,
    background: str,
    aspect: str,
    product_name: str = "",
    product_size_ml: Optional[float] = None,
) -> str:
    """Prompt for Grok Imagine (IMAGE mode) that composes ONE still per scene.

    The uploaded model photo(s) and product photo(s) are attached as
    references; this text tells Imagine to MERGE them into a single
    photoreal frame that matches the scene's on-screen action. That merged
    still is what each Scene_N/image.png should contain (like the original
    Toner_MS_Glow pack, where every scene has a distinct composited image).

    product_name    — full "Brand + Product" string, e.g. "MS Glow White Cell
                      DNA Toner". Set automatically from extract_product_info_from_image()
                      or entered manually by the user.
    product_size_ml — actual volume in ml (e.g. 50.0, 100.0). Used to generate
                      realistic scale guidance via _scale_description(). If None,
                      a safe generic anchor is used.
    """
    m = MODES.get(mode, MODES["ugc"])
    needs = m["needs"]
    refs = []
    if "model" in needs:
        refs.append("the SAME person from the attached model photo (keep her "
                    "face, hairstyle and skin identical)")
    if "product" in needs:
        refs.append("the EXACT product from the attached product photo "
                    "(keep the bottle shape, label and text identical)")
    ref_clause = " and ".join(refs) if refs else "the attached reference photo"

    prod = f" The product is {product_name}." if product_name else ""

    # Scale guidance — dynamically built from actual ml size.
    # _scale_description() maps volume → physical hand/face anchor so every
    # product (50 ml travel toner, 100 ml serum, 250 ml shampoo …) gets the
    # right proportions instead of a hardcoded "50 ml" anchor.
    scale_clause = ""
    if "product" in needs:
        scale_clause = " IMPORTANT product scale: " + _scale_description(product_size_ml)

    negatives = "no on-screen captions, no watermark, no extra people"
    if "product" in needs:
        negatives += (
            ", no oversized or giant bottle, bottle not larger than realistic "
            "for its volume, no distorted or unrealistic product scale"
        )

    return (
        f"Create a single photoreal vertical image. Combine {ref_clause} into "
        f"one natural frame.{prod}{scale_clause} "
        f"Camera: {m['camera']} "
        f"Setting/background: {background}. "
        f"On-screen action for this exact moment: {scene_action} "
        f"The model is mid-sentence saying (do not render text, just match her "
        f"expression): \"{spoken}\". "
        f"Aspect ratio {aspect}. Authentic UGC look, soft natural lighting, "
        f"{negatives}."
    )


# ──────────────────────────────────────────────────────────────────────
# 4. PARSE CHATGPT'S REPLY
# ──────────────────────────────────────────────────────────────────────

def parse_script_json(reply_text: str) -> dict:
    """Extract the JSON object from ChatGPT's reply, tolerant of stray text."""
    if not reply_text:
        raise ValueError("Empty reply from ChatGPT.")

    # Strip code fences if present.
    cleaned = re.sub(r"```(?:json)?", "", reply_text).strip()

    # Grab the outermost {...}.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in ChatGPT reply.")

    blob = cleaned[start : end + 1]
    data = json.loads(blob)

    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("JSON has no 'scenes' array.")
    for i, s in enumerate(scenes, 1):
        if "spoken" not in s or not str(s["spoken"]).strip():
            raise ValueError(f"Scene {i} is missing a 'spoken' line.")
    return data


# ──────────────────────────────────────────────────────────────────────
# 5. WRITE A SINGLE prompt.txt  (same shape as the Toner pack, plus voice)
# ──────────────────────────────────────────────────────────────────────

def render_prompt_txt(
    *,
    scene_num: int,
    spoken: str,
    action: str,
    camera: str,
    environment: str,
    aspect: str,
    voice_block: str,
) -> str:
    """One scene's prompt.txt. The voice_block is identical for every scene."""
    # Escape single quotes inside the spoken line so the batch_zip regex
    # (which reads speaking: '...') still captures it cleanly.
    safe_spoken = spoken.replace("'", "\u2019").strip()
    action_line = f"ACTION: {action.strip()}\n" if action and action.strip() else ""
    return (
        f"SCENE {scene_num} PROMPT\n\n"
        "PROMPT: \n"
        f"CAMERA: {camera}\n"
        f"CONTEXT: LIP-SYNC: Model is speaking: '{safe_spoken}'.\n"
        f"{action_line}"
        f"ENVIRONMENT: {environment}\n"
        f"FORMAT: {aspect}\n"
        f"{voice_block}\n\n"
        f"Generated via {BUILDER_NAME} \u2014 by {BUILDER_VENDOR}"
    )


# ──────────────────────────────────────────────────────────────────────
# 6. ASSEMBLE THE ZIP
# ──────────────────────────────────────────────────────────────────────

def _normalise_image(src: Path, dest: Path):
    """Copy an uploaded image to dest as PNG. Uses Pillow if available,
    otherwise copies bytes as-is (Grok accepts png/jpg/webp anyway)."""
    try:
        from PIL import Image  # optional dep, already in requirements
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.save(dest, format="PNG")
            return
    except Exception:
        pass
    shutil.copyfile(src, dest)


def assemble_zip(
    *,
    out_dir: Path,
    project_name: str,
    script: dict,
    mode: str,
    background: str,
    aspect: str,
    voice_key: str,
    tone_key: str,
    scene_images: dict[int, Path],
) -> Path:
    """Build <project_name>_all_assets.zip in out_dir and return its path.

    scene_images maps scene_num -> the image file to embed for that scene
    (the model/product reference Grok will animate). If a scene has no
    dedicated image, the closest lower-numbered image is reused.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    m = MODES.get(mode, MODES["ugc"])
    camera = m["camera"]
    voice_block = voice_spec_block(voice_key, tone_key)
    scenes = script["scenes"]

    safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", project_name).strip("_") or "Project"
    root_folder = f"{safe_name}_assets"

    # Staging dir
    stage = out_dir / f"_stage_{safe_name}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / root_folder).mkdir(parents=True)

    fallback_img: Optional[Path] = None
    for n in sorted(scene_images):
        fallback_img = scene_images[n]
        break

    for idx, s in enumerate(scenes, start=1):
        scene_dir = stage / root_folder / f"Scene_{idx}"
        scene_dir.mkdir(parents=True, exist_ok=True)

        img_src = scene_images.get(idx, fallback_img)
        if img_src and Path(img_src).exists():
            _normalise_image(Path(img_src), scene_dir / "image.png")

        ptxt = render_prompt_txt(
            scene_num=idx,
            spoken=str(s.get("spoken", "")),
            action=str(s.get("action", "")),
            camera=camera,
            environment=background,
            aspect=aspect,
            voice_block=voice_block,
        )
        (scene_dir / "prompt.txt").write_text(ptxt, encoding="utf-8")

    # Manifest (same vibe as the original pack)
    fingerprint = hashlib.sha256(
        f"{safe_name}|{mode}|{voice_key}|{tone_key}|{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()
    manifest = (
        f"{BUILDER_NAME} Build Manifest\n"
        f"Built by: {BUILDER_VENDOR}\n"
        f"Mode: {m['label']}\n"
        f"Voice: {voice_key} | Tone: {tone_key} | Aspect: {aspect}\n"
        f"Scenes: {len(scenes)}\n"
        f"Fingerprint: {fingerprint}\n"
        f"Generated: {datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}"
    )
    (stage / root_folder / ".manifest").write_text(manifest, encoding="utf-8")

    # Zip it
    zip_path = out_dir / f"{safe_name}_all_assets.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted((stage / root_folder).rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(stage))

    shutil.rmtree(stage, ignore_errors=True)
    return zip_path


# ──────────────────────────────────────────────────────────────────────
# 7. CONVENIENCE: validate uploads against the chosen mode
# ──────────────────────────────────────────────────────────────────────

def validate_uploads(mode: str, *, has_model: bool, has_product: bool) -> Optional[str]:
    """Return an error string if required uploads are missing, else None."""
    needs = MODES.get(mode, MODES["ugc"])["needs"]
    if "model" in needs and not has_model:
        return f"{MODES[mode]['label']} membutuhkan foto MODEL."
    if "product" in needs and not has_product:
        return f"{MODES[mode]['label']} membutuhkan foto PRODUK."
    return None
