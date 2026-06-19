"""
FakeFluencer V2 — Web UI (unified CDP + Chrome-profile architecture).

Every account is a Chrome profile reached over CDP. Add an account = add a
profile (one Chrome, one CDP port). The Chrome processes are managed by
chrome_supervisor.py (in the `chrome` container); this web app connects to
them with Playwright over CDP — it never launches its own browser and never
needs cookie session files.

Env:
    CDP_HOST      hostname where the Chrome CDP ports live (default 127.0.0.1;
                  in docker compose this is the chrome service name)
    PROFILES_JSON path to profiles.json (default <root>/profiles.json)
    PROFILES_DIR  path to the profiles folder (default <root>/profiles)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, stream_with_context)
from flask_cors import CORS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fakefluencer_generator as ffgen          # noqa: E402
from profiles import ProfileStore               # noqa: E402
from webapp.bridge_registry import BridgeRegistry  # noqa: E402
from webapp.pool_router import (                # noqa: E402
    ProfileRouter, NoProfileAvailable, LeaseTimeout,
)
from webapp.jobs import manager as jobs         # noqa: E402

try:
    import live_module                          # noqa: E402
    LIVE_IMPORT_ERROR = None
except Exception as e:                          # noqa: BLE001
    live_module = None
    LIVE_IMPORT_ERROR = str(e)

VERSION = "2.0.0-cdp"
PLATFORMS = ("chatgpt", "grok", "gemini", "aistudio")
PLATFORM_LABELS = {"chatgpt": "ChatGPT", "grok": "Grok",
                   "gemini": "Gemini", "aistudio": "AI Studio"}
CHAT_PLATFORMS = ("chatgpt", "grok", "gemini")

CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
SESSIONS_DIR = PROJECT_ROOT / "sessions"
MEDIA_DIR = PROJECT_ROOT / "media"
GENERATED_DIR = MEDIA_DIR / "generated"
UPLOADS_DIR = PROJECT_ROOT / "uploads"
WEB_UPLOADS_DIR = UPLOADS_DIR / "web"
CONFIG_PATH = PROJECT_ROOT / "ai_chat_bridge_config.json"
PROFILES_JSON = Path(os.environ.get("PROFILES_JSON", PROJECT_ROOT / "profiles.json"))
PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", PROJECT_ROOT / "profiles"))

for d in (SESSIONS_DIR, MEDIA_DIR, GENERATED_DIR, WEB_UPLOADS_DIR, PROFILES_DIR):
    d.mkdir(parents=True, exist_ok=True)

store = ProfileStore(PROFILES_JSON, PROFILES_DIR)
registry = BridgeRegistry(SESSIONS_DIR, MEDIA_DIR, CDP_HOST)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def cdp_probe(port: int) -> dict:
    base = f"http://{CDP_HOST}:{port}"
    out = {"reachable": False, "tabs": [], "version": None}
    try:
        with urllib.request.urlopen(f"{base}/json/version", timeout=3) as r:
            out["version"] = json.loads(r.read().decode()).get("Browser")
            out["reachable"] = True
    except Exception:  # noqa: BLE001
        return out
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=3) as r:
            tabs = json.loads(r.read().decode())
            out["tabs"] = [{"url": t.get("url", ""), "title": t.get("title", "")}
                           for t in tabs if t.get("type") == "page"]
    except Exception:  # noqa: BLE001
        pass
    return out


LOGIN_HINTS = {
    "chatgpt": ("chatgpt.com", "chat.openai.com"),
    "grok": ("grok.com", "x.com/i/grok"),
    "gemini": ("gemini.google.com",),
    "aistudio": ("ai.studio", "aistudio.google.com"),
}
SIGNIN_HINTS = ("accounts.google.com", "signin", "login", "auth")


def infer_login(platform: str, tabs: list[dict]) -> str:
    domains = LOGIN_HINTS.get(platform, ())
    on_platform = False
    for t in tabs:
        url = (t.get("url") or "").lower()
        if any(d in url for d in domains):
            if any(s in url for s in SIGNIN_HINTS):
                return "signin"
            on_platform = True
    return "ready" if on_platform else "unknown"


def _profile_healthy(profile) -> bool:
    """True if a profile's Chrome is reachable AND looks logged in.

    Used by the load-balancing router to skip dead or signed-out accounts.
    A profile is usable if its CDP endpoint answers and it isn't sitting on
    a sign-in page. 'unknown' (no platform tab yet) is treated as usable
    because a freshly-booted Chrome may not have navigated yet — the engine
    opens the tab on first use.
    """
    probe = cdp_probe(profile.port)
    if not probe.get("reachable"):
        return False
    state = infer_login(profile.platform, probe.get("tabs", []))
    return state in ("ready", "unknown")


# One process-wide router that load-balances + queues across all accounts of
# each platform. Manual profile picks still work (passed as prefer_id).
router = ProfileRouter(store, registry, health_check=_profile_healthy)


def _parse_size_ml(raw) -> "float | None":
    """Normalise a size value to ml (float) or None. Mirrors the desktop
    _parse_size: accepts '50', '50ml', '50,5', numbers, blanks."""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw).strip().lower().replace("ml", "").strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)
_api_proc: subprocess.Popen | None = None


@app.route("/")
@app.route("/dashboard")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    profs = store.list()
    reachable = sum(1 for p in profs if cdp_probe(p.port)["reachable"])
    all_jobs = jobs.list()
    running = [j for j in all_jobs if j["status"] in ("running", "awaiting_input")]
    return jsonify({
        "ok": True, "version": VERSION, "cdp_host": CDP_HOST,
        "profiles_total": len(profs), "profiles_reachable": reachable,
        "workers_active": len(registry.active_ids()),
        "jobs_total": len(all_jobs), "jobs_active": len(running),
        "api_server_running": _api_proc is not None and _api_proc.poll() is None,
        "pool": router.snapshot(),
    })


@app.route("/api/pool")
def api_pool():
    """Per-platform account availability + busy/queue state for the UI."""
    router.invalidate_health()  # force a fresh probe pass for an accurate view
    return jsonify({"ok": True, "pool": router.snapshot()})


@app.route("/api/profiles")
def api_profiles():
    out = []
    for p in store.list():
        probe = cdp_probe(p.port)
        out.append({
            "id": p.id, "platform": p.platform, "label": p.label, "port": p.port,
            "platform_label": PLATFORM_LABELS.get(p.platform, p.platform),
            "reachable": probe["reachable"], "version": probe["version"],
            "login": infer_login(p.platform, probe["tabs"]) if probe["reachable"] else "offline",
            "tabs": probe["tabs"][:5],
        })
    return jsonify({"profiles": out,
                    "platforms": [{"key": k, "label": PLATFORM_LABELS[k]} for k in PLATFORMS]})


@app.route("/api/profiles/add", methods=["POST"])
def api_profile_add():
    body = request.get_json(silent=True) or {}
    platform = body.get("platform", "")
    label = (body.get("label") or "default").strip()
    if platform not in PLATFORMS:
        return jsonify({"ok": False, "error": f"Platform '{platform}' tidak dikenal"}), 400
    try:
        prof = store.add(platform, label)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _write_supervisor_config()
    return jsonify({"ok": True, "profile": {"id": prof.id, "platform": prof.platform,
                    "label": prof.label, "port": prof.port},
                    "note": ("Profil ditambahkan. (Re)start service chrome agar Chrome "
                             "baru berjalan, lalu login lewat noVNC.")})


@app.route("/api/profiles/remove", methods=["POST"])
def api_profile_remove():
    body = request.get_json(silent=True) or {}
    pid = body.get("id", "")
    if not store.get(pid):
        return jsonify({"ok": False, "error": "Profil tidak ada"}), 404
    registry.close(pid)
    store.remove(pid)
    _write_supervisor_config()
    return jsonify({"ok": True, "note": "Profil dihapus dari daftar. Folder profil "
                    "di disk tidak dihapus otomatis."})


@app.route("/api/profiles/test", methods=["POST"])
def api_profile_test():
    body = request.get_json(silent=True) or {}
    prof = store.get(body.get("id", ""))
    if not prof:
        return jsonify({"ok": False, "error": "Profil tidak ada"}), 404
    if prof.platform not in CHAT_PLATFORMS:
        return jsonify({"ok": False, "error": f"{prof.platform} tidak mendukung tes chat"}), 400
    job = jobs.run("test", f"Tes {prof.id}", lambda j: _test_worker(j, prof))
    return jsonify({"ok": True, "job_id": job.id})


def _test_worker(job, prof):
    job.log(f"\U0001F50C Menyambung ke {prof.id} via CDP ({prof.cdp_url(CDP_HOST)})\u2026")
    if not cdp_probe(prof.port)["reachable"]:
        raise RuntimeError("Chrome profil ini tidak terjangkau. Pastikan service "
                           "chrome jalan dan profil sudah login.")
    worker = registry.worker_for(prof)
    job.log("\u2192 Mengirim pesan tes\u2026")
    r = worker.chat(prof.platform, "Reply with exactly: OK",
                    label=prof.label, timeout=120, force_new_chat=True)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "Tes gagal"))
    job.log("\u2713 Bridge OK. Balasan: " + (r.get("response", "")[:120] or "(kosong)"))
    job.result = {"ok": True, "response": r.get("response", "")}


@app.route("/api/generator/options")
def generator_options():
    modes = [{"key": k, "label": v["label"], "needs": list(v["needs"]),
              "live": bool(v.get("live"))} for k, v in ffgen.MODES.items()]
    profs = store.list()
    return jsonify({
        "modes": modes, "aspects": list(ffgen.ASPECTS),
        "voices": list(ffgen.VOICE_PROFILES.keys()),
        "tones": list(ffgen.EMOTIONAL_TONES.keys()),
        "script_profiles": [_pdict(p) for p in profs],
        "image_profiles": [_pdict(p) for p in profs if p.platform in ("grok", "gemini")],
    })


def _pdict(p):
    return {"id": p.id, "platform": p.platform, "label": p.label,
            "name": f"{PLATFORM_LABELS.get(p.platform, p.platform)} : {p.label}"}


@app.route("/api/upload", methods=["POST"])
def upload():
    saved = []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        safe = "".join(c for c in f.filename if c.isalnum() or c in "._- ")
        dest = WEB_UPLOADS_DIR / f"{int(time.time()*1000)}_{safe}"
        f.save(dest)
        saved.append({"path": str(dest), "preview": f"/uploads/web/{dest.name}"})
    return jsonify({"ok": True, "paths": [s["path"] for s in saved], "files": saved})


@app.route("/uploads/web/<path:filename>")
def serve_upload(filename):
    target = (WEB_UPLOADS_DIR / filename).resolve()
    try:
        target.relative_to(WEB_UPLOADS_DIR.resolve())
    except ValueError:
        return jsonify({"error": "invalid path"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(WEB_UPLOADS_DIR, filename)


@app.route("/api/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "ugc")
    model_imgs = [Path(p) for p in body.get("model_imgs", []) if Path(p).exists()]
    product_imgs = [Path(p) for p in body.get("product_imgs", []) if Path(p).exists()]
    err = ffgen.validate_uploads(mode, has_model=bool(model_imgs),
                                 has_product=bool(product_imgs))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    # ── Account selection ───────────────────────────────────────────────
    # Two ways to choose, per role (script / image / render):
    #   • Specific account  → send <role>_profile_id (manual pick; the router
    #     still queues if that exact account is busy, never reroutes silently).
    #   • Auto / balance     → send <role>_platform ("grok"|"gemini"|...) and
    #     leave the id blank; the router picks the least-busy healthy account
    #     of that platform, queueing if all are busy.
    # Backward compatible: an existing UI that only sends *_profile_id keeps
    # working — we derive the platform from the picked profile.
    def _resolve_role(id_key, plat_key, *, required=True, default_from=None):
        pid = (body.get(id_key) or "").strip()
        prof = store.get(pid) if pid else None
        if prof is not None:
            return {"platform": prof.platform, "prefer_id": prof.id}
        plat = (body.get(plat_key) or "").strip().lower()
        if plat:
            return {"platform": plat, "prefer_id": None}
        if default_from is not None:
            return dict(default_from)
        if required:
            return None
        return None

    script_sel = _resolve_role("script_profile_id", "script_platform")
    image_sel = _resolve_role("image_profile_id", "image_platform")
    if script_sel is None:
        return jsonify({"ok": False, "error": "Pilih profil/platform penyedia skrip"}), 400
    if image_sel is None:
        return jsonify({"ok": False, "error": "Pilih profil/platform pembuat gambar"}), 400
    # Render falls back to the image selection when not specified (old behaviour).
    render_sel = _resolve_role("render_profile_id", "render_platform",
                               required=False, default_from=image_sel)

    # Validate platforms exist in config (fail early with a clear message).
    for label, sel in (("skrip", script_sel), ("gambar", image_sel),
                       ("render", render_sel)):
        if not router.has_capacity(sel["platform"]):
            return jsonify({"ok": False,
                            "error": f"Tidak ada akun {sel['platform']} siap untuk {label} "
                                     f"(tambah/ login akun di Bank)."}), 400

    params = {
        "mode": mode, "num_scenes": max(1, min(10, int(body.get("num_scenes", 2)))),
        "output_mode": body.get("output_mode", "video"),
        "project_name": (body.get("project_name") or "Project").strip(),
        "aspect": body.get("aspect", "9:16"),
        "voice_key": body.get("voice_key", list(ffgen.VOICE_PROFILES)[0]),
        "tone_key": body.get("tone_key", "antusias"),
        "background": (body.get("background") or "").strip(),
        "video_duration": "10s" if str(body.get("video_duration", "")).startswith("10") else "6s",
        "video_resolution": "480p" if str(body.get("video_resolution", "")).startswith("480") else "720p",
        "model_imgs": model_imgs, "product_imgs": product_imgs,
        "confirm_name": (body.get("product_name") or "").strip(),
        "confirm_size_ml": body.get("product_size_ml"),
        "autochain": bool(body.get("autochain", True)),
    }
    title = f"{ffgen.MODES[mode]['label']} — {params['project_name']}"
    job = jobs.run("generate", title,
                   lambda j: _generate_worker(j, params, script_sel,
                                              image_sel, render_sel))
    return jsonify({"ok": True, "job_id": job.id})


def _generate_worker(job, params, script_sel, image_sel, render_sel=None):
    """Run a Generate job, leasing accounts from the load-balancing router.

    Each role (script / image / render) is leased from the router, which
    hands back the least-busy healthy account of the requested platform and
    queues the job if all accounts of that platform are busy. Leases are held
    for the whole job and released on exit (success or error), so a second
    concurrent job is routed to a different account automatically.
    """
    render_sel = render_sel or image_sel
    log = job.log

    def _wait_logger(role):
        def _cb(secs):
            log(f"\u23F3 Menunggu akun {role} yang kosong (antrian)\u2026")
        return _cb

    # Acquire ONE lease per distinct (platform, prefer_id) so that roles which
    # land on the same platform share a single account. This is essential when
    # only one account of a platform exists: leasing it twice would deadlock
    # (the second wait never frees). Roles on different platforms (or different
    # explicitly-picked accounts) still get independent accounts, giving real
    # N-account concurrency across simultaneous jobs.
    roles = [("skrip", "script", script_sel),
             ("gambar", "image", image_sel),
             ("render", "render", render_sel)]

    # Group roles by their lease key.
    def _key(sel):
        return (sel["platform"], sel.get("prefer_id") or "")

    order: list = []
    for _label, _name, sel in roles:
        k = _key(sel)
        if k not in [o[0] for o in order]:
            order.append((k, _label, sel))

    leased: dict = {}   # key -> Lease
    try:
        import contextlib
        with contextlib.ExitStack() as stack:
            for k, lbl, sel in order:
                lease = stack.enter_context(
                    router.lease(sel["platform"], prefer_id=sel.get("prefer_id"),
                                 on_wait=_wait_logger(lbl)))
                leased[k] = lease
            s = leased[_key(script_sel)]
            i = leased[_key(image_sel)]
            r = leased[_key(render_sel)]
            _run_generate(
                job, params,
                s.profile, i.profile, r.profile,
                s.worker, i.worker, r.worker)
    except NoProfileAvailable as e:
        raise RuntimeError(str(e))
    except LeaseTimeout as e:
        raise RuntimeError(str(e))


def _run_generate(job, params, script_profile, image_profile, render_profile,
                  sworker, iworker, rworker):
    render_profile = render_profile or image_profile
    log = job.log
    sp_label = _pdict(script_profile)["name"]
    log(f"\U0001F4DD Mengirim brief ke {sp_label}\u2026")
    if not cdp_probe(script_profile.port)["reachable"]:
        raise RuntimeError(f"Chrome untuk {script_profile.id} tidak terjangkau.")
    prompt = ffgen.build_chatgpt_prompt(
        mode=params["mode"], num_scenes=params["num_scenes"],
        background=params["background"], voice_key=params["voice_key"],
        tone_key=params["tone_key"], aspect=params["aspect"])
    needs = ffgen.MODES[params["mode"]]["needs"]
    ref_imgs = []
    if "model" in needs:
        ref_imgs += params["model_imgs"]
    if "product" in needs:
        ref_imgs += params["product_imgs"]
    seen = set()
    ref_imgs = [p for p in ref_imgs if not (p in seen or seen.add(p))][:6]
    if ref_imgs:
        log(f"\U0001F5BC  Melampirkan {len(ref_imgs)} foto referensi\u2026")
    result = sworker.chat(script_profile.platform, prompt, label=script_profile.label,
                          timeout=180, force_new_chat=True, attachments=ref_imgs)
    if job.cancelled:
        return
    if not result.get("ok"):
        raise RuntimeError(f"{sp_label} gagal: {result.get('error', '?')}")
    reply = result.get("response", "") or result.get("text", "") or ""
    try:
        script = ffgen.parse_script_json(reply)
    except Exception as e:  # noqa: BLE001
        log("   Balasan mentah:\n" + reply[:600])
        raise RuntimeError(f"Gagal baca skrip JSON: {e}")
    scenes = script["scenes"]
    want = params["num_scenes"]
    if len(scenes) > want:
        scenes = scenes[:want]; script["scenes"] = scenes
        log(f"\u2139 Dipangkas ke {want} scene.")
    elif len(scenes) < want:
        log(f"\u26A0 Hanya {len(scenes)} scene dibalas (minta {want}).")
    log(f"\u2713 {len(scenes)} scene siap.")

    # --- Confirm brand / product / size with the user BEFORE drawing. ---
    # Mirrors the desktop app's _gen_confirm_product step: the script provider
    # read these off the attached product photo; the user verifies/corrects
    # them before any image is generated. Cancelling here aborts the job.
    if "product" in needs and params["product_imgs"]:
        answer = job.ask_user({
            "kind": "confirm_product",
            "title": "Konfirmasi Produk",
            "brand": str(script.get("brand", "") or ""),
            "name": str(script.get("product_name", "") or ""),
            "size_ml": script.get("size_ml"),
            "by": sp_label,
            "prefill_name": params["confirm_name"],
            "prefill_size_ml": params["confirm_size_ml"],
        })
        if answer is None:
            log("\u2139 Generate dibatalkan di konfirmasi produk.")
            return
        brand = str(answer.get("brand", "") or "").strip()
        name = str(answer.get("name", "") or "").strip()
        prod_name = (f"{brand} {name}".strip() if brand else name).strip()
        prod_size = _parse_size_ml(answer.get("size_ml"))
        _sz = (f" \u2022 {int(prod_size) if float(prod_size) == int(prod_size) else prod_size} ml"
               if prod_size is not None else "")
        log(f"\u2713 Produk dikonfirmasi: {prod_name or '(tanpa nama)'}{_sz}")
    else:
        prod_name = params["confirm_name"] or str(script.get("product_name", "") or "")
        prod_size = params["confirm_size_ml"]
        if prod_size in ("", None):
            prod_size = script.get("size_ml")
        prod_size = _parse_size_ml(prod_size)
    model_imgs = params["model_imgs"]; product_imgs = params["product_imgs"]
    scene_refs = []
    if "model" in needs:
        scene_refs += model_imgs
    if "product" in needs:
        scene_refs += product_imgs
    seen = set()
    scene_refs = [p for p in scene_refs if not (p in seen or seen.add(p))][:4]
    raw_fallback = (model_imgs if ("model" in needs and model_imgs)
                    else product_imgs or model_imgs)
    ip_label = _pdict(image_profile)["name"]
    log(f"\U0001F3A8 Pembuat gambar: {ip_label}")
    if not cdp_probe(image_profile.port)["reachable"]:
        raise RuntimeError(f"Chrome untuk {image_profile.id} tidak terjangkau.")
    # iworker is the leased image account (passed in by _generate_worker).
    img_opts = {"mode": "image", "resolution": params["video_resolution"],
                "duration": "6s", "aspect": params["aspect"]}
    scene_dir = GENERATED_DIR / "scene_stills"
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_images: dict[int, Path] = {}
    for idx, s in enumerate(scenes, start=1):
        if job.cancelled:
            log("\U0001F6D1 Dibatalkan."); return
        log(f"\U0001F5BC  Gambar Scene {idx} via {ip_label}\u2026")
        img_prompt = ffgen.build_scene_image_prompt(
            mode=params["mode"], scene_action=str(s.get("action", "")),
            spoken=str(s.get("spoken", "")), background=params["background"],
            aspect=params["aspect"], product_name=prod_name, product_size_ml=prod_size)
        try:
            def _gen_image():
                if image_profile.platform == "gemini":
                    return iworker.chat("gemini", img_prompt, label=image_profile.label,
                                        timeout=300, force_new_chat=True, attachments=scene_refs)
                return iworker.chat("grok", img_prompt, label=image_profile.label,
                                    timeout=300, force_new_chat=True,
                                    attachments=scene_refs, imagine_opts=img_opts)

            def _pick_image(resp):
                for mobj in ((resp.get("media") or []) if resp.get("ok") else []):
                    if mobj.get("type") == "image" and mobj.get("local_path"):
                        lp = Path(mobj["local_path"])
                        if lp.exists():
                            return lp
                return None

            r = _gen_image()
            img_lp = _pick_image(r)
            # Gemini sometimes replies with TEXT instead of generating an image.
            # One automatic retry (fresh chat) clears most of these.
            if img_lp is None and image_profile.platform == "gemini" and r.get("ok"):
                log(f"   \u21BB Scene {idx}: Gemini balas teks, coba ulang sekali\u2026")
                r = _gen_image()
                img_lp = _pick_image(r)

            saved = None
            if img_lp is not None:
                saved = scene_dir / f"{params['project_name']}_scene{idx}.png"
                shutil.copy(img_lp, saved)
            if saved:
                scene_images[idx] = saved
                log(f"   \u2713 Scene {idx} siap.")
            elif not r.get("ok"):
                raise RuntimeError(r.get("error") or "Gemini gagal (tidak diketahui).")
            else:
                raise RuntimeError("Balasan diterima tapi tidak ada gambar "
                                   "(Gemini balas teks saja).")
        except Exception as e:  # noqa: BLE001
            log(f"   \u26A0 Scene {idx} gagal ({e}). Pakai foto upload.")
            if raw_fallback:
                scene_images[idx] = raw_fallback[(idx - 1) % len(raw_fallback)]
    log("\U0001F4E6 Merakit ZIP\u2026")
    zip_path = ffgen.assemble_zip(
        out_dir=GENERATED_DIR, project_name=params["project_name"], script=script,
        mode=params["mode"], background=params["background"], aspect=params["aspect"],
        voice_key=params["voice_key"], tone_key=params["tone_key"],
        scene_images=scene_images)
    log(f"\u2713 ZIP siap: {Path(zip_path).name}")
    job.result = {"zip_name": Path(zip_path).name, "scenes": len(scenes),
                  "download": f"/api/download?path={Path(zip_path).name}"}

    # ── Phase 4: auto-chain into the final batch render (Grok Imagine). ──
    # Mirrors the desktop _gen_run_grok_batch / _batch_zip_from_path step:
    # the ZIP is NOT the deliverable — each Scene_N (still + lip-sync text) is
    # fed to the image profile in Imagine mode to render the final video/image.
    if not params["autochain"]:
        log("\u2139 Auto-chain mati. ZIP siap untuk diproses manual.")
        return
    out_mode = params["output_mode"]  # "video" | "image"
    rp_label = _pdict(render_profile)["name"]
    log(f"\u26A1 Lanjut render final ({out_mode}) via {rp_label}\u2026")
    if render_profile.id != image_profile.id:
        if not cdp_probe(render_profile.port)["reachable"]:
            raise RuntimeError(f"Chrome untuk render {render_profile.id} tidak terjangkau.")
    # rworker is the leased render account (passed in by _generate_worker).
    final_dir = GENERATED_DIR / "final" / params["project_name"]
    final_dir.mkdir(parents=True, exist_ok=True)
    batch_opts = {"mode": out_mode, "aspect": params["aspect"],
                  "resolution": params["video_resolution"],
                  "duration": params["video_duration"]}
    rendered = []
    ok_count = 0
    for idx, s in enumerate(scenes, start=1):
        if job.cancelled:
            log("\U0001F6D1 Render final dibatalkan."); break
        still = scene_images.get(idx)
        spoken = str(s.get("spoken", "") or "").replace("'", "\u2019").strip()
        # Build the same lip-sync prompt the desktop batch extracts from prompt.txt.
        batch_prompt = (f"LIP-SYNC: Model is speaking: '{spoken}'."
                        if spoken else str(s.get("action", "") or "Scene"))
        log(f"\U0001F3AC Render Scene {idx}/{len(scenes)} ({out_mode})\u2026")
        try:
            atts = [still] if still and Path(still).exists() else []
            if render_profile.platform == "gemini":
                if out_mode == "video":
                    # Gemini VIDEO must go through the dedicated "Create video"
                    # (Omni/Veo) flow — a plain chat only returns a TEXT
                    # transcript (the old bug: no clip, just text + sparkle).
                    # imagine_opts routes chat() into _imagine_gemini, which
                    # arms video mode, attaches the still, polls the async
                    # render, and strips the visible ✦ watermark per-frame.
                    log("   \U0001F3A5 Mode video Gemini (Create video / Omni) — async, "
                        "tunggu beberapa menit\u2026")
                    r = rworker.chat("gemini", batch_prompt, label=render_profile.label,
                                     timeout=420, force_new_chat=True,
                                     attachments=atts, imagine_opts=batch_opts)
                else:
                    # Still image: the normal Gemini chat (Nano Banana) is correct.
                    r = rworker.chat("gemini", batch_prompt, label=render_profile.label,
                                     timeout=420, force_new_chat=True, attachments=atts)
            else:
                r = rworker.chat("grok", batch_prompt, label=render_profile.label,
                                 timeout=420, force_new_chat=True,
                                 attachments=atts, imagine_opts=batch_opts)
            if not r.get("ok"):
                # Surface the actual reason instead of a generic "no media".
                err_msg = r.get("error") or "tidak diketahui"
                dbg = r.get("debug")
                log(f"   \u26A0 Scene {idx} gagal render: {err_msg}")
                if dbg:
                    log(f"      (debug: {dbg})")
                continue
            media = r.get("media") or []
            saved_url = None
            for mobj in media:
                lp = Path(mobj.get("local_path", "") or "")
                if not lp.exists():
                    continue
                ext = lp.suffix.lower() or (".mp4" if out_mode == "video" else ".png")
                dest = final_dir / f"{params['project_name']}_scene{idx}{ext}"
                shutil.copy(lp, dest)
                rel = dest.resolve().relative_to(MEDIA_DIR.resolve()).as_posix()
                saved_url = f"/media/{rel}"
                # Tell the user whether the visible watermark was actually
                # stripped — if not, say why (e.g. ffmpeg/opencv missing).
                if out_mode == "video" and render_profile.platform == "gemini":
                    if mobj.get("dewatermarked"):
                        log("   \U0001F9F9 Watermark sparkle dihapus.")
                    else:
                        why = mobj.get("dewatermark_error") or (
                            "ffmpeg/opencv tidak tersedia di container web "
                            "(rebuild image dengan Dockerfile.web terbaru), "
                            "atau sparkle tidak terdeteksi.")
                        log(f"   \u26A0 Watermark TIDAK dihapus: {why}")
                break
            if saved_url:
                ok_count += 1
                rendered.append({"scene": idx, "type": out_mode, "url": saved_url})
                log(f"   \u2713 Scene {idx} ter-render.")
            else:
                log(f"   \u26A0 Scene {idx}: balasan OK tapi tidak ada file media "
                    f"(mungkin video belum selesai / harvest gagal).")
        except Exception as e:  # noqa: BLE001
            log(f"   \u26A0 Scene {idx} gagal render ({e}).")
    log(f"\U0001F4E6 Render final selesai: {ok_count}/{len(scenes)} scene.")
    job.result = {"zip_name": Path(zip_path).name, "scenes": len(scenes),
                  "download": f"/api/download?path={Path(zip_path).name}",
                  "output_mode": out_mode, "rendered": rendered,
                  "rendered_ok": ok_count}


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    prof = store.get(body.get("profile_id", ""))
    if not prof:
        return jsonify({"ok": False, "error": "Pilih profil"}), 400
    if prof.platform not in CHAT_PLATFORMS:
        return jsonify({"ok": False, "error": f"{prof.platform} tidak mendukung chat"}), 400
    message = (body.get("message") or "").strip()
    attachments = [Path(p) for p in body.get("attachments", []) if Path(p).exists()]
    if not message and not attachments:
        return jsonify({"ok": False, "error": "Pesan kosong"}), 400
    job = jobs.run("chat", f"Chat {prof.id} — {message[:32] or 'media'}",
                   lambda j: _chat_worker(j, prof, message, attachments,
                                          bool(body.get("new_chat", False)),
                                          body.get("imagine")))
    return jsonify({"ok": True, "job_id": job.id})


def _chat_worker(job, prof, message, attachments, new_chat, imagine):
    log = job.log
    if not cdp_probe(prof.port)["reachable"]:
        raise RuntimeError("Chrome profil ini tidak terjangkau (cek login/noVNC).")
    if attachments:
        log(f"\U0001F4CE {len(attachments)} lampiran")
    log(f"\u2192 {prof.id}\u2026")
    worker = registry.worker_for(prof)
    imagine_opts = None
    if prof.platform == "grok" and isinstance(imagine, dict) and imagine.get("mode"):
        imagine_opts = {"mode": imagine.get("mode", "image"),
                        "aspect": imagine.get("aspect", "9:16"),
                        "resolution": imagine.get("resolution", "720p"),
                        "duration": imagine.get("duration", "6s")}
        log(f"\U0001F3A8 Imagine {imagine_opts['mode']} {imagine_opts['aspect']}")
    r = worker.chat(prof.platform, message, label=prof.label, timeout=300,
                    force_new_chat=new_chat, attachments=attachments,
                    imagine_opts=imagine_opts)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "Chat gagal"))
    text = r.get("response", "") or r.get("text", "") or ""
    media_out = []
    for m in (r.get("media") or []):
        url = None
        try:
            rel = Path(m.get("local_path", "")).resolve().relative_to(MEDIA_DIR.resolve())
            url = f"/media/{rel.as_posix()}"
        except Exception:  # noqa: BLE001
            url = None
        media_out.append({"type": m.get("type"), "url": url, "alt": m.get("alt", "")})
    if text:
        log("\u2713 Balasan diterima.")
    if media_out:
        log(f"\u2713 {len(media_out)} media.")
    job.result = {"response": text, "media": media_out}


@app.route("/api/chat/new", methods=["POST"])
def api_chat_new():
    body = request.get_json(silent=True) or {}
    prof = store.get(body.get("profile_id", ""))
    if not prof:
        return jsonify({"ok": False, "error": "Profil tidak ada"}), 404
    worker = registry.worker_for(prof)
    try:
        ok = worker.start_new_chat(prof.platform, prof.label)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "reset": ok})


@app.route("/api/history")
def api_history():
    items = []
    if GENERATED_DIR.exists():
        for f in sorted(GENERATED_DIR.rglob("*"),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            kind = ("video" if ext in (".mp4", ".webm", ".mov") else
                    "image" if ext in (".png", ".jpg", ".jpeg", ".webp") else
                    "zip" if ext == ".zip" else "file")
            rel = f.relative_to(GENERATED_DIR).as_posix()
            items.append({"name": f.name, "kind": kind, "rel": rel,
                          "size_kb": round(f.stat().st_size / 1024, 1),
                          "mtime": f.stat().st_mtime, "url": f"/media/generated/{rel}"})
    return jsonify({"items": items[:200]})


@app.route("/media/<path:filename>")
def serve_media(filename):
    target = (MEDIA_DIR / filename).resolve()
    try:
        target.relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return jsonify({"error": "invalid path"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/api/download")
def api_download():
    name = request.args.get("path", "")
    target = (GENERATED_DIR / name).resolve()
    try:
        target.relative_to(GENERATED_DIR.resolve())
    except ValueError:
        return jsonify({"error": "invalid path"}), 400
    if not target.exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(GENERATED_DIR, name, as_attachment=True)


@app.route("/api/live/merge", methods=["POST"])
def api_live_merge():
    body = request.get_json(silent=True) or {}
    src = body.get("src") or str(GENERATED_DIR)
    portrait = bool(body.get("portrait", True))
    loop_min = body.get("loop_min")
    out = body.get("out") or str(GENERATED_DIR / "live_video.mp4")
    job = jobs.run("merge", "Gabung video LIVE",
                   lambda j: _merge_worker(j, src, portrait, loop_min, out))
    return jsonify({"ok": True, "job_id": job.id})


def _merge_worker(job, src, portrait, loop_min, out):
    log = job.log
    log(f"\U0001F3AC Menggabungkan video dari {src}\u2026")
    script = PROJECT_ROOT / "merge_scenes_video.py"
    fn = getattr(live_module, "merge_scenes_to_video", None) if live_module else None
    if callable(fn):
        try:
            res = fn(src, out, portrait=portrait, loop_minutes=loop_min)
            log(f"\u2713 Selesai: {res}")
            job.result = {"download": f"/api/download?path={Path(out).name}"}
            return
        except TypeError:
            pass  # signature differs; fall back to script below
    if script.exists():
        cmd = [sys.executable, str(script), "--src", str(src), "--out", str(out)]
        if portrait:
            cmd += ["--portrait"]
        if loop_min:
            cmd += ["--loop-min", str(loop_min)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        log((proc.stdout or "")[-1500:] or "(tidak ada output)")
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "merge gagal")[-500:])
        job.result = {"download": f"/api/download?path={Path(out).name}"}
        log("\u2713 Selesai.")
        return
    raise RuntimeError("Tidak menemukan fungsi/skrip merge.")


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    cfg = load_config()
    if request.method == "GET":
        return jsonify({"api_port": cfg.get("api_port", 5100),
                        "api_key": cfg.get("api_key", ""), "cdp_host": CDP_HOST,
                        "live_user": cfg.get("live_user", ""),
                        "live_ai": cfg.get("live_ai", "grok")})
    body = request.get_json(silent=True) or {}
    for k in ("api_port", "api_key", "live_user", "live_ai"):
        if k in body:
            cfg[k] = body[k]
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/apiserver", methods=["POST"])
def api_apiserver():
    global _api_proc
    body = request.get_json(silent=True) or {}
    action = body.get("action", "")
    cfg = load_config()
    if action == "start":
        if _api_proc and _api_proc.poll() is None:
            return jsonify({"ok": True, "running": True})
        port = str(cfg.get("api_port", 5100))
        env = os.environ.copy()
        if cfg.get("api_key"):
            env["API_KEY"] = cfg["api_key"]
        _api_proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "api_server.py"),
             "--port", port, "--host", "0.0.0.0"], env=env)
        return jsonify({"ok": True, "running": True, "port": port})
    if action == "stop":
        if _api_proc:
            _api_proc.terminate(); _api_proc = None
        return jsonify({"ok": True, "running": False})
    return jsonify({"ok": False, "error": "unknown action"}), 400


@app.route("/api/jobs")
def api_jobs():
    return jsonify({"jobs": jobs.list(request.args.get("kind") or None)})


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    after = int(request.args.get("after", 0))
    return jsonify({**job.to_dict(),
                    "lines": [{"seq": s, "time": t, "msg": m}
                              for (s, t, m) in job.lines_since(after)]})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    job.request_cancel()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/confirm", methods=["POST"])
def api_job_confirm(job_id):
    """Answer a job that is parked in `awaiting_input` (e.g. the product
    confirmation gate). Body: {"cancel": true} to abort, or the field values
    {"brand","name","size_ml"} to continue."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    if body.get("cancel"):
        ok = job.submit_answer(None)
    else:
        ok = job.submit_answer({
            "brand": (body.get("brand") or "").strip(),
            "name": (body.get("name") or "").strip(),
            "size_ml": body.get("size_ml"),
        })
    if not ok:
        return jsonify({"ok": False, "error": "Job tidak sedang menunggu konfirmasi."}), 409
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/stream")
def api_job_stream(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    @stream_with_context
    def gen():
        last = int(request.args.get("after", 0))
        asked = False
        while True:
            for (s, t, m) in job.lines_since(last):
                last = s
                yield f"data: {json.dumps({'seq': s, 'time': t, 'msg': m})}\n\n"
            snap = job.to_dict()
            # Mid-job confirmation: surface the prompt once, then keep the
            # stream open (awaiting_input is NOT terminal).
            if snap["status"] == "awaiting_input":
                if not asked and snap.get("prompt"):
                    asked = True
                    yield f"event: ask\ndata: {json.dumps(snap['prompt'])}\n\n"
                time.sleep(0.4)
                continue
            asked = False
            if snap["status"] != "running" and not job.lines_since(last):
                yield f"event: end\ndata: {json.dumps(snap)}\n\n"
                return
            time.sleep(0.4)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _write_supervisor_config():
    try:
        PROFILES_JSON.write_text(json.dumps(
            [{"id": p.id, "platform": p.platform, "label": p.label, "port": p.port}
             for p in store.list()], indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main():
    p = argparse.ArgumentParser(description="FakeFluencer V2 Web UI (CDP)")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    print(f"\nFakeFluencer V2 (CDP)  →  http://{args.host}:{args.port}/dashboard")
    print(f"CDP host: {CDP_HOST}  |  profiles: {PROFILES_JSON}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
