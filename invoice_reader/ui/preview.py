"""Read-only document viewer for invoice PDFs and images."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget

from invoice_reader.ui.theme import Glyph, glyph_icon
from invoice_reader.ui.widgets import open_path

log = logging.getLogger(__name__)
MAX_PAGES = 25


def _pil_to_qimage(img: Image.Image) -> QImage:
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    return QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888).copy()


def load_pages(path: str | Path) -> list[QImage]:
    path = Path(path)
    pages: list[QImage] = []
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        try:
            for i in range(min(len(doc), MAX_PAGES)):
                pages.append(_pil_to_qimage(doc[i].render(scale=2).to_pil()))
        finally:
            doc.close()
    else:
        with Image.open(path) as img:
            for i in range(min(getattr(img, "n_frames", 1), MAX_PAGES)):
                img.seek(i)
                pages.append(_pil_to_qimage(img.copy()))
    return pages


class DocumentPreview(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pages: list[QImage] = []
        self._zoom = 1.0
        self._fit = True
        self._path: str | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("Muted")
        bar.addWidget(self.title, 1)
        for glyph, tip, slot in ((Glyph.ZOOM_OUT, "Zoom out", lambda: self._set_zoom(self._zoom / 1.25)),
                                 (Glyph.ZOOM_IN, "Zoom in", lambda: self._set_zoom(self._zoom * 1.25)),
                                 (Glyph.FIT, "Fit width", self._fit_width),
                                 (Glyph.OPEN_FILE, "Open in default app", self._open_external)):
            b = QToolButton()
            b.setIcon(glyph_icon(glyph, size=16))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            bar.addWidget(b)
        lay.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignHCenter)
        self.container = QWidget()
        self.pages_layout = QVBoxLayout(self.container)
        self.pages_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.pages_layout.setSpacing(12)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll, 1)

    def load(self, path: str | None) -> None:
        self._path = path
        self._pages = []
        if path and Path(path).exists():
            try:
                self._pages = load_pages(path)
                self.title.setText(Path(path).name)
            except Exception as exc:  # noqa: BLE001 - preview is best effort
                log.warning("Preview failed for %s: %s", path, exc)
                self.title.setText(f"Preview unavailable: {exc}")
        else:
            self.title.setText("Original file not found")
        self._fit = True
        self._render()

    def _open_external(self) -> None:
        if self._path and Path(self._path).exists():
            open_path(self._path)

    def _fit_width(self) -> None:
        self._fit = True
        self._render()

    def _set_zoom(self, zoom: float) -> None:
        self._fit = False
        self._zoom = max(0.1, min(zoom, 4.0))
        self._render()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self._fit:
            self._render()

    def _render(self) -> None:
        while self.pages_layout.count():
            w = self.pages_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self._pages:
            return
        avail = max(200, self.scroll.viewport().width() - 30)
        if self._fit:
            self._zoom = avail / self._pages[0].width()
        for img in self._pages:
            label = QLabel()
            pix = QPixmap.fromImage(img).scaledToWidth(max(50, int(img.width() * self._zoom)),
                                                       Qt.SmoothTransformation)
            label.setPixmap(pix)
            label.setStyleSheet("border: 1px solid rgba(128,128,128,0.35); background: white;")
            self.pages_layout.addWidget(label)
