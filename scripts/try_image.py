"""Uji OCR+parser pada satu gambar lokal, tanpa Telegram.
   python scripts/try_image.py foto.jpg [gopay|ovo]
Berguna untuk menyetel parser OVO: jalankan pada screenshot OVO dan lihat baris yang salah."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config
from app.ocr import get_provider
from app.parser import parse_image

path = Path(sys.argv[1])
hint = {"gopay": "GoPay", "ovo": "OVO"}.get(sys.argv[2].lower()) if len(sys.argv) > 2 else None
res = parse_image(get_provider(config.OCR_PROVIDER, lang=config.TESSERACT_LANG), path.read_bytes(), hint)
print(f"dompet={res.wallet}  pass={res.pass_name}  baris={len(res.rows)}")
for i, r in enumerate(res.rows, 1):
    print(f"{i:>2}. {r.date}  {r.desc[:28]:28s} {r.amount:>12,}  {r.method:12s} {'⚠ ' + '; '.join(r.flags) if r.flags else ''}")
for n in res.notes:
    print("catatan:", n)
