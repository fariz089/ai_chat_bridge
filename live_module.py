"""
live_module.py
──────────────
Logika untuk menu LIVE di desktop app (dipanggil dari ai_chat_bridge.py).

Berisi dua hal:
  1. LiveReplyController — connect ke TikTok LIVE, baca komentar, kirim ke
     BridgeWorker yang SUDAH dipakai app (grok/chatgpt/gemini), ubah jawaban
     jadi suara (edge-tts), putar ke speaker / VB-CABLE.
  2. merge_scene_videos() — gabung beberapa .mp4 jadi satu video live (ffmpeg),
     opsional diulang sampai durasi target.

Modul ini sengaja TIDAK menyentuh GUI. Semua output disampaikan lewat callback
`log(msg)` supaya app bisa menampilkannya di panel log live.

Dependency opsional (di-install user lewat tombol di tab):
    pip install TikTokLive edge-tts
    (untuk arahkan suara ke device tertentu) pip install sounddevice soundfile
ffmpeg harus ada di PATH untuk fitur merge video.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import queue
from pathlib import Path
from typing import Callable, Optional

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

# Suara edge-tts Indonesia yang umum dipakai
INDO_VOICES = [
    "id-ID-ArdiNeural (Pria)",
    "id-ID-GadisNeural (Wanita)",
]


def voice_id(display: str) -> str:
    """'id-ID-ArdiNeural (Pria)' -> 'id-ID-ArdiNeural'."""
    return display.split(" ")[0].strip()


DEFAULT_PERSONA = (
    "Kamu adalah host TikTok LIVE yang ramah dan natural. "
    "Jawab komentar penonton dengan SINGKAT (maksimal 2 kalimat), santai, "
    "seperti ngobrol langsung. Jangan pakai emoji, jangan pakai format markdown, "
    "jangan sebut bahwa kamu AI. Sebut nama penonton kalau ada."
)


# ════════════════════════════════════════════════════════════════════════
# TTS
# ════════════════════════════════════════════════════════════════════════
async def _tts_save(text: str, voice: str, out_path: Path):
    import edge_tts
    await edge_tts.Communicate(text, voice).save(str(out_path))


def synth_voice(text: str, voice: str) -> Path:
    out = Path(tempfile.gettempdir()) / f"ttslive_{abs(hash(text)) % 10**8}.mp3"
    asyncio.run(_tts_save(text, voice, out))
    return out


def play_audio(path: Path, device_match: Optional[str], log: Callable[[str], None]):
    if device_match:
        try:
            import sounddevice as sd
            import soundfile as sf
            dev_index = None
            for i, d in enumerate(sd.query_devices()):
                if device_match.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                    dev_index = i
                    break
            if dev_index is None:
                log(f"⚠ Device '{device_match}' tidak ketemu, pakai speaker default.")
            data, sr = sf.read(str(path), dtype="float32")
            sd.play(data, sr, device=dev_index)
            sd.wait()
            return
        except ImportError:
            log("⚠ sounddevice/soundfile belum ada — pakai speaker default. "
                "(pip install sounddevice soundfile)")
    # default speaker
    try:
        from playsound import playsound
        playsound(str(path))
    except Exception:
        import os
        if os.name == "nt":
            try:
                import winsound
                # winsound tak putar mp3; pakai start sebagai fallback
                os.startfile(str(path))  # type: ignore
            except Exception:
                os.startfile(str(path))  # type: ignore
        else:
            import os as _os
            _os.system(f'afplay "{path}" 2>/dev/null || xdg-open "{path}" >/dev/null 2>&1')


def list_output_devices() -> list[str]:
    """Daftar nama device output (untuk dropdown). Kosong kalau sounddevice tak ada."""
    try:
        import sounddevice as sd
        names = []
        for d in sd.query_devices():
            if d["max_output_channels"] > 0:
                names.append(d["name"])
        # unik, jaga urutan
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))]
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════
# LIVE reply controller
# ════════════════════════════════════════════════════════════════════════
class LiveReplyController:
    """
    Jalankan listener TikTok LIVE di thread terpisah. Tiap komentar diproses
    serial oleh voice-worker (1 jawaban selesai diucapkan sebelum lanjut).

    Parameters
    ----------
    ensure_bridge : callable(platform_key) -> BridgeWorker
        Fungsi milik app (self._ensure_bridge_for) untuk dapat bridge yang
        sama dengan tab Chat — jadi sesi login dibagi, tidak buka browser baru.
    log : callable(str)
        Untuk menampilkan pesan ke panel log live.
    on_comment : callable(username, comment, answer) optional
        Dipanggil tiap pasangan komentar→jawaban (buat transcript di UI).
    """

    def __init__(self, ensure_bridge: Callable[[str], object],
                 log: Callable[[str], None],
                 on_comment: Optional[Callable[[str, str, str], None]] = None):
        self._ensure_bridge = ensure_bridge
        self._log = log
        self._on_comment = on_comment

        self._client = None
        self._live_thread: Optional[threading.Thread] = None
        self._voice_thread: Optional[threading.Thread] = None
        self._q: "queue.Queue" = queue.Queue()
        self._running = threading.Event()

        # diisi saat start()
        self.ai_model = "grok"
        self.label = "default"
        self.voice = "id-ID-ArdiNeural"
        self.persona = DEFAULT_PERSONA
        self.device_match: Optional[str] = None
        self.max_chars = 220
        self.reply_gifts = False

    # ── public API ──────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self, *, username: str, ai_model: str, label: str, voice: str,
              persona: str, device_match: Optional[str], max_chars: int,
              reply_gifts: bool):
        if self._running.is_set():
            self._log("⚠ Live sudah berjalan.")
            return
        # cek dependency dulu, kasih pesan jelas
        try:
            import TikTokLive  # noqa
        except ImportError:
            self._log("❌ Library 'TikTokLive' belum terpasang. "
                      "Klik tombol 'Install dependency' dulu.")
            return
        try:
            import edge_tts  # noqa
        except ImportError:
            self._log("❌ Library 'edge-tts' belum terpasang. "
                      "Klik tombol 'Install dependency' dulu.")
            return

        self.ai_model = ai_model
        self.label = label or "default"
        self.voice = voice
        self.persona = persona or DEFAULT_PERSONA
        self.device_match = device_match or None
        self.max_chars = max_chars
        self.reply_gifts = reply_gifts

        self._running.set()
        self._voice_thread = threading.Thread(target=self._voice_loop, daemon=True)
        self._voice_thread.start()
        self._live_thread = threading.Thread(
            target=self._live_loop, args=(username,), daemon=True)
        self._live_thread.start()

    def stop(self):
        if not self._running.is_set():
            return
        self._running.clear()
        self._log("🛑 Menghentikan live...")
        try:
            if self._client is not None:
                # TikTokLiveClient punya .disconnect() async; jalankan aman
                import asyncio as _a
                fut = getattr(self._client, "disconnect", None)
                if fut:
                    try:
                        loop = self._client._asyncio_loop  # internal, kalau ada
                        _a.run_coroutine_threadsafe(self._client.disconnect(), loop)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── internals ───────────────────────────────────────────────────────
    def _ask_bridge(self, username: str, comment: str) -> str:
        bridge = self._ensure_bridge(self.ai_model)
        prompt = (f"Penonton bernama '{username}' berkomentar: \"{comment}\". "
                  f"Balas komentar ini sebagai host live.")
        try:
            result = bridge.chat(
                self.ai_model, prompt, label=self.label, timeout=120,
                force_new_chat=True,
            )
        except Exception as e:
            self._log(f"✗ Bridge error: {e}")
            return ""
        if not result.get("ok"):
            self._log(f"✗ {self.ai_model} gagal: {result.get('error', '?')}")
            return ""
        text = (result.get("response") or result.get("text") or "").strip()
        text = text.replace("```", "").strip()
        if len(text) > self.max_chars:
            text = text[:self.max_chars].rsplit(" ", 1)[0] + "."
        return text

    def _voice_loop(self):
        while self._running.is_set():
            try:
                username, comment = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._log(f"💬 {username}: {comment}")
            answer = self._ask_bridge(username, comment)
            if not answer:
                continue
            self._log(f"🤖 {self.ai_model}: {answer}")
            if self._on_comment:
                try:
                    self._on_comment(username, comment, answer)
                except Exception:
                    pass
            try:
                mp3 = synth_voice(answer, self.voice)
                play_audio(mp3, self.device_match, self._log)
                try:
                    mp3.unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception as e:
                self._log(f"✗ TTS/putar gagal: {e}")

    def _enqueue(self, username: str, comment: str):
        if self._q.qsize() < 8:
            self._q.put((username, comment))

    def _live_loop(self, username: str):
        if not username.startswith("@"):
            username = "@" + username
        try:
            from TikTokLive import TikTokLiveClient
            from TikTokLive.events import CommentEvent, ConnectEvent, GiftEvent
        except Exception as e:
            self._log(f"❌ Gagal import TikTokLive: {e}")
            self._running.clear()
            return

        client = TikTokLiveClient(unique_id=username)
        self._client = client

        @client.on(ConnectEvent)
        async def _on_connect(event):
            self._log(f"✅ Terhubung ke LIVE @{event.unique_id}. Menunggu komentar...")

        @client.on(CommentEvent)
        async def _on_comment(event):
            if not self._running.is_set():
                return
            name = event.user.nickname or event.user.unique_id
            self._enqueue(name, event.comment)

        if self.reply_gifts:
            @client.on(GiftEvent)
            async def _on_gift(event):
                if not self._running.is_set():
                    return
                name = event.user.nickname or event.user.unique_id
                g = event.gift
                if getattr(g, "streakable", False) and not event.streaking:
                    self._enqueue(name, f"(mengirim gift {g.name}) terima kasih ya!")
                elif not getattr(g, "streakable", False):
                    self._enqueue(name, f"(mengirim gift {g.name}) makasih banyak!")

        try:
            client.run()
        except Exception as e:
            self._log(f"❌ Koneksi LIVE berhenti: {e}")
            self._log("   Pastikan akun sedang LIVE, username benar, internet OK.")
        finally:
            self._running.clear()
            self._log("● Live berhenti.")


# ════════════════════════════════════════════════════════════════════════
# Merge video
# ════════════════════════════════════════════════════════════════════════
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL)
        return float(out.strip())
    except Exception:
        return 0.0


def merge_scene_videos(paths: list[Path], out: Path,
                       log: Callable[[str], None],
                       loop_to: Optional[float] = None,
                       portrait: bool = True) -> bool:
    """
    Normalkan semua klip ke 1080x1920 (atau 1920x1080), 30fps, lalu concat.
    Opsional ulang sampai total >= loop_to detik. Return True jika sukses.
    """
    if not ffmpeg_available():
        log("❌ ffmpeg tidak ada di PATH. Install dulu (lihat tab).")
        return False
    paths = [p for p in paths if p.exists() and p.suffix.lower() in VIDEO_EXTS]
    if not paths:
        log("❌ Tidak ada file video valid.")
        return False

    w, h = (1080, 1920) if portrait else (1920, 1080)
    tmp = Path(tempfile.mkdtemp(prefix="merge_scenes_"))
    norm = []
    log(f"🎬 Menormalkan {len(paths)} klip ({w}x{h}, 30fps)...")
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1")
    for i, p in enumerate(paths):
        dst = tmp / f"norm_{i:03d}.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(p), "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-ar", "44100", "-ac", "2",
               "-pix_fmt", "yuv420p", str(dst)]
        subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)
        if not dst.exists():
            cmd2 = ["ffmpeg", "-y", "-i", str(p),
                    "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100",
                    "-vf", vf, "-shortest",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-pix_fmt", "yuv420p", str(dst)]
            subprocess.run(cmd2, check=False, stderr=subprocess.DEVNULL)
        if dst.exists():
            norm.append(dst)
            log(f"   ✓ {p.name}")
        else:
            log(f"   ✗ gagal proses {p.name} (dilewati)")

    if not norm:
        shutil.rmtree(tmp, ignore_errors=True)
        log("❌ Tidak ada klip yang berhasil dinormalkan.")
        return False

    list_file = tmp / "list.txt"
    list_file.write_text("\n".join(f"file '{f.as_posix()}'" for f in norm),
                         encoding="utf-8")
    joined = tmp / "joined.mp4"
    log("🔗 Menggabungkan...")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(list_file), "-c", "copy", str(joined)],
                   check=False, stderr=subprocess.DEVNULL)
    if not joined.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        log("❌ Gabung gagal.")
        return False

    out.parent.mkdir(parents=True, exist_ok=True)
    if loop_to:
        one = _probe_duration(joined)
        if one > 0:
            reps = max(1, int(loop_to // one) + 1)
            log(f"🔁 Mengulang {reps}x supaya total ≥ {loop_to:.0f}s...")
            subprocess.run(["ffmpeg", "-y", "-stream_loop", str(reps - 1),
                            "-i", str(joined), "-c", "copy",
                            "-t", str(loop_to), str(out)],
                           check=False, stderr=subprocess.DEVNULL)
        else:
            shutil.copy(joined, out)
    else:
        shutil.copy(joined, out)

    shutil.rmtree(tmp, ignore_errors=True)
    if out.exists():
        log(f"✅ Selesai: {out}  ({_probe_duration(out):.0f} detik)")
        log("   Pasang di TikTok LIVE Studio: Add source → Video → pilih file ini.")
        return True
    log("❌ Output tidak terbentuk.")
    return False
