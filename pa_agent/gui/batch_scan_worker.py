"""Background worker for batch scanning A-share stocks from a board/sector."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from pa_agent.data.base import KlineBar
from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)


@dataclass
class BatchScanStockResult:
    """Result for a single stock in the batch scan."""

    code: str
    name: str
    error: str | None = None
    stage1_direction: str | None = None
    stage1_confidence: int | None = None
    stage1_cycle: str | None = None
    stage1_gate: str | None = None
    stage1_patterns: list[str] | None = None
    stage2_order_type: str | None = None
    stage2_order_direction: str | None = None
    stage2_trade_confidence: int | None = None
    stage2_diagnosis_confidence: int | None = None
    stage2_terminal: str | None = None
    stage2_reasoning: str | None = None
    stage2_entry_price: float | None = None
    stage2_tp: float | None = None
    stage2_sl: float | None = None
    stage2_estimated_win_rate: int | None = None
    # 未来走势预期（来自 stage2_decision.next_bar/cycle_prediction）
    next_bar_direction: str | None = None
    next_bar_unpredictable: bool | None = None
    next_bar_probabilities: dict | None = None
    next_bar_reasoning: str | None = None
    next_cycle_cycle: str | None = None
    next_cycle_direction: str | None = None
    next_cycle_unpredictable: bool | None = None
    next_cycle_probabilities: dict | None = None
    next_cycle_reasoning: str | None = None
    # K 线数据（用于点击查看图表）
    kline_bars: list | None = None
    # 完整记录
    record: Any = None  # full AnalysisRecord for debug


class BatchScanWorker(QThread):
    """Background thread for batch scanning stocks.

    Fetches stock list from a board/sector, then for each stock fetches daily
    K-line data and runs the two-stage AI analysis.  Results are emitted per
    stock and as a final summary.
    """

    # Overall progress
    progress_changed = pyqtSignal(int, int)   # (current, total)
    status_message = pyqtSignal(str)
    # Per-stock result
    stock_done = pyqtSignal(object)           # BatchScanStockResult
    # Finished
    finished = pyqtSignal()                   # all stocks done / aborted
    # Live-stream events (for optional real-time panel)
    streaming_token = pyqtSignal(str, str, str)  # (symbol, stage, chunk)

    def __init__(
        self,
        stocks: list[dict[str, Any]],
        *,
        bar_count: int = 100,
        timeframe: str = "1d",
        orchestrator_factory: Callable[[], Any] | None = None,
        cancel_token: CancelToken | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._stocks = list(stocks)  # list of {code, name, ...}
        self._bar_count = bar_count
        self._timeframe = timeframe
        self._orchestrator_factory = orchestrator_factory
        self._cancel_token = cancel_token or CancelToken()
        self._results: list[BatchScanStockResult] = []

    @property
    def results(self) -> list[BatchScanStockResult]:
        return list(self._results)

    def cancel(self) -> None:
        """Request cancellation (checked before each stock and each Stage)."""
        self._cancel_token.set()

    def run(self) -> None:
        total = len(self._stocks)
        self._results = []

        # Build orchestrator once (reused across all stocks)
        orchestrator = (
            self._orchestrator_factory()
            if self._orchestrator_factory
            else None
        )
        if orchestrator is None:
            logger.error("BatchScanWorker: orchestrator_factory returned None")
            self.status_message.emit("AI 编排器未就绪")
            self.finished.emit()
            return

        for idx, stock_info in enumerate(self._stocks):
            if self._cancel_token.is_set():
                break

            code = stock_info.get("code", "")
            name = stock_info.get("name", code)
            self.progress_changed.emit(idx + 1, total)
            self.status_message.emit(f"[{idx + 1}/{total}] {name} ({code}) — 分析中…")

            result = self._analyze_one(
                code, name, orchestrator,
            )
            self._results.append(result)
            self.stock_done.emit(result)

            # Brief pause between stocks to avoid API rate limits
            if idx + 1 < total and not self._cancel_token.is_set():
                time.sleep(1.5)

        self.status_message.emit(
            f"批量扫描完成: {len(self._results)} 只股票"
        )
        self.finished.emit()

    # ── Single-stock analysis ────────────────────────────────────────────────

    def _analyze_one(
        self,
        code: str,
        name: str,
        orchestrator: Any,
    ) -> BatchScanStockResult:
        """Analyse a single stock and return the result."""
        # Fetch kline bars first (will also use for chart viewing later)
        kline_bars = self._fetch_kline_bars(code)
        if kline_bars is None:
            return BatchScanStockResult(
                code=code, name=name,
                error=f"获取 {self._timeframe} K 线数据失败",
            )

        try:
            from pa_agent.data.snapshot import build_display_frame

            now_ms = int(time.time() * 1000)
            frame = build_display_frame(
                kline_bars,
                self._bar_count,
                code,
                self._timeframe,
                now_ms=now_ms,
            )
            if frame is None:
                return BatchScanStockResult(
                    code=code, name=name,
                    error=f"构建 K 线帧失败",
                    kline_bars=kline_bars,
                )

            # Create internal cancel token per stock so we can still cancel
            # mid-analysis but also isolate per-stock failures.
            from pa_agent.util.threading import CancelToken as _CT

            stock_ct = _CT()
            parent_ct = self._cancel_token

            # Proxy cancel check: if the parent is cancelled, cancel the child.
            def _check_parent() -> None:
                if parent_ct.is_set():
                    stock_ct.set()

            # ── Run orchestrator submit ─────────────────────────────────────
            record = orchestrator.submit(
                frame,
                stock_ct,
                on_event=lambda ev: _check_parent(),
            )
            # If cancelled mid-way, return partial / error result
            if parent_ct.is_set():
                return BatchScanStockResult(
                    code=code, name=name,
                    error="已取消",
                    kline_bars=kline_bars,
                )
            if record is None or getattr(record, "exception", None):
                exc_info = getattr(record, "exception", None) if record else {}
                exc_msg = (exc_info or {}).get("message", "未知错误") if isinstance(exc_info, dict) else str(exc_info or "未知错误")
                return BatchScanStockResult(
                    code=code, name=name, error=exc_msg,
                    record=record, kline_bars=kline_bars,
                )

            # ── Extract key fields ──────────────────────────────────────────
            s1 = getattr(record, "stage1_diagnosis", None) or {}
            s2_full = getattr(record, "stage2_decision", None) or {}
            s2 = s2_full.get("decision") or {} if isinstance(s2_full, dict) else {}
            term = s2_full.get("terminal") or {} if isinstance(s2_full, dict) else {}

            # 未来走势预期
            nbp = s2_full.get("next_bar_prediction") or {} if isinstance(s2_full, dict) else {}
            ncp = s2_full.get("next_cycle_prediction") or {} if isinstance(s2_full, dict) else {}

            d_conf_raw = s1.get("diagnosis_confidence")
            d_conf = int(d_conf_raw) if d_conf_raw is not None else None

            tc_raw = s2.get("trade_confidence")
            trade_conf = int(tc_raw) if tc_raw is not None else None

            dc_raw = s2.get("diagnosis_confidence")
            diag_conf = int(dc_raw) if dc_raw is not None else None

            ew_raw = s2.get("estimated_win_rate")
            ew = int(ew_raw) if ew_raw is not None else None

            return BatchScanStockResult(
                code=code,
                name=name,
                error=None,
                stage1_direction=s1.get("direction"),
                stage1_confidence=d_conf,
                stage1_cycle=s1.get("cycle_position"),
                stage1_gate=s1.get("gate_result"),
                stage1_patterns=s1.get("detected_patterns"),
                stage2_order_type=s2.get("order_type"),
                stage2_order_direction=s2.get("order_direction"),
                stage2_trade_confidence=trade_conf,
                stage2_diagnosis_confidence=diag_conf,
                stage2_terminal=term.get("outcome"),
                stage2_reasoning=s2.get("reasoning"),
                stage2_entry_price=s2.get("entry_price"),
                stage2_tp=s2.get("take_profit_price"),
                stage2_sl=s2.get("stop_loss_price"),
                stage2_estimated_win_rate=ew,
                # 未来走势预期
                next_bar_direction=nbp.get("direction"),
                next_bar_unpredictable=nbp.get("unpredictable"),
                next_bar_probabilities=nbp.get("probabilities"),
                next_bar_reasoning=nbp.get("reasoning"),
                next_cycle_cycle=ncp.get("cycle"),
                next_cycle_direction=ncp.get("direction"),
                next_cycle_unpredictable=ncp.get("unpredictable"),
                next_cycle_probabilities=ncp.get("probabilities"),
                next_cycle_reasoning=ncp.get("reasoning"),
                # K 线数据
                kline_bars=kline_bars,
                record=record,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch scan failed for %s: %s", code, exc)
            return BatchScanStockResult(
                code=code, name=name, error=str(exc)[:200],
                kline_bars=kline_bars,
            )

    def _fetch_kline_bars(self, code: str) -> list[KlineBar] | None:
        """Fetch K-line data for *code* and return KlineBar list.

        Uses the East Money daily API directly.  The bars are newest-first
        with seq=1 for the most recent closed bar.
        """
        from pa_agent.data.eastmoney_client import (
            fetch_stock_daily_recent,
        )
        from pa_agent.data.base import normalize_kline_bar

        raw_rows = fetch_stock_daily_recent(code, n=self._bar_count + 5)
        if not raw_rows:
            return None

        # Convert to newest-first bar dicts
        bars_dict: list[dict[str, Any]] = []
        for row in raw_rows:
            row_time = row.get("time")
            if hasattr(row_time, "timestamp"):
                ts_ms = int(row_time.timestamp() * 1000)
            else:
                from pa_agent.data.ashare_common import row_time_to_ts_ms
                ts_ms = row_time_to_ts_ms(row_time)
            bars_dict.append({
                "ts_open": ts_ms,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
                "amount": float(row.get("amount", 0.0) or 0.0),
                "pct_chg": row.get("pct_chg"),
                "closed": True,
            })
        # Reverse to newest-first
        bars_dict = list(reversed(bars_dict))

        kline_bars: list[KlineBar] = []
        for i, b in enumerate(bars_dict[:self._bar_count]):
            kline_bars.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=i + 1,
                        ts_open=float(b["ts_open"]),
                        open=b["open"],
                        high=b["high"],
                        low=b["low"],
                        close=b["close"],
                        volume=b["volume"],
                        amount=b["amount"],
                        pct_chg=b.get("pct_chg"),
                        closed=True,
                    )
                )
            )
        return kline_bars if kline_bars else None


