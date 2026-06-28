"""Batch scan dialog for A-share sector/board screening.

Allows the user to select a sector/board type → specific board → sort criterion →
top N stocks, then run AI analysis on all of them and view aggregated statistics.
"""
from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from pa_agent.data.eastmoney_client import (
    BOARD_TYPE_LABELS,
    SORT_CRITERIA_MAP,
    fetch_board_top_stocks,
    fetch_sector_boards,
)

logger = logging.getLogger(__name__)

# Column indices for the results table
_COL_CODE = 0
_COL_NAME = 1
_COL_DIRECTION = 2
_COL_CYCLE = 3
_COL_CONFIDENCE = 4
_COL_ORDER_TYPE = 5
_COL_ORDER_DIR = 6
_COL_TERMINAL = 7
_COL_WIN_RATE = 8
_COL_REASONING = 9
_COL_ERROR = 10
_COL_COUNT = 11

_COL_HEADERS = [
    "代码", "名称", "方向", "周期", "置信度",
    "下单类型", "多/空", "终局", "胜率",
    "分析摘要", "错误信息",
]


class BatchScanDialog(QDialog):
    """Dialog for batch scanning A-share stocks from a board/sector."""

    def __init__(
        self,
        app_context: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = app_context
        self._worker: Any = None
        self._results: list[Any] = []
        self._board_cache: dict[str, list[dict[str, Any]]] = {}

        self.setWindowTitle("A股板块批量扫描")
        self.resize(1100, 720)
        self._setup_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Scan config group ────────────────────────────────────────────────
        cfg_group = QGroupBox("扫描参数")
        cfg_layout = QHBoxLayout(cfg_group)
        cfg_layout.setSpacing(12)

        # Board type
        cfg_layout.addWidget(QLabel("板块类型:"))
        self._board_type_combo = QComboBox()
        for key, label in BOARD_TYPE_LABELS.items():
            self._board_type_combo.addItem(label, key)
        self._board_type_combo.currentIndexChanged.connect(self._on_board_type_changed)
        self._board_type_combo.setMinimumWidth(90)
        cfg_layout.addWidget(self._board_type_combo)

        # Board selector (populated on type change)
        cfg_layout.addWidget(QLabel("具体板块:"))
        self._board_combo = QComboBox()
        self._board_combo.setMinimumWidth(160)
        self._board_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        cfg_layout.addWidget(self._board_combo)

        # Sort criterion
        cfg_layout.addWidget(QLabel("排序依据:"))
        self._sort_combo = QComboBox()
        for key, (_field, _desc, label) in SORT_CRITERIA_MAP.items():
            self._sort_combo.addItem(label, key)
        self._sort_combo.setMinimumWidth(80)
        cfg_layout.addWidget(self._sort_combo)

        # Top N
        cfg_layout.addWidget(QLabel("前N只:"))
        self._top_n_spin = QSpinBox()
        self._top_n_spin.setRange(1, 200)
        self._top_n_spin.setValue(10)
        self._top_n_spin.setMinimumWidth(60)
        cfg_layout.addWidget(self._top_n_spin)

        # Timeframe
        cfg_layout.addWidget(QLabel("周期:"))
        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["1d", "1w", "1M"])
        self._tf_combo.setCurrentText("1d")
        self._tf_combo.setMinimumWidth(55)
        cfg_layout.addWidget(self._tf_combo)

        # Refresh board list button
        self._refresh_board_btn = QPushButton("刷新列表")
        self._refresh_board_btn.clicked.connect(self._refresh_board_list)
        cfg_layout.addWidget(self._refresh_board_btn)

        # Only scan stocks not yet in results (dedup)
        self._skip_scanned_cb = QCheckBox("跳过已扫描的股票")
        self._skip_scanned_cb.setChecked(True)
        self._skip_scanned_cb.setToolTip("再次扫描时跳过之前已出结果的股票（按代码去重）")
        cfg_layout.addWidget(self._skip_scanned_cb)

        layout.addWidget(cfg_group)

        # ── Action buttons ───────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        self._start_btn = QPushButton("开始批量扫描")
        self._start_btn.setObjectName("primaryButton")
        self._start_btn.setMinimumWidth(140)
        self._start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self._start_btn)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_layout.addWidget(self._stop_btn)

        self._clear_btn = QPushButton("清空结果")
        self._clear_btn.clicked.connect(self._clear_results)
        btn_layout.addWidget(self._clear_btn)

        self._export_btn = QPushButton("导出统计")
        self._export_btn.clicked.connect(self._export_stats)
        self._export_btn.setEnabled(False)
        btn_layout.addWidget(self._export_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # ── Progress ─────────────────────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("mutedLabel")
        layout.addWidget(self._status_label)

        # ── Results table ────────────────────────────────────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(_COL_COUNT)
        self._table.setHorizontalHeaderLabels(_COL_HEADERS)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        # Connect cell click (for stock name → K-line chart)
        self._table.cellClicked.connect(self._on_name_clicked)

        # Default column widths
        widths = [70, 100, 60, 80, 55, 70, 55, 55, 45, 300, 120]
        for i, w in enumerate(widths):
            self._table.setColumnWidth(i, w)
        layout.addWidget(self._table, stretch=1)

        # ── Statistical summary ──────────────────────────────────────────────
        self._summary_edit = QTextEdit()
        self._summary_edit.setReadOnly(True)
        self._summary_edit.setMaximumHeight(160)
        self._summary_edit.setPlaceholderText("扫描完成后将在此显示统计汇总…")
        layout.addWidget(self._summary_edit)

        # Restore last-used board type
        self._on_board_type_changed(0)

    # ── Board list loading ───────────────────────────────────────────────────

    def _on_board_type_changed(self, _index: int) -> None:
        self._board_type_combo.setEnabled(False)
        self._refresh_board_list()

    def _refresh_board_list(self) -> None:
        """Fetch board list for the selected board type and populate combo."""
        board_type = self._board_type_combo.currentData() or "industry"
        self._status_label.setText(f"正在拉取{BOARD_TYPE_LABELS.get(board_type, '')}列表…")

        # Use QTimer to allow UI update before blocking fetch
        QTimer.singleShot(50, lambda: self._do_refresh_boards(board_type))

    def _do_refresh_boards(self, board_type: str) -> None:
        try:
            boards = fetch_sector_boards(board_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch boards: %s", exc)
            self._status_label.setText(f"拉取板块列表失败: {exc}")
            self._board_type_combo.setEnabled(True)
            return

        self._board_cache[board_type] = boards
        self._board_combo.clear()
        if not boards:
            self._board_combo.addItem("（无数据）", "")
            self._status_label.setText("板块列表为空")
        else:
            for b in boards:
                code = b.get("code", "")
                name = b.get("name", code)
                pct = b.get("pct_chg")
                pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
                self._board_combo.addItem(f"{name}{pct_str}", code)
            self._status_label.setText(
                f"共 {len(boards)} 个板块, 已选择: {boards[0].get('name', '')}"
            )
        self._board_type_combo.setEnabled(True)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        """Start the batch scan."""
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "扫描正在进行中")
            return

        board_code = self._board_combo.currentData()
        if not board_code:
            QMessageBox.warning(self, "提示", "请先选择一个板块")
            return

        sort_key = self._sort_combo.currentData()
        sort_field, sort_label, sort_desc = SORT_CRITERIA_MAP.get(
            sort_key, ("f3", True, "涨幅")
        )

        top_n = self._top_n_spin.value()
        timeframe = self._tf_combo.currentText()

        # Fetch stock list from the board
        self._status_label.setText(f"正在获取板块成分股列表…")
        self._start_btn.setEnabled(False)

        try:
            stocks = fetch_board_top_stocks(
                board_code,
                sort_field=sort_field,
                top_n=top_n,
                sort_desc=sort_desc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch board stocks: %s", exc)
            QMessageBox.critical(self, "错误", f"获取成分股失败:\n{exc}")
            self._start_btn.setEnabled(True)
            self._board_type_combo.setEnabled(True)
            return

        if not stocks:
            QMessageBox.information(self, "提示", "该板块未返回成分股数据")
            self._start_btn.setEnabled(True)
            return

        # Dedup against previously scanned stocks
        skip = self._skip_scanned_cb.isChecked()
        if skip and self._results:
            scanned_codes = {r.code for r in self._results if r.error is None}
            before = len(stocks)
            stocks = [s for s in stocks if s.get("code", "") not in scanned_codes]
            skipped = before - len(stocks)
            if skipped:
                self._status_label.setText(f"跳过 {skipped} 只已扫描股票, 剩余 {len(stocks)} 只")
        else:
            # Clear if not skipping (new scan)
            self._clear_results()

        if not stocks:
            QMessageBox.information(self, "提示", "所有股票已扫描完毕，无需重复扫描")
            self._start_btn.setEnabled(True)
            return

        # Build orchestrator factory
        def _make_orchestrator():
            from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

            client = getattr(self._ctx, "client", None)
            assembler = getattr(self._ctx, "assembler", None)
            router = getattr(self._ctx, "router", None)
            validator = getattr(self._ctx, "validator", None)
            pending_writer = getattr(self._ctx, "pending_writer", None)
            exp_reader = getattr(self._ctx, "exp_reader", None)

            if any(x is None for x in [client, assembler, router, validator,
                                       pending_writer, exp_reader]):
                return None

            return TwoStageOrchestrator(
                client=client,
                assembler=assembler,
                router=router,
                validator=validator,
                pending_writer=pending_writer,
                exp_reader=exp_reader,
                settings=getattr(self._ctx, "settings", None),
            )

        from pa_agent.gui.batch_scan_worker import BatchScanWorker

        self._worker = BatchScanWorker(
            stocks,
            bar_count=self._analysis_bar_count(),
            timeframe=timeframe,
            orchestrator_factory=_make_orchestrator,
            parent=None,
        )
        self._worker.progress_changed.connect(self._on_progress)
        self._worker.status_message.connect(self._status_label.setText)
        self._worker.stock_done.connect(self._on_stock_done)
        self._worker.finished.connect(self._on_scan_finished)

        board_name = self._board_combo.currentText().split(" (")[0]
        self._status_label.setText(
            f"扫描 {board_name} 按{sort_label}前{top_n}只 "
            f"({len(stocks)}只) …"
        )
        self._progress_bar.setMaximum(len(stocks))
        self._progress_bar.setValue(0)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._board_type_combo.setEnabled(False)
        self._board_combo.setEnabled(False)
        self._sort_combo.setEnabled(False)
        self._top_n_spin.setEnabled(False)
        self._tf_combo.setEnabled(False)

        self._worker.start()

    def _on_stop(self) -> None:
        """Stop the batch scan."""
        if self._worker is not None:
            self._worker.cancel()
            self._status_label.setText("正在停止…")

    def _clear_results(self) -> None:
        """Clear all results."""
        self._results = []
        self._table.setRowCount(0)
        self._summary_edit.clear()
        self._export_btn.setEnabled(False)

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _on_progress(self, current: int, total: int) -> None:
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)

    def _on_stock_done(self, result: Any) -> None:
        """Add a single stock result to the table."""
        self._results.append(result)
        self._add_table_row(result)
        self._update_summary()

    def _on_scan_finished(self) -> None:
        """Batch scan finished (or was cancelled)."""
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._board_type_combo.setEnabled(True)
        self._board_combo.setEnabled(True)
        self._sort_combo.setEnabled(True)
        self._top_n_spin.setEnabled(True)
        self._tf_combo.setEnabled(True)
        self._export_btn.setEnabled(bool(self._results))
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._update_summary()

        count_success = sum(1 for r in self._results if r.error is None)
        count_fail = sum(1 for r in self._results if r.error is not None)
        self._status_label.setText(
            f"扫描完成: {count_success} 成功, {count_fail} 失败, "
            f"共 {len(self._results)} 只"
        )

    # ── Table helpers ────────────────────────────────────────────────────────

    def _add_table_row(self, r: Any) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        items = [
            (r.code, _COL_CODE),
            (r.name, _COL_NAME),
            (r.stage1_direction or "", _COL_DIRECTION),
            (r.stage1_cycle or "", _COL_CYCLE),
            (str(r.stage1_confidence) if r.stage1_confidence is not None else "", _COL_CONFIDENCE),
            (r.stage2_order_type or "", _COL_ORDER_TYPE),
            (r.stage2_order_direction or "", _COL_ORDER_DIR),
            (r.stage2_terminal or "", _COL_TERMINAL),
            (f"{r.stage2_estimated_win_rate}%" if r.stage2_estimated_win_rate is not None else "", _COL_WIN_RATE),
            ((r.stage2_reasoning or "")[:120], _COL_REASONING),
            (r.error or "", _COL_ERROR),
        ]

        for text, col in items:
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft
                if col in (_COL_REASONING, _COL_ERROR, _COL_NAME)
                else Qt.AlignmentFlag.AlignCenter
            )
            # Color the direction cell
            if col == _COL_DIRECTION:
                if r.stage1_direction == "bullish":
                    item.setForeground(QColor("#ff4757"))
                elif r.stage1_direction == "bearish":
                    item.setForeground(QColor("#00d084"))
                else:
                    item.setForeground(QColor("#8b949e"))
            # Color error cell
            if col == _COL_ERROR and r.error:
                item.setForeground(QColor("#ff4757"))
            self._table.setItem(row, col, item)

        # Make name column clickable (opens K-line chart)
        if r.kline_bars:
            name_item = self._table.item(row, _COL_NAME)
            if name_item:
                name_item.setForeground(QColor("#58a6ff"))
                font = name_item.font()
                font.setUnderline(True)
                name_item.setFont(font)
                name_item.setToolTip("点击查看 K 线图表")

        self._table.resizeRowToContents(row)

    # ── K-line chart viewer ──────────────────────────────────────────────────

    def _on_name_clicked(self, row: int, col: int) -> None:
        """Open K-line chart when a stock name is clicked."""
        if col != _COL_NAME:
            return
        if row < 0 or row >= len(self._results):
            return
        r = self._results[row]
        if not r.kline_bars:
            return
        from pa_agent.gui.kline_view_dialog import KlineViewDialog
        dlg = KlineViewDialog(r.code, r.name, r.kline_bars, parent=self)
        dlg.exec()

    # ── Statistics ───────────────────────────────────────────────────────────

    def _update_summary(self) -> None:
        """Recompute and display the statistical summary."""
        if not self._results:
            self._summary_edit.clear()
            return

        total = len(self._results)
        success = [r for r in self._results if r.error is None]
        failed = [r for r in self._results if r.error is not None]

        # Direction counts
        bullish = sum(1 for r in success if r.stage1_direction == "bullish")
        bearish = sum(1 for r in success if r.stage1_direction == "bearish")
        neutral = sum(1 for r in success if r.stage1_direction == "neutral")

        # Cycle position counts
        cycles: dict[str, int] = {}
        for r in success:
            c = r.stage1_cycle or "unknown"
            cycles[c] = cycles.get(c, 0) + 1

        # Gate result counts
        gates: dict[str, int] = {}
        for r in success:
            g = r.stage1_gate or "unknown"
            gates[g] = gates.get(g, 0) + 1

        # Order type counts
        order_types: dict[str, int] = {}
        for r in success:
            ot = r.stage2_order_type or "不下单"
            order_types[ot] = order_types.get(ot, 0) + 1

        # Terminal outcome counts
        terminals: dict[str, int] = {}
        for r in success:
            t = r.stage2_terminal or "unknown"
            terminals[t] = terminals.get(t, 0) + 1

        # Order direction counts
        long_count = sum(1 for r in success if r.stage2_order_direction == "做多")
        short_count = sum(1 for r in success if r.stage2_order_direction == "做空")

        # Average confidence / win rate
        confs = [r.stage1_confidence for r in success if r.stage1_confidence is not None]
        trade_confs = [r.stage2_trade_confidence for r in success if r.stage2_trade_confidence is not None]
        win_rates = [r.stage2_estimated_win_rate for r in success if r.stage2_estimated_win_rate is not None]

        avg_conf = sum(confs) / len(confs) if confs else 0
        avg_trade_conf = sum(trade_confs) / len(trade_confs) if trade_confs else 0
        avg_win_rate = sum(win_rates) / len(win_rates) if win_rates else 0

        # Top patterns
        pattern_counts: dict[str, int] = {}
        for r in success:
            if r.stage1_patterns:
                for p in r.stage1_patterns:
                    pattern_counts[p] = pattern_counts.get(p, 0) + 1
        top_patterns = sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]

        # Build summary text
        lines = [
            "═" * 60,
            f"  批量扫描统计汇总  (共 {total} 只, 成功 {len(success)}, 失败 {len(failed)})",
            "═" * 60,
            "",
            f"【方向分布】 多头 {bullish} 只 | 空头 {bearish} 只 | 中性 {neutral} 只",
            f"【操作方向】 做多 {long_count} 只 | 做空 {short_count} 只",
            "",
            f"【平均诊断置信度】 {avg_conf:.0f} / 100  (n={len(confs)})",
            f"【平均交易置信度】 {avg_trade_conf:.0f} / 100  (n={len(trade_confs)})",
            f"【平均预估胜率】   {avg_win_rate:.0f}%  (n={len(win_rates)})",
            "",
        ]

        if cycles:
            cycle_str = " | ".join(f"{k}: {c}只" for k, c in
                                    sorted(cycles.items(), key=lambda x: -x[1]))
            lines.append(f"【周期分布】 {cycle_str}")

        if gates:
            gate_str = " | ".join(f"{k}: {c}只" for k, c in gates.items())
            lines.append(f"【关口结果】 {gate_str}")

        if order_types:
            ot_str = " | ".join(f"{k}: {c}只" for k, c in
                                 sorted(order_types.items(), key=lambda x: -x[1]))
            lines.append(f"【下单类型】 {ot_str}")

        if terminals:
            term_str = " | ".join(f"{k}: {c}只" for k, c in
                                   sorted(terminals.items(), key=lambda x: -x[1]))
            lines.append(f"【终局分布】 {term_str}")

        if top_patterns:
            pat_str = " | ".join(f"{p}({c})" for p, c in top_patterns)
            lines.append(f"【常见形态】 {pat_str}")

        # ── 未来走势预期统计 ────────────────────────────────────────────────
        nbp_valid = [r for r in success if r.next_bar_direction is not None]
        if nbp_valid:
            nb_bull = sum(1 for r in nbp_valid if r.next_bar_direction == "bullish")
            nb_bear = sum(1 for r in nbp_valid if r.next_bar_direction == "bearish")
            nb_neut = sum(1 for r in nbp_valid if r.next_bar_direction == "neutral")
            nb_unpred = sum(1 for r in nbp_valid if r.next_bar_unpredictable)
            lines.append("")
            lines.append(f"【下一根K线预期】 (n={len(nbp_valid)})")
            lines.append(f"  看涨 {nb_bull} 只 | 看跌 {nb_bear} 只 | 中性 {nb_neut} 只 | 不可预测 {nb_unpred} 只")
            # Average next-bar probabilities
            nb_probs: dict[str, list[float]] = {"bullish": [], "bearish": [], "neutral": []}
            for r in nbp_valid:
                if r.next_bar_probabilities and not r.next_bar_unpredictable:
                    for k in ("bullish", "bearish", "neutral"):
                        v = r.next_bar_probabilities.get(k)
                        if v is not None:
                            try:
                                nb_probs[k].append(float(v))
                            except (TypeError, ValueError):
                                pass
            if any(nb_probs.values()):
                avg_nb = {
                    k: (sum(vs) / len(vs)) if vs else 0
                    for k, vs in nb_probs.items()
                }
                lines.append(
                    f"  平均概率: 涨 {avg_nb['bullish']:.0f}% / "
                    f"跌 {avg_nb['bearish']:.0f}% / "
                    f"平 {avg_nb['neutral']:.0f}%"
                )

        ncp_valid = [r for r in success if r.next_cycle_direction is not None]
        if ncp_valid:
            nc_bull = sum(1 for r in ncp_valid if r.next_cycle_direction == "bullish")
            nc_bear = sum(1 for r in ncp_valid if r.next_cycle_direction == "bearish")
            nc_neut = sum(1 for r in ncp_valid if r.next_cycle_direction == "neutral")
            nc_unpred = sum(1 for r in ncp_valid if r.next_cycle_unpredictable)
            # Cycle type counts
            nc_cycles: dict[str, int] = {}
            for r in ncp_valid:
                c = r.next_cycle_cycle or "unknown"
                nc_cycles[c] = nc_cycles.get(c, 0) + 1
            lines.append("")
            lines.append(f"【下一市场周期预期】 (n={len(ncp_valid)})")
            lines.append(f"  看涨 {nc_bull} 只 | 看跌 {nc_bear} 只 | 中性 {nc_neut} 只 | 不可预测 {nc_unpred} 只")
            if nc_cycles:
                nc_cycle_str = " | ".join(f"{k}: {c}只" for k, c in
                                           sorted(nc_cycles.items(), key=lambda x: -x[1]))
                lines.append(f"  预期周期: {nc_cycle_str}")

        if failed:
            lines.append("")
            lines.append(f"【失败详情】")
            for r in failed:
                lines.append(f"  {r.code} {r.name}: {r.error}")

        lines.append("")
        lines.append("═" * 60)

        self._summary_edit.setText("\n".join(lines))

    def _export_stats(self) -> None:
        """Copy statistics to clipboard."""
        text = self._summary_edit.toPlainText()
        if text.strip():
            from PyQt6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(text)
            self._status_label.setText("统计已复制到剪贴板")
        else:
            self._status_label.setText("暂无统计可导出")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _analysis_bar_count(self) -> int:
        settings = getattr(self._ctx, "settings", None)
        if settings is None:
            return 100
        return int(getattr(settings.general, "analysis_bar_count", 100))

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Clean up worker on close."""
        if self._worker is not None:
            self._worker.cancel()
            if not self._worker.wait(3000):
                logger.warning("Batch scan worker did not finish in time")
        super().closeEvent(event)
