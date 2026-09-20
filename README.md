# Bot rekap GoPay (Telegram → Excel format Jago)

Kirim screenshot **Riwayat transaksi GoPay** ke bot. Bot membaca tanggal, item, dan nominal, lalu
membuat Excel berformat `Jago_September_2024.xlsx` untuk rekonsiliasi dan analisis perpindahan uang.
Hasilnya adalah **rekap internal**, bukan e-statement resmi GoPay. OVO menyusul setelah ada contoh layarnya.

## Deploy ke Railway

1. Buat bot lewat @BotFather, simpan token.
2. Push folder ini ke repo GitHub, lalu di Railway: **New Project → Deploy from GitHub repo**.
   Railway memakai `Dockerfile` (Tesseract + bahasa Indonesia terpasang saat build).
3. **Variables**:
   - `TELEGRAM_BOT_TOKEN` = token dari BotFather
   - `ALLOWED_USER_IDS` = ID Telegram kamu. Belum tahu? Isi `0`, deploy, kirim `/id` ke bot, lalu ganti.
   - `OCR_PROVIDER=tesseract` (bawaan)
4. **Volume** (disarankan): tambahkan Volume ke service, mount path `/data`. Railway mengisi
   `RAILWAY_VOLUME_MOUNT_PATH` otomatis dan bot memakainya. Tanpa Volume, sesi hilang tiap redeploy.
5. Jalankan **tepat 1 replika** (long polling; 2 replika akan saling bentrok). Tidak perlu domain publik.

> Cek log saat start: baris `data=/data` berarti Volume terpasang. Kalau tertulis `data=data`,
> Volume belum terpasang dan sesi akan hilang saat redeploy.
> Token bot tidak lagi tercetak di log. Jika pernah tercetak/terkirim ke pihak lain, ganti lewat BotFather (`/revoke`).

## Cara pakai

1. `/saldoawal 62620.36` — saldo GoPay sebelum transaksi pertama (screenshot tidak memuat saldo).
2. Kirim screenshot berurutan dari atas ke bawah (boleh album). **Kirim sebagai File** (📎 → File);
   mode Photo dikompres Telegram dan tanggal/metode bisa hilang.
3. Cek balasan bot. Baris bertanda ⚠ perlu dicek. Perbaiki dengan
   `/ubah <no> ket|nominal|tgl|metode <nilai>` atau `/hapus <no>`. `/daftar` menampilkan semua baris.
4. `/rekap` → unduh Excel. Isi kolom **C** dan **I** secara manual.
5. Samakan **Saldo Akhir** di Excel dengan saldo di aplikasi. Selisih berarti ada baris salah baca atau terlewat.

## Isi Excel

| Kolom | Isi |
|---|---|
| A Tanggal | dari header tanggal di layar |
| B Keterangan Transaksi | nama item/merchant seperti terbaca |
| C Kategori Transaksi | **manual**; warna baris muncul otomatis untuk kategori di `CATEGORY_FILLS` |
| D Debit / E Kredit | debit bertanda minus |
| F Saldo Kumulatif | rumus `=D+E+F(baris sebelumnya)` |
| G / H Subjek / Objek | debit: `GoPay` → merchant; kredit: sumber → `GOPAY` (konvensi file acuan) |
| I Keterangan Tambahan | **manual** |

- **GoPay Coins / Later** bukan saldo, jadi dipisah ke sheet **Non-Saldo**.
- Dalam satu hari, urutan layar (terbaru di atas) dibalik menjadi kronologis. Ini asumsi.
- Nama merchant berhuruf merah + komentar = OCR kurang yakin.
- Rumus baru terhitung saat dibuka di Excel/Sheets/LibreOffice; beberapa previewer menampilkan kosong.

## Status verifikasi

Sudah diuji: foto layar GoPay contoh (4/4 baris benar), 18 unit test, Excel dihitung ulang di
LibreOffice (0 error, saldo cocok hitungan manual), alur bot dengan mock Telegram.

**Belum diuji:** koneksi Telegram sungguhan, build Docker/deploy Railway, album multi-foto,
mode gelap, screenshot GoPay dengan tampilan/versi aplikasi lain.

Batas yang diketahui:
- Nominal terbaca andal; **nama merchant lebih rentan salah** — bot menandainya, tapi tetap dicek.
- Foto layar yang dikompres Telegram kehilangan tanggal/metode (bot menandai baris itu).
- Tidak ada koreksi perspektif; foto sangat miring bisa gagal.
- Dua transaksi identik tepat di batas dua screenshot dianggap tumpang tindih dan dilewati (bot memberi tahu).

## Struktur

```
app/parser.py         baca layout GoPay dari koordinat kata OCR (tanggal, nominal, metode, arah)
app/ocr/              OCR_PROVIDER; tesseract_provider.py = crop layar, normalisasi, 3 pass, OCR ulang area nama
app/excel_export.py   tulis Excel format Jago (ubah warna kategori di CATEGORY_FILLS)
app/session.py        sesi per chat (JSON di volume), deteksi tumpang tindih
app/bot.py            handler Telegram
scripts/try_image.py  uji satu gambar tanpa Telegram:  python scripts/try_image.py foto.jpg
tests/                pytest
```

Menambah OVO: jalankan `scripts/try_image.py` pada screenshot OVO, setel `parse_page`, lalu tambahkan
`"OVO"` ke `SUPPORTED_WALLETS` di `app/config.py`.
Ganti OCR: buat kelas turunan `OcrProvider` (method `passes`) dan daftarkan di `app/ocr/__init__.py`.
