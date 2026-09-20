"""Regresi: semua pesan HTML yang dikirim bot harus valid menurut Telegram.

Bug nyata di produksi: '/saldoawal <nominal>' dalam pesan HTML membuat Telegram menolak
("unsupported start tag 'nominal'") dan hasil OCR tidak pernah tampil.
"""
import asyncio
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.bot as B
from app.models import ParseResult, Txn
from app.session import Store

ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}


class _Strict(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.bad.append(tag)


def assert_telegram_html(text: str):
    p = _Strict()
    p.feed(text)
    if p.bad:
        raise BadRequest(f"Can't parse entities: unsupported start tag \"{p.bad[0]}\"")


def make_msg(sink, strict=True):
    msg = MagicMock()
    msg.chat_id = 7
    status = MagicMock()
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()

    async def reply_text(text, parse_mode=None, **kw):
        if strict and parse_mode is not None:
            assert_telegram_html(text)
        sink.append(text)
        return status

    msg.reply_text = reply_text
    return msg


def fake_result(rows, wallet="GoPay"):
    return ParseResult(wallet, rows, ["catatan <uji>"], "2024-01-01", "test", wallet)


def rows():
    return [Txn("2024-01-01", "lapak <gaming> & co", -114300, "GoPay Saldo", "GoPay", ["nama kurang jelas <cek>"]),
            Txn("2024-01-01", "Cashback", 2000, "GoPay Coins", "GoPay")]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    B.store = Store(Path(tempfile.mkdtemp()))
    monkeypatch.setattr(B, "provider", object())
    monkeypatch.setattr(B, "parse_image", lambda *a, **k: fake_result(rows()))


def test_image_reply_is_valid_html_without_saldo_awal():
    sink = []
    asyncio.run(B.process_image(make_msg(sink), b"x", compressed=True))
    assert any("saldoawal" in t for t in sink)          # petunjuk saldo awal tampil (tag tidak dianggap HTML)
    assert any("&lt;nominal&gt;" in t for t in sink)


def test_help_text_is_valid_html():
    assert_telegram_html(B.HELP)


def test_fallback_to_plain_text_when_telegram_rejects_markup():
    sink, calls = [], []
    msg = MagicMock()

    async def reply_text(text, parse_mode=None, **kw):
        calls.append(parse_mode)
        if parse_mode is not None:
            raise BadRequest("Can't parse entities")
        sink.append(text)

    msg.reply_text = reply_text
    asyncio.run(B.send_html(msg, "<b>Halo</b> <pre>a &lt;b&gt;</pre>"))
    assert calls == [B.ParseMode.HTML, None] and sink == ["Halo a <b>"]
