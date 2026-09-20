from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def cx(self) -> float:
        return self.left + self.width / 2

    @property
    def cy(self) -> float:
        return self.top + self.height / 2


@dataclass
class OcrPage:
    words: list[Word]
    width: int
    height: int
    image_bgr: np.ndarray | None      # untuk cek warna (hijau = kredit)
    color_reliable: bool              # False bila UI gelap (gambar dibalik)
    pass_name: str
    _reocr: Callable[[int, int, int, int], tuple[str, float]] | None = field(default=None, repr=False)

    def reocr(self, x0: int, y0: int, x1: int, y1: int) -> tuple[str, float]:
        """OCR ulang satu area kecil (dipakai untuk nama merchant yang keyakinannya rendah)."""
        return self._reocr(x0, y0, x1, y1) if self._reocr else ("", 0.0)


class OcrProvider(ABC):
    name: str

    @abstractmethod
    def passes(self, image_bytes: bytes) -> Iterator[OcrPage]:
        """Hasilkan hasil OCR satu per satu (lazy). Parser berhenti begitu hasilnya cukup baik."""
