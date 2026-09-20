"""Konfigurasi dari environment variable (diatur di Railway -> Variables)."""
import os
from pathlib import Path


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Bot ini memproses data keuangan -> WAJIB dibatasi ke ID Telegram tertentu.
# Kirim /id ke bot untuk mengetahui ID kamu. Pisahkan dengan koma bila lebih dari satu.
ALLOWED_USER_IDS = _ids(os.environ.get("ALLOWED_USER_IDS", ""))

OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "tesseract").strip().lower()
TESSERACT_LANG = os.environ.get("TESSERACT_LANG", "ind+eng")

# Ambang keyakinan OCR (0-100). Di bawah ini baris diberi tanda untuk dicek manual.
TITLE_LOW_CONF = int(os.environ.get("TITLE_LOW_CONF", "80"))
AMOUNT_LOW_CONF = int(os.environ.get("AMOUNT_LOW_CONF", "75"))

# Di Railway, pasang Volume lalu variabel RAILWAY_VOLUME_MOUNT_PATH terisi otomatis.
DATA_DIR = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "./data")

# Dompet yang sudah divalidasi dengan contoh layar nyata. OVO belum (menunggu contoh screenshot).
SUPPORTED_WALLETS = {"GoPay"}
