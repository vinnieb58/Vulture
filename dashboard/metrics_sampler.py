"""Background Raven metrics sampler for the dashboard container.

Collects raw CPU readings every 5 seconds, rolls them into 60-second buckets,
and persists buckets to JSONL so short CPU spikes are captured accurately.
"""

from __future__ import annotations

import logging
import os
import threading

from raven_metrics_history import RAW_SAMPLE_INTERVAL_SECONDS, record_raw_sample

logger = logging.getLogger(__name__)

SAMPLER_ENABLED = os.environ.get("DASHBOARD_METRICS_SAMPLER_ENABLED", "1").lower() not in (
    "0",
    "false",
    "no",
)


class MetricsSampler:
    """Daemon thread that records Raven host metrics on a fixed interval."""

    def __init__(self, *, interval_seconds: int = RAW_SAMPLE_INTERVAL_SECONDS) -> None:
        self._interval = max(1, interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="raven-metrics-sampler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Background metrics sampler started (raw_interval=%ss)",
            self._interval,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5.0)
            self._thread = None
        logger.info("Background metrics sampler stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                appended = record_raw_sample()
                if appended:
                    logger.debug("Background metrics bucket appended")
            except Exception:
                logger.exception("Background metrics sample failed")
            if self._stop_event.wait(self._interval):
                break


_sampler: MetricsSampler | None = None


def start_metrics_sampler() -> None:
    """Start the background sampler unless disabled by env."""
    global _sampler
    if not SAMPLER_ENABLED:
        logger.info("Background metrics sampler disabled by env")
        return
    if _sampler is None:
        _sampler = MetricsSampler()
    if _sampler.running:
        logger.debug("Background metrics sampler already running; not starting another")
        return
    _sampler.start()


def stop_metrics_sampler() -> None:
    """Stop the background sampler if running."""
    global _sampler
    if _sampler is not None:
        _sampler.stop()


def is_metrics_sampler_running() -> bool:
    return _sampler is not None and _sampler.running
