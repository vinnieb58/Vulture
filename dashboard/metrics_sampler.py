"""Background Raven metrics sampler for the dashboard container.

Runs a lightweight daemon thread that appends host metric samples every
``DASHBOARD_METRICS_SAMPLE_INTERVAL_SECONDS`` (default 60s) so history stays
continuous even when nobody is viewing the Nest dashboard.
"""

from __future__ import annotations

import logging
import os
import threading

from raven_metrics_history import MIN_SAMPLE_INTERVAL_SECONDS, record_sample_if_due

logger = logging.getLogger(__name__)

SAMPLER_ENABLED = os.environ.get("DASHBOARD_METRICS_SAMPLER_ENABLED", "1").lower() not in (
    "0",
    "false",
    "no",
)


class MetricsSampler:
    """Daemon thread that records Raven host metrics on a fixed interval."""

    def __init__(self, *, interval_seconds: int = MIN_SAMPLE_INTERVAL_SECONDS) -> None:
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
            "Background metrics sampler started (interval=%ss)",
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
                appended = record_sample_if_due()
                if appended:
                    logger.debug("Background metrics sample appended")
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
    _sampler.start()


def stop_metrics_sampler() -> None:
    """Stop the background sampler if running."""
    global _sampler
    if _sampler is not None:
        _sampler.stop()


def is_metrics_sampler_running() -> bool:
    return _sampler is not None and _sampler.running
