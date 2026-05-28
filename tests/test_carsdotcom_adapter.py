"""Unit tests for Cars.com adapter graceful degradation."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.carsdotcom import search_carsdotcom
from adapters.registry import get_capabilities


class TestCarsdotcomCapabilities:
    def test_marked_flaky_and_browser_sensitive(self):
        caps = get_capabilities("carsdotcom")
        assert caps is not None
        assert caps.get("flaky") is True
        assert caps.get("browser_sensitive") is True
        assert caps.get("blocking_risk") == "cloudflare_akamai"
        assert caps.get("failure_mode") == "returns_empty_list"
        assert caps.get("requires_browser") is True


class TestCarsdotcomGracefulFailure:
    @patch("adapters.carsdotcom._fetch_html", return_value=None)
    def test_fetch_failure_returns_empty_list(self, _fetch):
        results = search_carsdotcom("toyota camry", city="77002", limit=5)
        assert results == []

    @patch("adapters.carsdotcom._fetch_html", return_value=None)
    def test_fetch_failure_does_not_raise(self, _fetch):
        search_carsdotcom("honda civic", limit=3)
