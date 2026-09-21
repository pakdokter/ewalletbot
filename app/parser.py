"""Parser riwayat transaksi dari hasil OCR (koordinat kata), bukan dari teks polos.

Struktur layar GoPay (sudah divalidasi pada contoh nyata):
    [pil tanggal]  Selasa 2 Jan 2024
    [ikon]  Nama merchant / item                 -Rp114.300     <- nominal rata kanan
                                                 GoPay Saldo    <- metode di bawah nominal
  * minus di depan nominal = debit; tanpa minus = kredit (hijau)
  * "GoPay Coins" adalah koin, BUKAN saldo -> dipisah agar saldo berjalan tidak salah

OVO: memakai mesin yang sama tetapi BELUM divalidasi (belum ada contoh layar OVO).
Semua baris OVO yang arahnya (debit/kredit) hanya ditebak diberi tanda untuk dicek.
"""
from __future__ import annotations

import difflib
import re
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import cv2
import numpy as np

from . import config
from .models import ParseResult, Txn
from .ocr.base import OcrPage, OcrProvider, Word

WEEKDAYS = ["senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu"]   # index == date.weekday()
MONTH3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7,
          "agu": 8, "ags": 8, "agt": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "des": 12, "dec": 12}
MONTH_NAMES = {1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni", 7: "Juli",
               8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember"}

DATE_RE = re.compile(r"(?:\b(senin|selasa|rabu|kamis|jum'?at|sabtu|minggu)\b\s*)?(\d{1,2})\s*([A-Za-z]{3,9})\.?\s+(\d{4})", re.I)
MONTH_HDR_RE = re.compile(r"\b(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)\s+(\d{4})\b", re.I)
AMT_RE = re.compile(r"^(?P<sign>[-–—−+])?(?P<rp>rp)?\.?(?P<num>\d{1,3}(?:[.,]\d{3})+|\d{1,9})$", re.I)

METHOD_WORDS = {"saldo": "Saldo", "coins": "Coins", "coin": "Coins", "later": "Later", "paylater": "PayLater",
                "tabungan": "Tabungan", "cash": "Cash", "points": "Points", "point": "Points", "kartu": "Kartu"}

WALLETS = {
    "GoPay": {"main": "saldo"},
    "OVO": {"main": "cash"},
}
CREDIT_HINTS = ("top up", "topup", "terima", "cashback", "refund", "pengembalian", "transfer masuk", "bonus", "reward")

GREEN_MIN = 15      # dominasi hijau pada tinta nominal -> kredit (contoh nyata: kredit 26-29, debit -6)
NEUTRAL_MAX = 8


# ---------------------------------------------------------------- util
def parse_date_text(text: str):
    """Cari 'Selasa 2 Jan 2024' / '2 Januari 2024' dalam teks. -> (date, weekday_ok|None) atau None."""
    for m in DATE_RE.finditer(text):
        wd, d, mon, y = m.groups()
        month = MONTH3.get(mon.lower()[:3])
        if not month:
            continue
        try:
            dt = date(int(y), month, int(d))
        except ValueError:
            continue
        if not 2015 <= dt.year <= 2040:
            continue
        ok = None
        if wd:
            ok = WEEKDAYS.index(wd.lower().replace("'", "")) == dt.weekday()
        return dt, ok
    return None


def is_main_balance(method: str, wallet: str) -> bool:
    """True bila baris memengaruhi saldo dompet (Saldo/Cash). Coins/Points/Later dipisah."""
    m = method.lower()
    if method == "?":
        return True     # tak diketahui: tetap dihitung tapi sudah diberi tanda
    return WALLETS.get(wallet, {"main": "saldo"})["main"] in m


@dataclass
class _Line:
    cy: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def _cluster_lines(words: Iterable[Word]) -> list[_Line]:
    ws = [w for w in words if w.conf >= 0]
    if not ws:
        return []
    mh = statistics.median(w.height for w in ws if w.conf > 30) if any(w.conf > 30 for w in ws) else 20
    lines: list[_Line] = []
    for w in sorted(ws, key=lambda w: w.cy):
        if lines and abs(w.cy - lines[-1].cy) <= 0.6 * mh:
            lines[-1].words.append(w)
            lines[-1].cy = statistics.fmean(x.cy for x in lines[-1].words)
        else:
            lines.append(_Line(w.cy, [w]))
    for l in lines:
        l.words.sort(key=lambda w: w.left)
    return lines


def _merge_split_amounts(words: list[Word]) -> list[Word]:
    """Gabungkan '-' + 'Rp146.000' atau 'Rp' + '500.000' yang terpecah oleh OCR."""
    out = list(words)
    for _ in range(2):
        merged = True
        while merged:
            merged = False
            for a in out:
                if a.text not in {"-", "–", "—", "−", "+", "Rp", "rp", "-Rp", "+Rp", "Rp."}:
                    continue
                for b in out:
                    if b is a or not (b.text[:1].isdigit() or b.text.lower().startswith("rp")):
                        continue
                    gap = b.left - a.right
                    if -5 <= gap <= 1.3 * a.height and abs(a.cy - b.cy) <= 0.7 * max(a.height, b.height):
                        new = Word(a.text + b.text, a.left, min(a.top, b.top), b.right - a.left,
                                   max(a.bottom, b.bottom) - min(a.top, b.top), min(a.conf, b.conf))
                        out = [w for w in out if w is not a and w is not b] + [new]
                        merged = True
                        break
                if merged:
                    break
    return out


def _green_score(page: OcrPage, w: Word) -> float | None:
    """Dominasi hijau pada 'tinta' kata (piksel tergelap). Hijau = kredit di GoPay."""
    if page.image_bgr is None or not page.color_reliable:
        return None
    h, wd = page.image_bgr.shape[:2]
    x0, y0, x1, y1 = max(0, w.left), max(0, w.top), min(wd, w.right), min(h, w.bottom)
    roi = page.image_bgr[y0:y1, x0:x1]
    if roi.size < 60:
        return None
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).reshape(-1)
    idx = np.argsort(gray)[: max(10, len(gray) // 8)]
    b, g, r = roi.reshape(-1, 3)[idx].astype(int).mean(axis=0)
    return float(g - max(b, r))


def detect_wallet(words: list[Word]) -> str | None:
    text = " ".join(w.text.lower() for w in words)
    g = len(re.findall(r"go\s?pay|gojek", text))
    o = len(re.findall(r"\bovo\b", text))
    if g == o:
        return None
    return "GoPay" if g > o else "OVO"


def _detect_method(words: list[Word], wallet: str) -> str | None:
    for w in sorted(words, key=lambda w: w.left):
        tok = re.sub(r"[^a-z]", "", w.text.lower())
        if len(tok) < 4:
            continue
        m = difflib.get_close_matches(tok, list(METHOD_WORDS), n=1, cutoff=0.72)
        if m:
            return f"{wallet} {METHOD_WORDS[m[0]]}"
    return None


def _looks_like_date_header(l: _Line) -> bool:
    """Baris pendek tanpa nominal yang memuat tahun (20xx) atau nama hari -> kemungkinan header tanggal rusak."""
    t = l.text
    if "rp" in t.lower() or len(l.words) > 6:
        return False
    if re.search(r"\b20\d{2}\b", t):
        return True
    first = re.sub(r"[^a-z]", "", l.words[0].text.lower())
    return len(first) >= 4 and bool(difflib.get_close_matches(first, WEEKDAYS, n=1, cutoff=0.75))


def _clean_title(text: str) -> str:
    toks = text.replace("“", '"').replace("”", '"').split()

    def noise(t: str) -> bool:
        return len(t) == 1 or bool(re.search(r"[^\w\-'&.\"]", t))

    while len(toks) > 1 and noise(toks[0]):
        toks.pop(0)
    while len(toks) > 1 and noise(toks[-1]):
        toks.pop()
    return re.sub(r"\s+", " ", " ".join(toks)).strip(" \"'|_")


# ---------------------------------------------------------------- parser satu halaman
def parse_page(page: OcrPage, wallet_hint: str | None = None, carry_date: str | None = None) -> ParseResult:
    W, H = page.width, page.height
    notes: list[str] = []
    detected = detect_wallet(page.words)
    wallet = wallet_hint or detected
    lines = _cluster_lines(page.words)

    # --- header tanggal & bulan
    date_headers: list[tuple[float, date, bool | None]] = []
    month_headers: list[tuple[float, int, int]] = []
    suspect_headers: list[float] = []      # baris yang tampak seperti header tanggal tetapi gagal diparse
    header_words: set[int] = set()
    for l in lines:
        r = parse_date_text(l.text)
        if r and "rp" not in l.text.lower():
            date_headers.append((l.cy, r[0], r[1]))
            header_words |= {id(w) for w in l.words}
            continue
        m = MONTH_HDR_RE.search(l.text)
        if m:
            mon = MONTH3[m.group(1).lower()[:3]]
            month_headers.append((l.cy, mon, int(m.group(2))))
            header_words |= {id(w) for w in l.words}
            continue
        if _looks_like_date_header(l):
            suspect_headers.append(l.cy)

    # --- batas area daftar (di bawah filter, di atas navigasi bawah)
    top = 0.10 * H
    bottom = float(H)
    for l in lines:
        t = l.text.lower()
        if l.cy < 0.45 * H and any(k in t for k in ("atur tanggal", "aturtanggal", "layanan", "metode", "riwayat transaksi", "download")):
            top = max(top, max(w.bottom for w in l.words))
        if l.cy > 0.6 * H and sum(k in t for k in ("beranda", "keuangan", "qris", "riwayat", "profil", "home", "akun")) >= 2:
            bottom = min(bottom, min(w.top for w in l.words))

    # --- kandidat nominal (kolom kanan)
    words = _merge_split_amounts([w for w in page.words if w.conf >= 0])
    amts: list[tuple[Word, int, int, bool]] = []    # (word, sign_text: -1/0/+1, nilai, ada "Rp")
    for w in words:
        if not (top <= w.cy <= bottom) or w.left < 0.45 * W or id(w) in header_words:
            continue
        t = w.text.strip().rstrip("|:;")
        m = AMT_RE.match(t)
        if not m:
            if re.search(r"\d", t) and len(t) >= 3 and not re.fullmatch(r"\d{1,2}[.:]\d{2}", t):
                notes.append(f"angka tak terbaca di kolom nominal: '{t}'")
            continue
        grouped = bool(re.search(r"[.,]", m["num"]))
        if not (m["rp"] or grouped):
            continue
        value = int(re.sub(r"[.,]", "", m["num"]))
        if value <= 0:
            continue
        sign = {"-": -1, "–": -1, "—": -1, "−": -1, "+": 1}.get(m["sign"] or "", 0)
        amts.append((w, sign, value, bool(m["rp"])))
    amts.sort(key=lambda x: x[0].cy)

    if not amts:
        return ParseResult(wallet, [], notes + ["tidak ada baris transaksi terdeteksi"], carry_date, page.pass_name, detected)

    gaps = [amts[i + 1][0].cy - amts[i][0].cy for i in range(len(amts) - 1)]
    rowh = statistics.median(gaps) if gaps else 0.10 * H
    medh = statistics.median(w.height for w in words if w.conf > 30) if any(w.conf > 30 for w in words) else 20
    line_idx = {id(w): i for i, l in enumerate(lines) for w in l.words}

    rows: list[Txn] = []
    used: set[int] = set()
    for i, (a, sign, value, had_rp) in enumerate(amts):
        flags: list[str] = []
        band_top = (amts[i - 1][0].cy + a.cy) / 2 if i else max(top, a.cy - 0.55 * rowh)
        band_bot = (a.cy + amts[i + 1][0].cy) / 2 if i + 1 < len(amts) else min(bottom, a.cy + 0.55 * rowh)

        # metode: kata di kolom kanan tepat di bawah nominal
        # (baris judul ada ~0.8x tinggi nominal di bawah nominal; baris metode ~1.4x -> ambang 1.2x)
        mw = [w for w in words if w is not a and id(w) not in header_words and w.left >= 0.55 * W
              and a.cy + 1.2 * a.height < w.cy <= band_bot]
        method = _detect_method(mw, wallet or "GoPay")
        if not method:
            # Teks metode hilang (umum pada foto terkompres). Tebakan dari format nominal: di layar contoh,
            # nominal Saldo memakai "Rp" sedangkan Coins tanpa "Rp". Selalu ditandai agar dicek.
            method = f"{wallet or 'GoPay'} {'Saldo' if had_rp else 'Coins'}"
            flags.append("metode ditebak dari format nominal" + (" (bisa Saldo/Later)" if had_rp else ""))
        method_left = min((w.left for w in mw), default=None)
        for w in mw:
            used.add(id(w))

        # judul: kata di kiri nominal, di luar kolom ikon
        right_limit = min(a.left, method_left if method_left is not None else a.left)
        tw = [w for w in words if id(w) not in used and id(w) not in header_words and w is not a
              and band_top < w.cy <= band_bot and w.cx > 0.20 * W and w.right <= right_limit + 0.02 * W
              and w.conf >= 10 and re.search(r"\w", w.text)]
        tw.sort(key=lambda w: (line_idx.get(id(w), 0), w.left))
        title = _clean_title(" ".join(w.text for w in tw))
        tconf = statistics.fmean(w.conf for w in tw) if tw else 0.0

        if not title or tconf < config.TITLE_LOW_CONF:
            # pita horizontal tempat judul berada: dari sedikit di atas nominal sampai tepat di atas baris metode
            txt, c = page.reocr(0.20 * W, a.cy - 0.4 * a.height, right_limit - 0.01 * W, a.cy + 1.35 * a.height)
            txt = _clean_title(txt)
            if txt and re.search(r"[A-Za-z0-9]", txt) and (not title or c >= tconf + 10):
                title, tconf = txt, c
        if not title:
            title = "(tidak terbaca)"
            flags.append("nama tidak terbaca")
        elif tconf < config.TITLE_LOW_CONF:
            flags.append("nama kurang jelas")

        if a.conf < config.AMOUNT_LOW_CONF:
            flags.append(f"nominal kurang yakin ({a.conf:.0f}%)")

        # arah debit/kredit
        green = _green_score(page, a)
        color_credit = None if green is None else (True if green >= GREEN_MIN else (False if green <= NEUTRAL_MAX else None))
        if wallet == "OVO" or wallet is None:
            if sign != 0:
                direction = sign
            else:
                guess_credit = color_credit if color_credit is not None else any(k in title.lower() for k in CREDIT_HINTS)
                direction = 1 if guess_credit else -1
                flags.append("arah debit/kredit ditebak" + (" (OVO belum divalidasi)" if wallet == "OVO" else ""))
        else:   # konvensi GoPay: minus = debit, tanpa minus = kredit
            direction = -1 if sign < 0 else 1
            if color_credit is not None and (direction > 0) != color_credit:
                flags.append("tanda +/- tidak cocok dengan warna nominal")
        amount = direction * value

        # tanggal: pil/header terdekat di atas baris ini
        above = [h for h in date_headers if h[0] < a.cy]
        if above:
            _, dt, wd_ok = above[-1]
            if any(above[-1][0] < y < a.cy for y in suspect_headers):
                flags.append("tanggal meragukan (header tanggal di atas baris ini tidak terbaca utuh)")
            if wd_ok is False:
                flags.append("hari tidak cocok dengan tanggal")
            mh = [m for m in month_headers if m[0] < a.cy]
            if mh and (mh[-1][1], mh[-1][2]) != (dt.month, dt.year):
                flags.append("bulan tidak cocok dengan header")
            iso = dt.isoformat()
        elif carry_date:
            iso = carry_date
            flags.append("tanggal dari screenshot sebelumnya")
        else:
            iso = None
            flags.append("tanggal tidak ditemukan")

        rows.append(Txn(iso, title, amount, method, wallet or "?", flags))

    last = next((r.date for r in reversed(rows) if r.date), carry_date)
    return ParseResult(wallet, rows, notes, last, page.pass_name, detected)


# ---------------------------------------------------------------- orkestrasi beberapa pass OCR
def _clean(r: Txn) -> bool:
    return not r.flags


def _same_amount_index(rows: list[Txn], row: Txn) -> tuple[int, int]:
    same = [x for x in rows if x.amount == row.amount]
    return same.index(row), len(same)


def merge_results(results: list[ParseResult]) -> ParseResult:
    """Ambil pass dengan baris terbanyak/terbersih sebagai dasar, lalu isi tanggal/metode yang hilang
    dari pass lain. Baris dicocokkan lewat NOMINAL (bagian yang paling andal dibaca), berurutan bila ada
    nominal kembar dan jumlahnya sama di kedua pass."""
    base = max(results, key=lambda r: (len(r.rows), sum(_clean(x) for x in r.rows)))
    for row in base.rows:
        need_date = row.date is None or any(f.startswith(("tanggal", "hari", "bulan")) for f in row.flags)
        need_method = any("metode" in f for f in row.flags)
        if not (need_date or need_method):
            continue
        k, n = _same_amount_index(base.rows, row)
        for other in results:
            if other is base:
                continue
            same = [x for x in other.rows if x.amount == row.amount]
            if len(same) != n:
                continue
            o = same[k]
            if need_date and o.date and not any(f.startswith(("tanggal", "hari", "bulan")) for f in o.flags):
                row.date = o.date
                row.flags = [f for f in row.flags if not f.startswith(("tanggal", "hari", "bulan"))]
                need_date = False
            if need_method and not any("metode" in f for f in o.flags):
                row.method = o.method
                row.flags = [f for f in row.flags if "metode" not in f]
                need_method = False
            if not (need_date or need_method):
                break
    return base


def parse_image(provider: OcrProvider, data: bytes, wallet_hint: str | None = None,
                carry_date: str | None = None) -> ParseResult:
    """Coba pass OCR berurutan sampai semua baris bersih; hasil antar-pass digabung."""
    results: list[ParseResult] = []
    merged: ParseResult | None = None
    for page in provider.passes(data):
        results.append(parse_page(page, wallet_hint, carry_date))
        if not any(r.rows for r in results):
            continue
        merged = merge_results(results)
        if merged.rows and all(_clean(r) for r in merged.rows):
            break
    if merged is None:
        merged = results[0]
    merged.last_date = next((r.date for r in reversed(merged.rows) if r.date), carry_date)
    return merged
