from .base import OcrPage, OcrProvider, Word


def get_provider(name: str, **kwargs) -> OcrProvider:
    """Pilih penyedia OCR dari env OCR_PROVIDER. Saat ini hanya 'tesseract'."""
    if name == "tesseract":
        from .tesseract_provider import TesseractProvider
        return TesseractProvider(**kwargs)
    raise ValueError(f"OCR_PROVIDER '{name}' tidak dikenal. Yang tersedia: tesseract")


__all__ = ["OcrPage", "OcrProvider", "Word", "get_provider"]
