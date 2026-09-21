"""Bot tidak boleh diam: setiap bentuk pesan harus sampai ke handler yang benar dan dijawab."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from telegram import Bot, Update, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.bot as B
from app.session import Store, parse_rupiah


@pytest.fixture(autouse=True)
def isolated():
    B.store = Store(Path(tempfile.mkdtemp()))
    B.config.ALLOWED_USER_IDS.add(42)


def handlers_group0():
    app = MagicMock()
    added = []
    app.add_handler = lambda h, group=0: added.append((group, h))
    B.register(app)
    return [h for g, h in added if g == 0]


BOT = Bot("123:ABC")
BOT._bot_user = User(999, "Bot", True, username="ojan_bot")


def update(text=None, photo=False):
    msg = {"message_id": 1, "date": 1700000000, "chat": {"id": 7, "type": "private"},
           "from": {"id": 42, "is_bot": False, "first_name": "x"}}
    if text is not None:
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if photo:
        msg["photo"] = [{"file_id": "a", "file_unique_id": "b", "width": 10, "height": 10}]
    return Update.de_json({"update_id": 1, "message": msg}, BOT)


def route(u):
    """Handler pertama yang cocok di grup 0 (seperti dispatcher python-telegram-bot)."""
    for h in handlers_group0():
        if h.check_update(u):
            return h.callback
    return None


@pytest.mark.parametrize("text,expected", [
    ("/saldoawal 0", B.cmd_saldoawal), ("/saldoawal@ojan_bot 0", B.cmd_saldoawal), ("/Saldoawal 0", B.cmd_saldoawal),
    ("0", B.on_text), ("saldoawal 0", B.on_text), ("halo", B.on_text),
    ("/saldo awal 0", B.on_unknown_command), ("/foo", B.on_unknown_command),
    ("/rekap", B.cmd_rekap), ("/tanggal 01/01/2024", B.cmd_tanggal),
])
def test_every_message_shape_has_a_handler(text, expected):
    assert route(update(text)) is expected


def test_photo_goes_to_image_handler():
    assert route(update(photo=True)) is B.on_image


def run_text(text, sink):
    u = MagicMock()
    u.effective_chat.id = 7
    u.effective_user.id = 42
    u.message = MagicMock()
    u.message.text = text

    async def reply(t, **kw):
        sink.append(t)

    u.message.reply_text = reply
    asyncio.run(B.on_text(u, MagicMock()))


def test_bare_zero_sets_saldo_awal_when_unset():
    sink = []
    run_text("0", sink)
    assert sink == ["Saldo awal: Rp 0,00"]
    assert B.store.get(7).saldo_awal == 0.0          # nol adalah nilai sah, bukan "belum diisi"


def test_bare_number_does_not_overwrite_existing_saldo():
    B.store.get(7).saldo_awal = 500.0
    sink = []
    run_text("1000", sink)
    assert B.store.get(7).saldo_awal == 500.0 and "Saldo awal saat ini" in sink[0]


def test_explicit_text_overwrites_and_gibberish_gets_help():
    B.store.get(7).saldo_awal = 500.0
    sink = []
    run_text("saldoawal 1.250.000", sink)
    assert B.store.get(7).saldo_awal == 1250000.0
    sink.clear()
    run_text("halo apa kabar", sink)
    assert sink == [B.UNKNOWN]


@pytest.mark.parametrize("bad", ["nan", "inf", "1e5", "", "abc"])
def test_parse_rupiah_rejects_non_numbers(bad):
    with pytest.raises(ValueError):
        parse_rupiah(bad)
