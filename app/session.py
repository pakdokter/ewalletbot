"""Sesi per chat: daftar transaksi yang sedang dikumpulkan dari screenshot.

Disimpan sebagai JSON di DATA_DIR (Volume Railway) supaya tidak hilang saat redeploy.
Gambar TIDAK disimpan - hanya teks hasil baca.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import Txn


def parse_rupiah(text: str) -> float:
    """'62620.36' -> 62620.36 ; '1.250.000' -> 1250000 ; 'Rp 500.000' -> 500000 ; '1.250,50' -> 1250.5"""
    t = re.sub(r"[Rp\s]", "", text, flags=re.I)
    neg = t.startswith(("-", "(")) or t.endswith(")")
    t = t.strip("-+()")
    if "," in t and "." in t:                       # gaya Indonesia: titik ribuan, koma desimal
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", t) else t.replace(",", "")
    elif "." in t and re.fullmatch(r"\d{1,3}(\.\d{3})+", t):   # 1.250.000 / 500.000 -> ribuan
        t = t.replace(".", "")
    v = float(t)
    return -v if neg else v


def parse_date_input(text: str) -> str:
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Format tanggal: dd/mm/yyyy, mis. 31/01/2024")


def merge_overlap(prev: list[Txn], new: list[Txn]) -> tuple[list[Txn], int]:
    """Buang awal `new` yang sama persis dengan akhir `prev` (screenshot berurutan saling tumpang tindih).

    Yang dibandingkan adalah URUTAN baris (bukan sekadar himpunan), sehingga dua transaksi identik
    yang memang terjadi pada hari yang sama tidak otomatis dianggap duplikat kecuali berada
    tepat di batas antar screenshot. Jumlah yang dibuang dilaporkan ke pengguna.
    """
    for k in range(min(len(prev), len(new)), 0, -1):
        if [t.key() for t in prev[-k:]] == [t.key() for t in new[:k]]:
            return new[k:], k
    return new, 0


@dataclass
class Session:
    chat_id: int
    wallet: str | None = None            # "GoPay" | "OVO" | None (auto-deteksi)
    saldo_awal: float | None = None
    rows: list[Txn] = field(default_factory=list)
    last_date: str | None = None
    next_seq: int = 0

    def add_rows(self, rows: list[Txn]) -> tuple[list[Txn], int]:
        fresh, dropped = merge_overlap(self.rows, rows)
        for t in fresh:
            t.seq = self.next_seq
            self.next_seq += 1
            self.rows.append(t)
        if fresh:
            self.last_date = next((t.date for t in reversed(fresh) if t.date), self.last_date)
        return fresh, dropped

    def chronological(self) -> list[Txn]:
        """Urut tanggal naik. Di layar, item terbaru tampil paling atas -> dalam satu hari, seq besar = lebih lama."""
        return sorted(self.rows, key=lambda t: (t.date or "9999-12-31", -t.seq))


class Store:
    def __init__(self, base: Path):
        self.dir = base / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[int, Session] = {}

    def _path(self, chat_id: int) -> Path:
        return self.dir / f"{chat_id}.json"

    def get(self, chat_id: int) -> Session:
        if chat_id in self._cache:
            return self._cache[chat_id]
        p = self._path(chat_id)
        if p.exists():
            d = json.loads(p.read_text())
            s = Session(chat_id, d.get("wallet"), d.get("saldo_awal"), [Txn(**r) for r in d.get("rows", [])],
                        d.get("last_date"), d.get("next_seq", 0))
        else:
            s = Session(chat_id)
        self._cache[chat_id] = s
        return s

    def save(self, s: Session) -> None:
        d = asdict(s)
        tmp = self._path(s.chat_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        tmp.replace(self._path(s.chat_id))

    def reset(self, chat_id: int) -> None:
        self._cache.pop(chat_id, None)
        self._path(chat_id).unlink(missing_ok=True)
