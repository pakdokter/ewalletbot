"""Bot Telegram: screenshot GoPay/OVO -> rekap Excel (format Jago) untuk rekonsiliasi."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytesseract
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import config
from .excel_export import build_workbook
from .models import Txn
from .ocr import get_provider
from .parser import parse_image
from .session import Store, parse_date_input, parse_rupiah

log = logging.getLogger("ewallet_bot")
store = Store(config.DATA_DIR)
provider = None                      # diisi di main() setelah cek konfigurasi
_locks: dict[int, asyncio.Lock] = {}

HELP = (
    "<b>Bot rekap GoPay</b> <i>(OVO menyusul)</i>\n"
    "Kirim screenshot <i>Riwayat transaksi</i> GoPay, saya baca tanggal, item, dan nominalnya, "
    "lalu kamu bisa unduh Excel berformat Jago.\n\n"
    "<b>Alur</b>\n"
    "1. /saldoawal 62620.36 - saldo dompet sebelum transaksi pertama\n"
    "2. Kirim screenshot berurutan dari atas ke bawah (boleh album). "
    "<b>Kirim sebagai File</b> (📎 → File) agar tidak dikompres Telegram.\n"
    "3. Cek hasil; perbaiki dengan /ubah atau /hapus\n"
    "4. /rekap - unduh Excel\n\n"
    "<b>Perintah</b>\n"
    "/daftar - lihat semua baris\n"
    "/ubah &lt;no&gt; ket|nominal|tgl|metode &lt;nilai&gt; - koreksi baris\n"
    "/hapus &lt;no&gt; [no ...] - hapus baris\n"
    "/reset - kosongkan sesi\n"
    "/id - tampilkan ID Telegram kamu\n\n"
    "Nominal dan tanggal dibaca dengan cukup andal; <b>nama merchant lebih rentan salah</b>. "
    "Baris bertanda ⚠ perlu kamu cek."
)


# ---------------------------------------------------------------- util
def rp(n: int | float) -> str:
    s = f"{abs(round(n)):,}".replace(",", ".")
    return f"-{s}" if n < 0 else s


def rp2(n: float) -> str:
    return f"{n:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def short_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%y") if iso else "??/??/??"


def table(rows: list[tuple[int, Txn]]) -> str:
    out = [" #  Tanggal   Keterangan                     Nominal"]
    for i, t in rows:
        mark = "⚠" if t.flags else " "
        tag = "" if t.method.lower().endswith(("saldo", "cash")) or t.method == "?" else f" ({t.method.split()[-1]})"
        name = t.desc if len(t.desc) <= 22 else t.desc[:21] + "…"
        out.append(f"{i:>2}{mark} {short_date(t.date)}  {(name + tag):28s} {rp(t.amount):>11}")
    return "<pre>" + html.escape("\n".join(out)) + "</pre>"


def flag_lines(rows: list[tuple[int, Txn]]) -> str:
    return "\n".join(f"⚠ {i}: {html.escape('; '.join(t.flags))}" for i, t in rows if t.flags)


async def send_html(msg: Message, text: str) -> None:
    """Kirim sebagai HTML; jika Telegram menolak markup, kirim ulang sebagai teks polos agar hasil tidak hilang."""
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)
    except BadRequest:
        log.exception("HTML ditolak Telegram, kirim ulang sebagai teks polos")
        plain = html.unescape(re.sub(r"</?(b|i|pre|code)>", "", text))
        await msg.reply_text(plain)


async def reply_chunks(msg: Message, rows: list[tuple[int, Txn]], head: str = "", tail: str = "") -> None:
    chunks = [rows[i:i + 25] for i in range(0, len(rows), 25)] or [[]]
    for n, ch in enumerate(chunks):
        text = (head if n == 0 else "") + (table(ch) if ch else "")
        if n == len(chunks) - 1:
            text += tail
        await send_html(msg, text)


def authorized(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or u.id not in config.ALLOWED_USER_IDS:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Akses ditolak. ID Telegram kamu: {u.id if u else '?'}. "
                    "Jika ini kamu, tambahkan ke ALLOWED_USER_IDS lalu redeploy.")
            log.warning("akses ditolak untuk user %s", u.id if u else None)
            return
        return await fn(update, ctx)
    return wrapper


def lock_for(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


# ---------------------------------------------------------------- perintah
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID Telegram kamu: {update.effective_user.id}")


@authorized
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


@authorized
async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg == "gopay":
        s = store.get(update.effective_chat.id)
        s.wallet = "GoPay"
        store.save(s)
        await update.message.reply_text("Dompet: GoPay.")
    else:
        await update.message.reply_text("Saat ini hanya GoPay yang didukung (OVO menunggu contoh layar). Pakai /wallet gopay.")


@authorized
async def cmd_saldoawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.get(update.effective_chat.id)
    try:
        s.saldo_awal = parse_rupiah(" ".join(ctx.args))
    except (ValueError, IndexError):
        await update.message.reply_text("Contoh: /saldoawal 62620.36  atau  /saldoawal 1.250.000")
        return
    store.save(s)
    await update.message.reply_text(f"Saldo awal: Rp {rp2(s.saldo_awal)}")


@authorized
async def cmd_daftar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.get(update.effective_chat.id)
    if not s.rows:
        await update.message.reply_text("Belum ada transaksi. Kirim screenshot riwayat transaksi.")
        return
    rows = list(enumerate(s.rows, 1))
    await reply_chunks(update.message, rows, head=f"<b>{s.wallet or '?'}</b> - {len(rows)} baris (urutan seperti di layar)\n",
                       tail="\n" + flag_lines(rows))


@authorized
async def cmd_ubah(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.get(update.effective_chat.id)
    usage = "Contoh: /ubah 3 ket lapakgaming | /ubah 3 nominal 114300 | /ubah 3 tgl 01/01/2024 | /ubah 3 metode coins"
    if len(ctx.args) < 3 or not ctx.args[0].isdigit():
        await update.message.reply_text(usage)
        return
    idx, field, value = int(ctx.args[0]), ctx.args[1].lower(), " ".join(ctx.args[2:])
    if not 1 <= idx <= len(s.rows):
        await update.message.reply_text(f"Nomor harus 1-{len(s.rows)}.")
        return
    t = s.rows[idx - 1]
    try:
        if field in ("ket", "keterangan", "nama"):
            t.desc = value.strip()
        elif field == "nominal":
            v = parse_rupiah(value)
            explicit = value.strip().startswith(("-", "+", "("))
            sign = (-1 if v < 0 else 1) if explicit else (-1 if t.amount < 0 else 1)
            t.amount = sign * int(round(abs(v)))
        elif field in ("tgl", "tanggal"):
            t.date = parse_date_input(value)
        elif field == "metode":
            t.method = f"{t.wallet} {value.strip().capitalize()}"
        else:
            await update.message.reply_text(usage)
            return
    except ValueError as e:
        await update.message.reply_text(f"Nilai tidak valid: {e}")
        return
    t.flags = []
    store.save(s)
    await update.message.reply_text("Diubah:")
    await reply_chunks(update.message, [(idx, t)])


@authorized
async def cmd_hapus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.get(update.effective_chat.id)
    nums = sorted({int(a) for a in ctx.args if a.isdigit()}, reverse=True)
    if not nums or any(not 1 <= n <= len(s.rows) for n in nums):
        await update.message.reply_text(f"Contoh: /hapus 2 5   (nomor 1-{len(s.rows)}). Nomor akan bergeser setelah dihapus.")
        return
    for n in nums:
        del s.rows[n - 1]
    store.save(s)
    await update.message.reply_text(f"{len(nums)} baris dihapus. Sisa {len(s.rows)} baris - lihat /daftar untuk nomor terbaru.")


@authorized
async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.reset(update.effective_chat.id)
    await update.message.reply_text("Sesi dikosongkan (transaksi, saldo awal, dan pilihan dompet).")


@authorized
async def cmd_rekap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = store.get(update.effective_chat.id)
    if not s.rows:
        await update.message.reply_text("Belum ada transaksi.")
        return
    undated = [i for i, t in enumerate(s.rows, 1) if not t.date]
    if undated:
        await update.message.reply_text("Baris tanpa tanggal: " + ", ".join(map(str, undated)) +
                                        ". Isi dengan /ubah <no> tgl dd/mm/yyyy, lalu /rekap lagi.")
        return
    with tempfile.TemporaryDirectory() as d:
        dates = sorted(t.date for t in s.rows if t.date)
        path = Path(d) / f"{s.wallet or 'Dompet'}_{dates[0].replace('-', '')}_{dates[-1].replace('-', '')}.xlsx"
        info = await asyncio.to_thread(build_workbook, s, path)
        warn = []
        if s.saldo_awal is None:
            warn.append("Saldo awal belum diisi (dianggap 0) - kirim /saldoawal lalu /rekap lagi.")
        if info["flagged"]:
            warn.append(f"{info['flagged']} baris masih bertanda ⚠ (huruf merah + komentar di Excel).")
        caption = (f"{s.wallet}: {info['main']} baris saldo, {info['other']} baris non-saldo. "
                   "Kolom C dan I diisi manual. Samakan 'Saldo Akhir' dengan saldo di aplikasi. "
                   "Ini rekap internal, bukan e-statement resmi.")
        if warn:
            caption += "\n\n" + "\n".join(warn)
        with path.open("rb") as f:
            await update.message.reply_document(f, filename=path.name, caption=caption[:1000])


# ---------------------------------------------------------------- gambar
async def process_image(msg: Message, data: bytes, compressed: bool) -> None:
    chat_id = msg.chat_id
    async with lock_for(chat_id):
        s = store.get(chat_id)
        status = await msg.reply_text("⏳ Membaca gambar…")
        try:
            res = await asyncio.to_thread(parse_image, provider, data, s.wallet, s.last_date)
        except Exception:
            log.exception("OCR gagal")
            await status.edit_text("Gagal membaca gambar. Coba kirim ulang sebagai File (📎 → File).")
            return

        if res.detected and res.detected not in config.SUPPORTED_WALLETS:
            await status.edit_text(f"Gambar ini tampak dari {res.detected}. Bot baru mendukung GoPay; {res.detected} menyusul "
                                   "setelah ada contoh layarnya.")
            return
        if s.wallet and res.detected and res.detected != s.wallet:
            await status.edit_text(f"Gambar ini tampak dari {res.detected}, sedangkan sesi untuk {s.wallet}. "
                                   "Tidak ditambahkan. Kirim /reset bila ingin mulai dompet lain.")
            return
        if not s.wallet and not res.detected:
            await status.edit_text("Belum bisa mengenali ini sebagai GoPay. Pastikan halaman Riwayat transaksi GoPay, "
                                   "atau kirim /wallet gopay lalu kirim ulang.")
            return
        if not res.rows:
            await status.edit_text("Tidak ada baris transaksi yang terbaca. " + "; ".join(res.notes) +
                                   "\nPastikan yang dikirim halaman Riwayat transaksi; kirim sebagai File bila foto layar.")
            return

        if not s.wallet:
            s.wallet = res.detected
        fresh, dropped = s.add_rows(res.rows)
        store.save(s)
        await status.delete()
        if not fresh:
            await msg.reply_text(f"Tidak ada baris baru: {dropped} baris di gambar ini sama dengan akhir gambar sebelumnya "
                                 "(tumpang tindih). Jika ini transaksi berbeda, cek dengan /daftar.")
            return

        base = len(s.rows) - len(fresh)
        numbered = [(base + i, t) for i, t in enumerate(fresh, 1)]
        head = f"✅ <b>{len(fresh)} baris</b> ditambahkan ({s.wallet})\n"
        tail = "\n" + flag_lines(numbered)
        if dropped:
            tail += f"\nℹ️ {dropped} baris di awal gambar sama dengan akhir gambar sebelumnya (tumpang tindih) dan dilewati. " \
                    "Jika itu sebenarnya transaksi berbeda, tambahkan lewat /ubah pada baris terkait atau kirim ulang."
        for n in res.notes:
            tail += f"\nℹ️ {html.escape(n)}"
        if compressed and any(("tanggal tidak" in f or "metode tidak" in f) for t in fresh for f in t.flags):
            tail += "\n📎 Tanggal/metode hilang karena Telegram mengompres foto. Kirim ulang sebagai <b>File</b> (📎 → File)."
        if s.saldo_awal is None:
            tail += "\n💡 Belum ada saldo awal: /saldoawal &lt;nominal&gt;"
        tail += f"\nTotal sesi: {len(s.rows)} baris. Kirim screenshot berikutnya atau /rekap."
        await reply_chunks(msg, numbered, head=head, tail=tail)


async def _flush_group(key: tuple, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(1.5)                               # tunggu semua foto dalam album tiba
    items = sorted(ctx.bot_data["groups"].pop(key, []), key=lambda x: x[0])
    for _, message, data, compressed in items:             # urut sesuai pengiriman = urutan scroll
        await process_image(message, data, compressed)


@authorized
async def on_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        tg_file, compressed = await msg.photo[-1].get_file(), True
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        tg_file, compressed = await msg.document.get_file(), False
    else:
        return
    data = bytes(await tg_file.download_as_bytearray())
    if msg.media_group_id:
        key = (msg.chat_id, msg.media_group_id)
        groups = ctx.bot_data.setdefault("groups", {})
        first = key not in groups
        groups.setdefault(key, []).append((msg.message_id, msg, data, compressed))
        if first:
            asyncio.create_task(_flush_group(key, ctx))
        return
    await process_image(msg, data, compressed)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Error tak tertangani", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Terjadi kesalahan di bot. Data yang sudah masuk aman, cek dengan /daftar.")
        except Exception:
            pass


# ---------------------------------------------------------------- main
def main() -> None:
    global provider
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):          # URL request Telegram memuat token bot
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN belum diisi.")
    if not config.ALLOWED_USER_IDS:
        sys.exit("ALLOWED_USER_IDS kosong. Bot ini memproses data keuangan, jadi harus dibatasi ke ID Telegram tertentu. "
                 "Belum tahu ID? Isi sementara ALLOWED_USER_IDS=0, deploy, kirim /id ke bot, lalu ganti dengan ID itu.")
    kwargs = {"lang": config.TESSERACT_LANG} if config.OCR_PROVIDER == "tesseract" else {}
    provider = get_provider(config.OCR_PROVIDER, **kwargs)
    if config.OCR_PROVIDER == "tesseract":
        langs = set(pytesseract.get_languages())
        missing = {l for l in config.TESSERACT_LANG.split("+")} - langs
        if missing:
            sys.exit(f"Bahasa Tesseract belum terpasang: {', '.join(sorted(missing))}")
    log.info("Tesseract %s | provider=%s | data=%s", pytesseract.get_tesseract_version(), config.OCR_PROVIDER, config.DATA_DIR)

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("id", cmd_id))
    for name, fn in (("start", cmd_start), ("bantuan", cmd_start), ("wallet", cmd_wallet), ("saldoawal", cmd_saldoawal),
                     ("daftar", cmd_daftar), ("ubah", cmd_ubah), ("hapus", cmd_hapus), ("rekap", cmd_rekap), ("reset", cmd_reset)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
