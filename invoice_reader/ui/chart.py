from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, QMargins, Qt
from PySide6.QtGui import QPainter

from invoice_reader.models import PRICE_PER_LABEL
from invoice_reader.ui.theme import is_dark


class PriceChart(QChartView):
    """Price history with one series per vendor, shown on the latest pricing basis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(220)
        self.set_history([])

    def set_history(self, rows) -> None:
        chart = QChart()
        chart.setTheme(QChart.ChartThemeDark if is_dark() else QChart.ChartThemeLight)
        chart.setBackgroundVisible(False)
        chart.setMargins(QMargins(4, 4, 4, 4))
        chart.legend().setAlignment(Qt.AlignBottom)
        if not rows:
            chart.setTitle("No price history yet")
            self._swap(chart)
            return

        basis = rows[-1]["price_per"] or 1
        by_vendor: dict[str, list] = defaultdict(list)
        for r in rows:
            by_vendor[r["vendor"] or "Unknown vendor"].append(r)

        x_axis = QDateTimeAxis()
        x_axis.setFormat("MMM d, yyyy")
        x_axis.setTickCount(min(6, max(2, len(rows))))
        y_axis = QValueAxis()
        y_axis.setLabelFormat("$%.2f")
        y_axis.setTitleText(f"Price - {PRICE_PER_LABEL.get(basis, f'per {basis}').lower()}")
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)

        values, stamps = [], []
        for vendor, vrows in by_vendor.items():
            series = QLineSeries()
            series.setName(vendor)
            series.setPointsVisible(True)
            for r in vrows:
                ts = datetime.fromisoformat(r["price_date"]).timestamp() * 1000
                value = r["unit_cost"] * basis
                series.append(ts, value)
                values.append(value)
                stamps.append(ts)
            chart.addSeries(series)
            series.attachAxis(x_axis)
            series.attachAxis(y_axis)

        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.15 or max(hi * 0.1, 0.01)
        y_axis.setRange(max(0, lo - pad), hi + pad)
        day = 86_400_000
        t0, t1 = min(stamps), max(stamps)
        if t1 - t0 < day * 2:
            t0, t1 = t0 - day * 15, t1 + day * 15
        x_axis.setRange(QDateTime.fromMSecsSinceEpoch(int(t0)), QDateTime.fromMSecsSinceEpoch(int(t1)))
        chart.legend().setVisible(len(by_vendor) > 1)
        self._swap(chart)

    def _swap(self, chart: QChart) -> None:
        old = self.chart()
        self.setChart(chart)
        if old is not None and old is not chart:
            old.deleteLater()
