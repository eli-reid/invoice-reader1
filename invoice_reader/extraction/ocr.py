"""OCR backends. Windows' built-in OCR engine is used by default (no extra
install needed); RapidOCR is used if installed and Windows OCR is unavailable."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from statistics import median

from PIL import Image, ImageOps

log = logging.getLogger(__name__)


class OcrUnavailableError(RuntimeError):
    pass


@dataclass
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float


def _prepare(img: Image.Image, max_dim: int = 9000) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    longest = max(img.size)
    if img.width < 1600:
        scale = 2000 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    elif longest > max_dim:
        scale = max_dim / longest
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def _windows_ocr_words(img: Image.Image) -> list[Word]:
    try:
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except ImportError as exc:
        raise OcrUnavailableError("Windows OCR runtime packages are not installed") from exc

    async def run() -> list[Word]:
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailableError("No Windows OCR language pack is installed")
        rgba = img.convert("RGBA")
        writer = DataWriter()
        writer.write_bytes(bytearray(rgba.tobytes()))
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width, rgba.height, BitmapAlphaMode.PREMULTIPLIED)
        result = await engine.recognize_async(bitmap)
        words = []
        for line in result.lines:
            for w in line.words:
                r = w.bounding_rect
                words.append(Word(w.text, r.x, r.y, r.width, r.height))
        return words

    return asyncio.run(run())


def _rapid_ocr_words(img: Image.Image) -> list[Word]:
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as exc:
        raise OcrUnavailableError("RapidOCR is not installed") from exc
    engine = RapidOCR()
    result, _ = engine(np.array(img.convert("RGB")))
    words = []
    for box, text, _score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        words.append(Word(text, min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    return words


def group_words_into_lines(words: list[Word]) -> list[str]:
    """Rebuild table rows: OCR engines often split columns into separate lines,
    so words are regrouped by vertical position and sorted left to right."""
    if not words:
        return []
    tolerance = median(w.h for w in words) * 0.55
    rows: list[list[Word]] = []
    for word in sorted(words, key=lambda w: w.y + w.h / 2):
        cy = word.y + word.h / 2
        if rows:
            row = rows[-1]
            row_cy = sum(w.y + w.h / 2 for w in row) / len(row)
            if abs(cy - row_cy) <= tolerance:
                row.append(word)
                continue
        rows.append([word])
    return [" ".join(w.text for w in sorted(row, key=lambda w: w.x)) for row in rows]


def available_engines() -> list[str]:
    engines = []
    try:
        from winrt.windows.media.ocr import OcrEngine
        if OcrEngine.try_create_from_user_profile_languages() is not None:
            engines.append("Windows OCR")
    except Exception:  # noqa: BLE001 - any failure means the engine is unusable
        pass
    try:
        import rapidocr_onnxruntime  # type: ignore  # noqa: F401
        engines.append("RapidOCR")
    except ImportError:
        pass
    return engines


def ocr_image(img: Image.Image) -> str:
    prepared = _prepare(img)
    errors = []
    for backend in (_windows_ocr_words, _rapid_ocr_words):
        try:
            return "\n".join(group_words_into_lines(backend(prepared)))
        except OcrUnavailableError as exc:
            errors.append(str(exc))
            log.debug("OCR backend unavailable: %s", exc)
    raise OcrUnavailableError("; ".join(errors))
