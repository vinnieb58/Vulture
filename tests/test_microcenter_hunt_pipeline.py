"""Hunt pipeline wiring for Micro Center (storeid via adapter_options)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.hunt_service import hunt_to_execution_dict
from models.hunt import Hunt


def _minimal_hunt(**overrides) -> Hunt:
    base = dict(
        hunt_id="test-id",
        name="mc-test",
        source_sites=["microcenter"],
        search_terms=["ryzen 7 7800x3d"],
        location="columbus",
        adapter_options={"storeid": "141", "limit": 5},
    )
    base.update(overrides)
    return Hunt(**base)


class TestMicrocenterHuntPipeline:
    def test_execution_dict_forwards_adapter_options(self):
        hunt = _minimal_hunt()
        d = hunt_to_execution_dict(hunt)
        assert d["adapter_options"]["storeid"] == "141"
        assert d["limit"] == 5
        assert d["source_sites"] == ["microcenter"]
