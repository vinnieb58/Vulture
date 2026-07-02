"""Search result statistics for logging and Discord summaries."""

from __future__ import annotations

import logging
from dataclasses import dataclass


@dataclass
class SearchStats:
    ticketmaster_returned: int = 0
    seatgeek_returned: int = 0
    after_genre_filter: int = 0
    noise_hidden: int = 0
    merged_count: int = 0
    displayed_count: int = 0

    def log_summary(self, *, logger: logging.Logger, prefix: str = "Concert search") -> None:
        logger.info(
            "%s stats: tm=%d sg=%d genre_filtered=%d noise_hidden=%d merged=%d displayed=%d",
            prefix,
            self.ticketmaster_returned,
            self.seatgeek_returned,
            self.after_genre_filter,
            self.noise_hidden,
            self.merged_count,
            self.displayed_count,
        )
