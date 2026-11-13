"""File -> text -> ParsedInvoice pipeline. Safe to call from worker threads."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PIL import Image

from invoice_reader.extraction.ocr import ocr_image
from invoice_reader.extraction.parser import finalize_warnings, find_known_vendor, parse_invoice_text
from invoice_reader.models import ParsedInvoice

log = logging.getLogger(__name__)

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS
OCR_DPI = 300
MIN_TEXT_CHARS = 40  # pages with less embedded text than this are treated as scans


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def _pdf_text(path: Path) -> tuple[str, str]:
    import pdfplumber
    import pypdfium2 as pdfium

    pages_text: list[str] = []
    used_text = used_ocr = False
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            if sum(c.isalnum() for c in text) >= MIN_TEXT_CHARS:
                pages_text.append(text)
                used_text = True
            else:
                pages_text.append("")
    scanned = [i for i, t in enumerate(pages_text) if not t]
    if scanned:
        doc = pdfium.PdfDocument(str(path))
        try:
            for i in scanned:
                bitmap = doc[i].render(scale=OCR_DPI / 72)
                pages_text[i] = ocr_image(bitmap.to_pil())
                used_ocr = True
        finally:
            doc.close()
    method = "mixed" if used_text and used_ocr else ("ocr" if used_ocr else "text")
    log.info("Extracted %d page(s) from %s via %s", page_count, path.name, method)
    return "\n".join(pages_text), method


def _image_text(path: Path) -> str:
    texts = []
    with Image.open(path) as img:
        frames = getattr(img, "n_frames", 1)
        for i in range(frames):
            img.seek(i)
            texts.append(ocr_image(img.copy()))
    return "\n".join(texts)


def extract_text(path: str | Path) -> tuple[str, str]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in PDF_EXTENSIONS:
        return _pdf_text(path)
    if suffix in IMAGE_EXTENSIONS:
        return _image_text(path), "ocr"
    raise ValueError(f"Unsupported file type: {suffix}")


def _letterhead_vendor(path: Path, known: list[str], exclude: list[str], aliases: dict[str, str]) -> str:
    if path.suffix.lower() not in PDF_EXTENSIONS:
        return ""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        try:
            img = doc[0].render(scale=OCR_DPI / 72).to_pil()
        finally:
            doc.close()
        top = img.crop((0, 0, img.width, int(img.height * 0.25)))
        return find_known_vendor(ocr_image(top), known, exclude, aliases)
    except Exception as exc:  # noqa: BLE001 - best-effort enhancement only
        log.info("Letterhead OCR skipped for %s: %s", path.name, exc)
        return ""


def process_file(path: str | Path, known_vendors: list[str] | None = None,
                 file_hash: str | None = None, exclude_names: list[str] | None = None,
                 vendor_aliases: dict[str, str] | None = None) -> ParsedInvoice:
    path = Path(path)
    text, method = extract_text(path)
    invoice = parse_invoice_text(text, known_vendors, exclude_names, vendor_aliases)
    if not invoice.vendor_confident and method == "text":
        # Vendor name often exists only in the logo image; read the letterhead with OCR.
        logo_vendor = _letterhead_vendor(path, known_vendors or [], exclude_names or [], vendor_aliases or {})
        if logo_vendor:
            invoice.vendor, invoice.vendor_confident = logo_vendor, True
            finalize_warnings(invoice)
    invoice.source_path = str(path)
    invoice.file_hash = file_hash or file_sha256(path)
    invoice.method = method
    return invoice
