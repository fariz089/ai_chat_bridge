"""
merge_scenes_video.py
─────────────────────
Gabungkan banyak scene .mp4 (hasil Grok Imagine dari Generator Tab) menjadi
SATU video panjang yang siap dipasang sebagai source "Video" di TikTok LIVE.

Kenapa perlu: tiap scene cuma 6–10 detik. Untuk live, lebih enak satu file
panjang. Script ini juga bisa MENGULANG (loop) sampai durasi target supaya
videonya awet diputar saat live.

Butuh: ffmpeg terpasang di PATH.
   Windows: download dari https://www.gyan.dev/ffmpeg/builds/ lalu tambahkan
            folder bin ke PATH. Cek: `ffmpeg -version`

CONTOH PEMAKAIAN
────────────────
# Gabung semua mp4 di folder media/generated (urut nama):
    python merge_scenes_video.py --in media/generated --out live_video.mp4

# Gabung file tertentu sesuai urutan:
    python merge_scenes_video.py --files a.mp4 b.mp4 c.mp4 --out live_video.mp4

# Gabung lalu ulang sampai total minimal 5 menit (300 detik):
    python merge_scenes_video.py --in media/generated --out live_video.mp4 --loop-to 300
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}


def _check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit("❌ ffmpeg tidak ditemukan di PATH. Install dulu (lihat header file).")


def collect_inputs(in_dir: str | None, files: list[str] | None) -> list[Path]:
    if files:
        paths = [Path(f) for f in files]
    else:
        d = Path(in_dir)
        paths = sorted(p for p in d.iterdir()
                       if p.suffix.lower() in VIDEO_EXTS)
    paths = [p for p in paths if p.exists()]
    if not paths:
        sys.exit("❌ Tidak ada file video yang ditemukan.")
    return paths


def _probe_duration(path: Path) -> float:
    """Total durasi 1 file (detik) via ffprobe; 0 kalau gagal."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip())
    except Exception:
        return 0.0


def merge(paths: list[Path], out: Path, loop_to: float | None):
    """
    Re-encode semua input ke parameter seragam lalu concat. Re-encode dipakai
    (bukan stream-copy) supaya aman walau scene punya resolusi/fps berbeda.
    """
    tmp = Path(tempfile.mkdtemp(prefix="merge_scenes_"))
    norm_files: list[Path] = []

    print(f"🎬 Menormalkan {len(paths)} klip (1080x1920 vertikal, 30fps)...")
    for i, p in enumerate(paths):
        dst = tmp / f"norm_{i:03d}.mp4"
        # skala ke 1080x1920 (vertikal TikTok), pad biar tidak gepeng, 30fps,
        # audio AAC stereo 44.1k supaya concat mulus.
        vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
              "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1")
        cmd = ["ffmpeg", "-y", "-i", str(p),
               "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-ar", "44100", "-ac", "2",
               "-pix_fmt", "yuv420p", str(dst)]
        # kalau klip tak punya audio, tambahkan track sunyi
        subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)
        if not dst.exists():
            # coba lagi dengan generate audio sunyi
            cmd2 = ["ffmpeg", "-y", "-i", str(p),
                    "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=44100",
                    "-vf", vf, "-shortest",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-pix_fmt", "yuv420p", str(dst)]
            subprocess.run(cmd2, check=True, stderr=subprocess.DEVNULL)
        norm_files.append(dst)
        print(f"   ✓ {p.name}")

    # concat list
    list_file = tmp / "list.txt"
    list_file.write_text(
        "\n".join(f"file '{f.as_posix()}'" for f in norm_files),
        encoding="utf-8")

    base_out = tmp / "joined.mp4"
    print("🔗 Menggabungkan...")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(base_out)],
        check=True, stderr=subprocess.DEVNULL)

    if loop_to:
        one_dur = _probe_duration(base_out)
        if one_dur <= 0:
            print("⚠️  Tidak bisa baca durasi; lewati loop.")
            shutil.copy(base_out, out)
        else:
            reps = max(1, int(loop_to // one_dur) + 1)
            print(f"🔁 Mengulang {reps}x supaya total ≥ {loop_to:.0f}s "
                  f"(1 putaran = {one_dur:.0f}s)...")
            subprocess.run(
                ["ffmpeg", "-y", "-stream_loop", str(reps - 1),
                 "-i", str(base_out), "-c", "copy",
                 "-t", str(loop_to), str(out)],
                check=True, stderr=subprocess.DEVNULL)
    else:
        shutil.copy(base_out, out)

    shutil.rmtree(tmp, ignore_errors=True)
    final_dur = _probe_duration(out)
    print(f"\n✅ Selesai: {out}  ({final_dur:.0f} detik)")
    print("   Pasang di TikTok LIVE Studio: Add source → Video → pilih file ini.")


def main():
    ap = argparse.ArgumentParser(description="Gabung scene mp4 jadi 1 video live.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--in", dest="in_dir", help="folder berisi .mp4 (urut nama)")
    g.add_argument("--files", nargs="+", help="daftar file mp4 sesuai urutan")
    ap.add_argument("--out", default="live_video.mp4", help="file output")
    ap.add_argument("--loop-to", type=float, default=None,
                    help="ulang sampai total detik ini (mis. 300 = 5 menit)")
    args = ap.parse_args()

    _check_ffmpeg()
    paths = collect_inputs(args.in_dir, args.files)
    print("Urutan klip:")
    for p in paths:
        print(f"  - {p.name}")
    merge(paths, Path(args.out), args.loop_to)


if __name__ == "__main__":
    main()
