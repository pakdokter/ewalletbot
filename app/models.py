from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Txn:
    date: str | None          # ISO yyyy-mm-dd; None = belum diketahui
    desc: str                 # nama item / merchant seperti terbaca di layar
    amount: int               # rupiah bertanda: debit < 0, kredit > 0
    method: str               # mis. "GoPay Saldo", "GoPay Coins", "?" bila tak terbaca
    wallet: str               # "GoPay" | "OVO"
    flags: list[str] = field(default_factory=list)   # alasan perlu dicek manual
    seq: int = 0              # urutan kemunculan di layar (atas -> bawah, lintas screenshot)

    def key(self) -> tuple:
        """Kunci untuk mendeteksi baris yang sama pada screenshot yang tumpang tindih."""
        return (self.date, self.desc.strip().lower(), self.amount, self.method.strip().lower())


@dataclass
class ParseResult:
    wallet: str | None
    rows: list[Txn]
    notes: list[str]
    last_date: str | None
    pass_name: str = ""
    detected: str | None = None       # dompet yang terdeteksi dari teks layar (terlepas dari hint pengguna)
