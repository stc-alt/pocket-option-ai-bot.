"""OTCharts OTC streaming adapter.

This module deliberately does not fall back to frequent candle polling when
streaming is unavailable. Pocket Option OTC signals and result checks must use
the same OTC price feed.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

import requests


BASE_URL = "https://otcharts.com/v1"
OTC_SYMBOLS = {
    "EUR/USD": "EURUSD_otc",
    "GBP/USD": "GBPUSD_otc",
    "AUD/USD": "AUDUSD_otc",
    "USD/JPY": "USDJPY_otc",
    "BTC/USD": "BTCUSD_otc",
}


class OTChartsError(RuntimeError):
    pass


class OTCStreamClient:
    """One SSE connection for all configured OTC symbols.

    on_tick, when supplied, is called as on_tick(display_pair, tick_dict).
    Tick dictionaries contain price, time, and the raw OTCharts symbol.
    """

    def __init__(
        self,
        api_key: str,
        on_tick: Optional[Callable[[str, dict], None]] = None,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise OTChartsError("OTCHARTS_API_KEY is missing.")
        self.api_key = api_key
        self.on_tick = on_tick
        self.session = session or requests.Session()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self._latest = {}
        self._bars = {pair: deque(maxlen=600) for pair in OTC_SYMBOLS}
        self._current_bar = {}
        self._status = "stopped"
        self._last_error = None
        self._connected_symbols = set()

    @property
    def status(self):
        with self._lock:
            return {
                "status": self._status,
                "last_error": self._last_error,
                "connected_symbols": sorted(self._connected_symbols),
                "markets": list(OTC_SYMBOLS),
            }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="otcharts-otc-stream", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 3.0):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)
        with self._lock:
            self._status = "stopped"

    def latest_price(self, pair: str):
        with self._lock:
            tick = self._latest.get(pair)
            return None if tick is None else tick["price"]

    def candles(self, pair: str):
        """Return a copy of completed 5-minute OHLC bars for a display pair."""
        with self._lock:
            return [dict(bar) for bar in self._bars.get(pair, ())]

    def _run(self):
        symbols = ",".join(OTC_SYMBOLS.values())
        url = f"{BASE_URL}/stream"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        backoff = 2
        while not self._stop.is_set():
            response = None
            try:
                with self._lock:
                    self._status = "connecting"
                    self._last_error = None
                response = self.session.get(
                    url,
                    params={"venue": "otc", "symbol": symbols},
                    headers=headers,
                    stream=True,
                    timeout=(10, 45),
                )
                if response.status_code in (401, 403):
                    raise OTChartsError(
                        f"OTCharts rejected the key or plan (HTTP {response.status_code})."
                    )
                if response.status_code == 429:
                    raise OTChartsError("OTCharts rate limit reached (HTTP 429).")
                response.raise_for_status()
                with self._lock:
                    self._status = "connected"
                backoff = 2
                self._consume_sse(response)
                if not self._stop.is_set():
                    raise OTChartsError("OTCharts stream closed; reconnecting.")
            except OTChartsError as exc:
                with self._lock:
                    self._last_error = str(exc)
                    self._status = "error"
                # Plan/auth errors are not fixed by reconnecting.
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    break
            except requests.RequestException as exc:
                with self._lock:
                    self._last_error = f"Stream network error: {exc}"
                    self._status = "reconnecting"
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Unexpected stream error: {exc}"
                    self._status = "reconnecting"
            finally:
                if response is not None:
                    response.close()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
        with self._lock:
            if self._status != "error":
                self._status = "stopped"

    def _consume_sse(self, response):
        event_name = "message"
        data_lines = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if self._stop.is_set():
                return
            if raw_line is None:
                continue
            line = raw_line.strip()
            if not line:
                if data_lines:
                    self._handle_event(event_name, "\n".join(data_lines))
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE heartbeat/comment
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines and not self._stop.is_set():
            self._handle_event(event_name, "\n".join(data_lines))

    def _handle_event(self, event_name, payload):
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            raise OTChartsError("OTCharts sent an invalid JSON stream event.")

        if event_name == "connected":
            received = data.get("symbols") or []
            with self._lock:
                self._connected_symbols = set(received)
                missing = set(OTC_SYMBOLS.values()) - self._connected_symbols
                if missing:
                    self._last_error = "Stream connected without all requested markets: " + ", ".join(sorted(missing))
        elif event_name == "tick":
            raw_symbol = data.get("symbol")
            pair = next((name for name, symbol in OTC_SYMBOLS.items() if symbol == raw_symbol), None)
            if pair is None:
                return
            try:
                price = float(data["price"])
                stamp = float(data["time"])
            except (KeyError, TypeError, ValueError):
                return
            if price <= 0:
                return
            tick = {"price": price, "time": stamp, "symbol": raw_symbol}
            self._record_tick(pair, tick)
            if self.on_tick:
                self.on_tick(pair, tick)
        elif event_name in ("closed", "dropped", "replaced"):
            detail = data.get("message") or data.get("reason") or event_name
            with self._lock:
                self._last_error = f"OTCharts stream event: {detail}"
            if event_name == "closed":
                raise OTChartsError(f"OTCharts closed the stream: {detail}")

    def _record_tick(self, pair, tick):
        # Candle buckets use the provider's Unix timestamp. Pocket Option OTC
        # timestamps are broker-clock based; keep the provider timestamp intact.
        bucket = int(tick["time"] // 300) * 300
        with self._lock:
            self._latest[pair] = tick
            current = self._current_bar.get(pair)
            if current is None or current["time"] != bucket:
                if current is not None:
                    self._bars[pair].append(current)
                self._current_bar[pair] = {
                    "time": bucket,
                    "open": tick["price"],
                    "high": tick["price"],
                    "low": tick["price"],
                    "close": tick["price"],
                }
            else:
                current["high"] = max(current["high"], tick["price"])
                current["low"] = min(current["low"], tick["price"])
                current["close"] = tick["price"]
