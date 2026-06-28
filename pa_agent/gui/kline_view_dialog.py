"""Lightweight K-line chart viewer for batch scan results."""
from __future__ import annotations

from typing import Any

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.gui.widgets.candle_item import CandleItem

_COLOR_UP = QColor(0, 208, 132)
_COLOR_DOWN = QColor(255, 71, 87)

_LATEST_N = 30  # number of candles to show in the chart


class KlineViewDialog(QDialog):
    """Simple K-line chart dialog for a single stock."""

    def __init__(
        self,
        code: str,
        name: str,
        bars: list[Any],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._code = code
        self._name = name
        self._bars = bars

        self.setWindowTitle(f"{name} ({code}) — K 线图表")
        self.resize(900, 560)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Title
        title = QLabel(f"{self._name} ({self._code}) — 最近{min(len(self._bars), _LATEST_N)}根K线")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #e6edf3;")
        layout.addWidget(title)

        # Candlestick chart
        pg.setConfigOptions(antialias=True)
        plot = pg.PlotWidget()
        plot.setBackground("#0d1117")
        plot.showGrid(x=True, y=True, alpha=0.15)
        plot.setLabel("left", "价格")
        plot.getAxis("left").setTextPen(QColor("#8b949e"))
        plot.getAxis("bottom").setTextPen(QColor("#8b949e"))

        # Only show latest N candles
        bars_to_show = self._bars[:min(len(self._bars), _LATEST_N)]
        bars_to_show = list(reversed(bars_to_show))  # oldest-first for pyqtgraph

        candle_items: list[pg.GraphicsObject] = []
        for i, bar in enumerate(bars_to_show):
            ci = CandleItem(bar, i, forming=False)
            candle_items.append(ci)
            plot.addItem(ci)

        # Auto-range
        prices = [b.close for b in bars_to_show if b.close] + \
                 [b.high for b in bars_to_show if b.high] + \
                 [b.low for b in bars_to_show if b.low]
        if prices:
            pad = (max(prices) - min(prices)) * 0.08 or 1
            plot.setYRange(min(prices) - pad, max(prices) + pad)
        plot.setXRange(-0.5, len(bars_to_show) - 0.5)

        plot.getAxis("bottom").setTicks(
            [(i, "") for i in range(len(bars_to_show))]
        )

        layout.addWidget(plot, stretch=1)

        # Info panel below chart
        info_layout = QHBoxLayout()
        last = bars_to_show[-1] if bars_to_show else None
        if last:
            pct = last.pct_chg
            color = _COLOR_UP if pct is not None and pct >= 0 else _COLOR_DOWN
            chg_str = f"{pct:+.2f}%" if pct is not None else "—"
            info_layout.addWidget(QLabel(
                f"最新收盘: {last.close:.2f}  "
                f"涨跌: <span style='color:{color.name()}'>{chg_str}</span>  "
                f"高: {last.high:.2f}  低: {last.low:.2f}  开: {last.open:.2f}  "
                f"量: {int(last.volume):,}"
            ))
        info_layout.addStretch()
        layout.addLayout(info_layout)

        # Recent OHLCV table
        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["日期", "开盘", "收盘", "最高", "最低", "涨跌幅"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        # Show last 10 bars (newest first)
        recent = self._bars[:min(len(self._bars), 10)]
        table.setRowCount(len(recent))
        for i, bar in enumerate(recent):
            from datetime import datetime
            dt = datetime.fromtimestamp(bar.ts_open / 1000)
            date_str = dt.strftime("%m-%d")
            pct_str = f"{bar.pct_chg:+.2f}%" if bar.pct_chg is not None else "—"

            table.setItem(i, 0, QTableWidgetItem(date_str))
            table.setItem(i, 1, QTableWidgetItem(f"{bar.open:.2f}"))
            table.setItem(i, 2, QTableWidgetItem(f"{bar.close:.2f}"))
            table.setItem(i, 3, QTableWidgetItem(f"{bar.high:.2f}"))
            table.setItem(i, 4, QTableWidgetItem(f"{bar.low:.2f}"))

            pct_item = QTableWidgetItem(pct_str)
            if bar.pct_chg is not None:
                pct_item.setForeground(_COLOR_UP if bar.pct_chg >= 0 else _COLOR_DOWN)
            table.setItem(i, 5, pct_item)

            # Align center
            for col in range(6):
                table.item(i, col).setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        table.setMaximumHeight(180)
        layout.addWidget(table)
