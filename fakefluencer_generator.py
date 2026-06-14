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

import hashlib
import io
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
    if extra_notes:
        role_lines.append(f"Extra direction: {extra_notes}")

    schema = (
        "Reply with ONE valid JSON object and NOTHING else — no markdown, "
        "no backticks, no commentary. Schema:\n"
        "{\n"
        '  "product_name": "<short product name in Indonesian>",\n'
        '  "scenes": [\n'
        '    { "role": "HOOK|BODY|CTA",\n'
        '      "spoken": "<the exact words the model says, in casual Indonesian>",\n'
        '      "action": "<one short line describing what is shown on screen>" }\n'
        "  ]\n"
        "}\n"
        f"Produce EXACTLY {num_scenes} scene object(s). "
        "Scene 1 role must be HOOK; the final scene role must be CTA; "
        "any middle scenes are BODY. "
        "Each 'spoken' line must be 1-2 sentences, natural spoken Indonesian, "
        "and must be consistent in personality with the same single speaker. "
        "Do not include emojis. Do not include the brand more than necessary."
    )

    return "\n".join(role_lines) + "\n\n" + schema


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
