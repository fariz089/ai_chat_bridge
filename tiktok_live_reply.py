"""
tiktok_live_reply.py
─────────────────────
Auto-jawab komentar TikTok LIVE pakai SUARA (TTS).

Alur:
  1. Connect ke room LIVE pakai @username (library TikTokLive).
  2. Setiap komentar masuk → kirim teks ke AI Chat Bridge (port 5100)
     yang sudah jalan di project Anda (bisa pilih grok / chatgpt / gemini).
  3. Jawaban teks dari bridge → diubah jadi suara MP3 oleh edge-tts (GRATIS,
     suara Indonesia natural).
  4. MP3 diputar ke speaker. Supaya MASUK ke siaran TikTok LIVE, set output
     audio Python ke "virtual audio cable" lalu pilih cable itu sebagai
     Microphone di TikTok LIVE Studio. (lihat CARA PASANG di bawah)

──────────────────────────────────────────────────────────────────────
CARA PASANG (sekali saja)
──────────────────────────────────────────────────────────────────────
1) Install dependency:
       pip install TikTokLive edge-tts playsound==1.2.2

2) (Agar suara MASUK ke TikTok) Install virtual audio cable:
       - Windows : VB-CABLE  (https://vb-audio.com/Cable/)  -> gratis
       - Setelah install, Windows punya device baru "CABLE Input" & "CABLE Output".
   Di TikTok LIVE Studio -> ikon Mic -> pilih "CABLE Output" sebagai mic.
   (Tanpa cable, suara cuma keluar di speaker Anda — tetap jalan, tapi
    penonton tidak dengar. Untuk tes dulu, jalankan tanpa cable saja.)

3) Pastikan AI Chat Bridge Anda SUDAH jalan (API di port 5100, sudah login
   ke grok/chatgpt/gemini lewat GUI bridge).

4) Jalankan:
       python tiktok_live_reply.py --user @username_tiktok_anda --ai grok

   Opsi penting:
       --ai        grok | chatgpt | gemini   (default: grok)
       --voice     suara edge-tts (default id-ID-ArdiNeural / pria)
                   contoh wanita: id-ID-GadisNeural
       --persona   gaya jawaban AI (default: host live ramah, jawaban singkat)
       --device    nama device output audio (kalau pakai VB-CABLE,
                   isi sebagian nama "CABLE" — butuh paket sounddevice)
       --max-chars batas panjang jawaban biar ngomongnya nggak kepanjangan
──────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import tempfile
import threading
import urllib.request
import urllib.error
from pathlib import Path

# ── Konfigurasi default (selaras dgn ai_chat_bridge_config.json) ────────
BRIDGE_URL = "http://localhost:5100/v1/chat/completions"
BRIDGE_KEY = "kuda"      # samakan dengan api_key di config Anda
DEFAULT_AI = "grok"      # grok | chatgpt | gemini
DEFAULT_VOICE = "id-ID-ArdiNeural"   # pria. wanita: id-ID-GadisNeural

DEFAULT_PERSONA = (
    "Kamu adalah host TikTok LIVE yang ramah dan natural. "
    "Jawab komentar penonton dengan SINGKAT (maksimal 2 kalimat), santai, "
    "seperti ngobrol langsung. Jangan pakai emoji, jangan pakai format markdown, "
    "jangan sebut bahwa kamu AI. Sebut nama penonton kalau ada."
)


# ── 1. Panggil AI Chat Bridge (OpenAI-compatible) ───────────────────────
def ask_bridge(ai_model: str, persona: str, username: str, comment: str,
               max_chars: int = 220) -> str:
    """Kirim komentar ke bridge, balikkan teks jawaban."""
    payload = {
        "model": ai_model,
        "messages": [
            {"role": "system", "content": persona},
            {"role": "user",
             "content": f"Penonton bernama '{username}' berkomentar: \"{comment}\". "
                        f"Balas komentar ini sebagai host live."},
        ],
        # paksa chat baru tiap komentar biar tidak menumpuk konteks panjang
        "bridge_options": {"new_chat": True},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {BRIDGE_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        return f"Maaf, ada gangguan teknis sebentar ya."
    except Exception as e:
        print(f"[bridge error] {e}")
        return ""
    # buang tag media/markdown sisa, potong kepanjangan
    text = text.replace("```", "").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "."
    return text


# ── 2. Text-to-Speech via edge-tts (gratis) → file mp3 ──────────────────
async def _tts_to_file(text: str, voice: str, out_path: Path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


def synth_voice(text: str, voice: str) -> Path:
    out = Path(tempfile.gettempdir()) / f"ttslive_{abs(hash(text)) % 10**8}.mp3"
    asyncio.run(_tts_to_file(text, voice, out))
    return out


# ── 3. Putar mp3 (ke speaker, atau ke device tertentu/VB-CABLE) ─────────
def play_audio(path: Path, device_match: str | None = None):
    if device_match:
        # Jalur device-spesifik: butuh sounddevice + soundfile
        try:
            import sounddevice as sd
            import soundfile as sf
            import numpy as np  # noqa
            # cari index device output yang namanya cocok
            dev_index = None
            for i, d in enumerate(sd.query_devices()):
                if device_match.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                    dev_index = i
                    break
            if dev_index is None:
                print(f"[audio] device '{device_match}' tidak ketemu, pakai default.")
            data, sr = sf.read(str(path), dtype="float32")
            sd.play(data, sr, device=dev_index)
            sd.wait()
            return
        except ImportError:
            print("[audio] sounddevice/soundfile belum terpasang "
                  "(pip install sounddevice soundfile). Fallback ke speaker biasa.")
    # Jalur default: speaker sistem
    try:
        from playsound import playsound
        playsound(str(path))
    except Exception as e:
        # fallback terakhir: putar pakai pemutar OS
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 || afplay "{path}"')
        print(f"[audio] playsound gagal ({e}); pakai fallback OS.")


# ── 4. Worker antrian: proses 1 komentar dalam satu waktu ───────────────
class VoiceWorker(threading.Thread):
    def __init__(self, ai_model, persona, voice, device_match, max_chars):
        super().__init__(daemon=True)
        self.q: queue.Queue = queue.Queue()
        self.ai_model = ai_model
        self.persona = persona
        self.voice = voice
        self.device_match = device_match
        self.max_chars = max_chars
        self.running = True

    def submit(self, username: str, comment: str):
        # batasi antrian biar tidak menumpuk kalau live ramai
        if self.q.qsize() < 8:
            self.q.put((username, comment))

    def run(self):
        while self.running:
            try:
                username, comment = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            print(f"\n💬 {username}: {comment}")
            answer = ask_bridge(self.ai_model, self.persona,
                                username, comment, self.max_chars)
            if not answer:
                continue
            print(f"🤖 {self.ai_model}: {answer}")
            try:
                mp3 = synth_voice(answer, self.voice)
                play_audio(mp3, self.device_match)
                try:
                    mp3.unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"[tts/play error] {e}")


# ── 5. Connect TikTok LIVE & dengarkan komentar ─────────────────────────
def run_live(username: str, worker: VoiceWorker, reply_gifts: bool):
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent, GiftEvent

    client = TikTokLiveClient(unique_id=username)

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        print(f"✅ Terhubung ke LIVE @{event.unique_id} (room {client.room_id})")
        print("   Menunggu komentar... (Ctrl+C untuk berhenti)\n")

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        worker.submit(event.user.nickname or event.user.unique_id, event.comment)

    if reply_gifts:
        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            # hanya respon saat streak gift selesai (atau gift non-streak)
            if event.gift.streakable and not event.streaking:
                name = event.user.nickname or event.user.unique_id
                worker.submit(name, f"(baru saja mengirim gift {event.gift.name}) terima kasih ya!")
            elif not event.gift.streakable:
                name = event.user.nickname or event.user.unique_id
                worker.submit(name, f"(mengirim gift {event.gift.name}) makasih banyak!")

    client.run()


def main():
    ap = argparse.ArgumentParser(description="Auto-jawab komentar TikTok LIVE pakai suara.")
    ap.add_argument("--user", required=True, help="username TikTok, contoh @namaakun")
    ap.add_argument("--ai", default=DEFAULT_AI, choices=["grok", "chatgpt", "gemini"])
    ap.add_argument("--voice", default=DEFAULT_VOICE,
                    help="suara edge-tts (id-ID-ArdiNeural / id-ID-GadisNeural)")
    ap.add_argument("--persona", default=DEFAULT_PERSONA)
    ap.add_argument("--device", default=None,
                    help="sebagian nama device output (mis. 'CABLE') untuk VB-CABLE")
    ap.add_argument("--max-chars", type=int, default=220)
    ap.add_argument("--reply-gifts", action="store_true",
                    help="juga ucapkan terima kasih saat ada gift")
    args = ap.parse_args()

    username = args.user if args.user.startswith("@") else "@" + args.user

    print("─" * 60)
    print(f"  TikTok LIVE Auto-Reply (suara)")
    print(f"  Akun     : {username}")
    print(f"  AI bridge: {args.ai}  (via {BRIDGE_URL})")
    print(f"  Suara    : {args.voice}")
    print(f"  Output   : {args.device or 'speaker default'}")
    print("─" * 60)

    worker = VoiceWorker(args.ai, args.persona, args.voice,
                         args.device, args.max_chars)
    worker.start()

    try:
        run_live(username, worker, args.reply_gifts)
    except KeyboardInterrupt:
        print("\n👋 Berhenti.")
    except Exception as e:
        print(f"\n❌ Error koneksi LIVE: {e}")
        print("   Pastikan: akun sedang LIVE, username benar, dan internet OK.")


if __name__ == "__main__":
    main()
