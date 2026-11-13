from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QMessageBox, QPushButton, QVBoxLayout, QWidget

from invoice_reader.db import INBOX_FAILED, INBOX_PENDING, INBOX_PROCESSING
from invoice_reader.models import ParsedInvoice
from invoice_reader.ui.theme import Glyph
from invoice_reader.ui.widgets import DataTable, PageHeader, make_item, reveal_in_explorer

STATUS_LABELS = {
    "processing": ("\u23f3 Reading...", None),
    "pending": ("\u25cf Needs review", QColor("#0f6cbd")),
    "failed": ("\u2716 Failed", QColor("#c42b1c")),
    "imported": ("\u2713 Imported", QColor("#107c10")),
    "ignored": ("Ignored", None),
    "skipped": ("Not an invoice", None),
}


class InboxPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(12)

        header = PageHeader("Import queue", "New invoices from the watch folder and manual imports land here")
        header.add_action("Import invoices", Glyph.ADD).clicked.connect(win.browse_import)
        self.review_btn = header.add_action("Review", Glyph.EYE, primary=True)
        self.review_btn.clicked.connect(self.review_selected)
        lay.addWidget(header)

        bar = QHBoxLayout()
        self.show_all = QCheckBox("Show history (imported, ignored and skipped files)")
        self.show_all.toggled.connect(self.refresh)
        bar.addWidget(self.show_all)
        bar.addStretch(1)
        for text, slot in (("Re-read", self.reprocess), ("Ignore", self.ignore), ("Show in folder", self.reveal),
                           ("Remove from list", self.remove)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        lay.addLayout(bar)

        self.table = DataTable(["File", "Status", "Vendor", "Invoice #", "Date", "Lines", "Notes", "Detected",
                                "Source"])
        self.table.activated_id.connect(lambda i: i and self.win.review_inbox(i))
        lay.addWidget(self.table, 1)

    def refresh(self) -> None:
        rows = []
        for r in self.win.db.inbox_list(active_only=not self.show_all.isChecked()):
            parsed = ParsedInvoice.from_json(r["parsed_json"]) if r["parsed_json"] else None
            label, color = STATUS_LABELS.get(r["status"], (r["status"], None))
            status = make_item(label, r["status"])
            if color:
                status.setForeground(color)
            notes = r["error"] or ("; ".join(parsed.warnings) if parsed and r["status"] == INBOX_PENDING else "")
            detected = datetime.fromisoformat(r["detected_at"])
            rows.append([
                make_item(Path(r["file_path"]).name, item_id=r["id"]),
                status,
                make_item(parsed.vendor if parsed else ""),
                make_item(parsed.invoice_number if parsed else ""),
                make_item(parsed.invoice_date or "" if parsed else ""),
                make_item(str(len(parsed.lines)) if parsed else "", len(parsed.lines) if parsed else 0,
                          align_right=True),
                make_item(notes),
                make_item(f"{detected:%b %d, %Y %I:%M %p}", r["detected_at"]),
                make_item("Watch folder" if r["source"] == "watch" else "Manual"),
            ])
        self.table.set_rows(rows)
        self.table.fit_columns({0: 260, 6: 380})

    def _selected(self) -> list[int]:
        return [i for i in self.table.selected_ids() if i is not None]

    def review_selected(self) -> None:
        ids = self._selected()
        if not ids:
            first = next((r["id"] for r in self.win.db.inbox_list() if r["status"] == INBOX_PENDING), None)
            if first is None:
                QMessageBox.information(self, "Nothing to review", "There are no invoices waiting for review.")
                return
            ids = [first]
        for inbox_id in ids:
            if not self.win.review_inbox(inbox_id):
                break

    def reprocess(self) -> None:
        for i in self._selected():
            self.win.importer.reprocess(i)

    def ignore(self) -> None:
        for i in self._selected():
            row = self.win.db.inbox_get(i)
            if row and row["status"] in (INBOX_PENDING, INBOX_FAILED, INBOX_PROCESSING):
                self.win.importer.ignore(i)

    def reveal(self) -> None:
        ids = self._selected()
        if ids and (row := self.win.db.inbox_get(ids[0])) and Path(row["file_path"]).exists():
            reveal_in_explorer(row["file_path"])

    def remove(self) -> None:
        ids = self._selected()
        if not ids:
            return
        if QMessageBox.question(
                self, "Remove from list",
                f"Remove {len(ids)} entr{'y' if len(ids) == 1 else 'ies'} from the queue?\n\n"
                "The files themselves are not deleted.") != QMessageBox.Yes:
            return
        for i in ids:
            self.win.db.inbox_delete(i)
        self.win.importer.inbox_changed.emit()
