"""OCR Tesseract untuk screenshot / foto layar riwayat transaksi.

Temuan dari uji pada foto layar GoPay yang nyata:
  * OCR langsung pada foto utuh gagal (tanggal & sebagian nominal hilang).
  * Memotong area layar HP dulu menaikkan hasil dari ~10 menjadi ~14 dari 16 token kunci.
  * Nama merchant berlatar ikon bisa salah baca dengan keyakinan rendah; OCR ulang pada
    potongan kecil (psm 7, diperbesar 2x) memulihkannya. Itu dilakukan di parser.
"""
from __future__ import annotations

import os
from typing import Iterator

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from .base import OcrPage, OcrProvider, Word

os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # disarankan Tesseract; hemat CPU di container kecil

MAX_WIDTH = 1800   # batasi supaya OCR tidak lambat di server kecil
MIN_WIDTH = 1300   # screenshot kecil (mis. foto terkompres Telegram) diperbesar


class TesseractProvider(OcrProvider):
    name = "tesseract"

    def __init__(self, lang: str = "ind+eng"):
        self.lang = lang

    # ---------- util gambar ----------
    @staticmethod
    def _decode(data: bytes) -> np.ndarray:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)   # patuh EXIF orientation
        if img is None:
            raise ValueError("File bukan gambar yang valid")
        return img

    @staticmethod
    def _crop_screen(img: np.ndarray) -> np.ndarray:
        """Jika ini foto HP (layar terang di tengah latar lain), potong hanya area layarnya."""
        h, w = img.shape[:2]
        scale = 4
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (w // scale, h // scale))
        blur = cv2.GaussianBlur(small, (9, 9), 0)
        _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return img
        c = max(cnts, key=cv2.contourArea)
        x, y, cw, ch = (v * scale for v in cv2.boundingRect(c))
        frac = (cw * ch) / (w * h)
        aspect = ch / max(cw, 1)
        # layar HP: tinggi/lebar ~1.6-2.4 dan tidak memenuhi seluruh frame
        if 0.12 <= frac <= 0.85 and 1.4 <= aspect <= 2.6:
            m = int(0.01 * cw)   # buang sedikit tepi (bezel)
            return img[y + m : y + ch - m, x + m : x + cw - m]
        return img

    @staticmethod
    def _fit_width(img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        if w > MAX_WIDTH:
            f = MAX_WIDTH / w
            return cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        if w < MIN_WIDTH:
            f = MIN_WIDTH / w
            return cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        return img

    # ---------- OCR ----------
    def _data(self, im: np.ndarray, psm: int) -> list[Word]:
        d = pytesseract.image_to_data(im, lang=self.lang, config=f"--psm {psm}", output_type=Output.DICT)
        words = []
        for i, t in enumerate(d["text"]):
            t = t.strip()
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                continue
            if t and conf >= 0:
                words.append(Word(t, d["left"][i], d["top"][i], d["width"][i], d["height"][i], conf))
        return words

    def _make_reocr(self, gray: np.ndarray):
        h, w = gray.shape[:2]

        def reocr(x0: int, y0: int, x1: int, y1: int) -> tuple[str, float]:
            x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))
            if x1 - x0 < 20 or y1 - y0 < 10:
                return "", 0.0
            crop = gray[y0:y1, x0:x1]
            crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            best = ("", 0.0)
            for psm in (7, 6):
                words = self._data(crop, psm)
                if not words:
                    continue
                conf = sum(x.conf for x in words) / len(words)
                if conf > best[1]:
                    best = (" ".join(x.text for x in sorted(words, key=lambda x: (round(x.cy / 20), x.left))), conf)
            return best

        return reocr

    def passes(self, image_bytes: bytes) -> Iterator[OcrPage]:
        img = self._fit_width(self._crop_screen(self._decode(image_bytes)))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dark = gray.mean() < 110
        if dark:                       # mode gelap: balik agar teks gelap di latar terang
            gray = 255 - gray
        norm = cv2.divide(gray, cv2.GaussianBlur(gray, (0, 0), 41), scale=255)   # ratakan cahaya/glare
        h, w = gray.shape[:2]
        reocr = self._make_reocr(gray)
        # urutan berdasarkan uji: gray+psm6 terbaik, lalu dua alternatif sebagai cadangan
        for name, im, psm in (("gray-psm6", gray, 6), ("norm-psm11", norm, 11), ("gray-psm4", gray, 4)):
            yield OcrPage(self._data(im, psm), w, h, img, not dark, name, reocr)
