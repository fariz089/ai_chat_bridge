# FakeFluencer V2 — Web (CDP + Chrome profiles + Docker)

Arsitektur baru: **semua akun = Chrome profile, diakses lewat CDP.** Tambah akun
= tambah profil (satu Chrome, satu port CDP). Tidak ada lagi file cookie atau
Chrome extension — semuanya seragam.

```
┌─────────────┐         CDP (9301, 9302, …)        ┌────────────────────────┐
│   web       │ ───────────────────────────────▶  │   chrome               │
│  Flask +    │                                    │  N× Google Chrome      │
│  Playwright │   media/ profiles/ (volume)        │  (1 per profil)        │
│  :5000      │ ◀───────────────────────────────  │  + noVNC :6080 (login) │
└─────────────┘                                    └────────────────────────┘
```

- **web**: UI + engine Playwright. Hanya *connect* ke Chrome via CDP; tidak
  pernah menjalankan browser sendiri.
- **chrome**: menjalankan satu Google Chrome per profil (lihat `profiles.json`),
  semua tampil di satu layar virtual yang dipublish lewat **noVNC** untuk login.

## Mulai cepat (server Ubuntu)

```bash
# 1. taruh seluruh project di server, lalu:
docker compose up -d --build

# 2. buka UI, tambah profil
#    http://SERVER_IP:5000/dashboard  →  tab Bank  →  "Tambah profil"
#    (mis. Grok:main, ChatGPT:main, Gemini:main)

# 3. nyalakan Chrome untuk profil baru
docker compose restart chrome

# 4. login tiap profil SEKALI lewat noVNC
#    http://SERVER_IP:6080
#    → akan terlihat jendela Chrome per profil; login ke akun masing-masing.
#    Login menetap di ./profiles (volume), jadi cukup sekali.

# 5. pakai: tab Create / Chat → pilih profil → jalan.
```

Cek tiap profil sudah benar lewat tab **Bank → Tes** (mengirim 1 pesan uji).

## Kenapa login di server, bukan copy profil dari laptop?

Cookie login Google/X disimpan terikat ke OS + perangkat. Menyalin profil dari
Windows/Mac ke container Linux **sering gagal** (terutama Google). Jadi login
dilakukan langsung di profil yang ada di server, lewat noVNC. Sekali login,
permanen di volume `./profiles`.

## Menambah akun

1. Bank → Tambah profil (pilih platform + label, mis. `Grok : akun2`).
   → dapat port CDP berikutnya (9303, 9304, …) dan baris baru di `profiles.json`.
2. `docker compose restart chrome` → Chrome baru otomatis hidup.
3. noVNC → login akun itu.

Hapus profil dari UI hanya menghapus dari daftar; folder `./profiles/<id>` tetap
ada (hapus manual bila ingin benar-benar bersih).

## Data persisten (volume bind-mount)

| Folder | Isi |
|--------|-----|
| `./profiles/` | profil Chrome tiap akun (login tersimpan di sini) |
| `./profiles.json` | peta akun ↔ port CDP (ditulis oleh UI, dibaca container chrome) |
| `./media/` | output: gambar, video, ZIP |
| `./uploads/` | foto referensi yang diunggah |

Backup = salin keempatnya.

## Halaman

| Tab | Isi |
|-----|-----|
| **Dashboard** | status CDP, jumlah profil & yang terjangkau, job berjalan |
| **Create** | generator: pilih profil skrip + profil gambar, upload, → ZIP |
| **Chat** | ChatGPT/Grok/Gemini per profil; Grok + Imagine; media inline |
| **Bank** | kelola profil (tambah/hapus/tes), link noVNC untuk login |
| **History** | galeri output |
| **Settings** | API server OpenAI-compatible, gabung video LIVE, info CDP |

## LIVE

Bagian LIVE yang **portabel** (menggabungkan scene `.mp4` jadi satu video
panjang untuk source TikTok LIVE) ada di **Settings → LIVE**. Auto-reply suara
(butuh perangkat audio VB-CABLE) **tetap di aplikasi desktop** dan tidak
dipindah ke web.

## Tanpa Docker (dev lokal)

Butuh Chrome berjalan dengan CDP sendiri, mis.:
```bash
google-chrome --user-data-dir=./profiles/grok_main --remote-debugging-port=9301 &
CDP_HOST=127.0.0.1 python webapp/app.py --port 5000
```
Tapi alur Docker jauh lebih mudah karena supervisor mengurus semua Chrome.

## Keamanan

Jangan ekspos `:5000` dan `:6080` langsung ke internet. Pakai Tailscale, SSH
tunnel (`ssh -L 5000:localhost:5000 -L 6080:localhost:6080 server`), atau
reverse-proxy dengan autentikasi. noVNC di setup ini **tanpa password** dan
hanya untuk diakses lewat tunnel/jaringan privat.

## Catatan jujur / risiko

- **Google sering minta verifikasi di IP datacenter.** Login Gemini/AI Studio
  di server bisa rewel; kadang perlu beberapa kali atau verifikasi perangkat.
- **Sesi bisa kedaluwarsa** — kalau Bank menandai profil `perlu login`, buka
  noVNC dan login ulang di profil itu.
- Setup ini belum diuji end-to-end dengan login hidup di lingkungan ini (tanpa
  jaringan). Logika & wiring sudah lolos tes; mohon tes sekali di servermu.
