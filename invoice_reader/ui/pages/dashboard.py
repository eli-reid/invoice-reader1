from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from invoice_reader.ui.theme import Glyph, glyph_icon
from invoice_reader.ui.widgets import (
    Card, DataTable, DropZone, PageHeader, StatCard, fmt_pct, fmt_price, make_item, pct_change,
)

UP = QColor("#c42b1c")
DOWN = QColor("#107c10")


class DashboardPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(14)

        header = PageHeader("Dashboard", "Electrical product pricing at a glance")
        header.add_action("Scan folder now", Glyph.REFRESH).clicked.connect(win.scan_now)
        header.add_action("Import invoices", Glyph.ADD, primary=True).clicked.connect(win.browse_import)
        lay.addWidget(header)

        watch = Card()
        row = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(glyph_icon(Glyph.FOLDER, size=24).pixmap(24, 24))
        row.addWidget(icon)
        text = QVBoxLayout()
        t = QLabel("Watch folder")
        t.setObjectName("SectionTitle")
        self.watch_status = QLabel()
        self.watch_status.setObjectName("Muted")
        self.watch_status.setWordWrap(True)
        text.addWidget(t)
        text.addWidget(self.watch_status)
        row.addLayout(text, 1)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(win.open_watch_folder)
        change = QPushButton("Change...")
        change.clicked.connect(win.choose_watch_folder)
        row.addWidget(open_btn)
        row.addWidget(change)
        watch.layout_.addLayout(row)
        lay.addWidget(watch)

        stats = QGridLayout()
        stats.setSpacing(12)
        self.cards = {
            "products": StatCard("Products tracked", Glyph.TAG),
            "invoices": StatCard("Invoices imported", Glyph.DOCUMENT),
            "vendors": StatCard("Vendors", Glyph.HOME),
            "review": StatCard("Waiting for review", Glyph.INBOX),
        }
        for i, card in enumerate(self.cards.values()):
            stats.addWidget(card, 0, i)
        self.cards["review"].mousePressEvent = lambda _e: win.navigate("inbox")
        lay.addLayout(stats)

        drop = DropZone()
        drop.files_dropped.connect(win.import_files)
        drop.browse_clicked.connect(win.browse_import)
        lay.addWidget(drop)

        title = QLabel("Recent price changes")
        title.setObjectName("SectionTitle")
        lay.addWidget(title)
        self.changes = DataTable(["Item #", "Name", "Previous", "Latest", "Change", "Vendor", "Date"])
        self.changes.activated_id.connect(lambda pid: pid and win.show_product(pid))
        lay.addWidget(self.changes, 1)

    def set_watch_status(self, text: str) -> None:
        self.watch_status.setText(text)

    def refresh(self) -> None:
        for key, value in self.win.db.stats().items():
            if key in self.cards:
                self.cards[key].set_value(value)
        rows = []
        for r in self.win.db.recent_price_changes(100):
            change = pct_change(r["last_cost"], r["prev_cost"])
            change_item = make_item(fmt_pct(change), change or 0, align_right=True)
            change_item.setForeground(UP if (change or 0) > 0 else DOWN)
            per = r["last_per"] or 1
            rows.append([
                make_item(r["item_number"], item_id=r["id"]),
                make_item(r["name"]),
                make_item(fmt_price(r["prev_cost"] * per, per), r["prev_cost"], align_right=True),
                make_item(fmt_price(r["last_price"], per), r["last_cost"], align_right=True),
                change_item,
                make_item(r["last_vendor"] or ""),
                make_item(r["last_date"] or "", r["last_date"] or ""),
            ])
        self.changes.set_rows(rows)
        self.changes.fit_columns({1: 360})
