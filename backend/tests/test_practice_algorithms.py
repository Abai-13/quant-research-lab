from __future__ import annotations

import pytest

from app.data.errors import DataError
from app.practice.algorithms import baseline_configuration, calculate_practice_targets, executable_algorithms, registered_algorithms, resolve_algorithm


def test_catalog_is_built_from_real_component_registries():
    items = registered_algorithms()
    assert {item["component"] for item in items} == {"signal", "selection", "allocation"}
    assert all(item["algorithm_id"] and item["version"] for item in items)
    assert items == registered_algorithms()


def test_configuration_pins_versions_and_rejects_unknown_or_drifted_components():
    config = baseline_configuration()
    with pytest.raises(DataError, match="version mismatch"):
        config["signal"]["version"] = "future"
        calculate_practice_targets(None, None, {}, config)

    config = baseline_configuration()
    config["selection"]["code"] = "not-registered"
    with pytest.raises(DataError):
        calculate_practice_targets(None, None, {}, config)


def test_configuration_rejects_unrecognized_parameters():
    config = baseline_configuration()
    config["allocation"]["parameters"] = {"cap": 0.5}
    with pytest.raises(DataError, match="does not accept"):
        calculate_practice_targets(None, None, {}, config)


def test_observation_plan_pin_resolves_to_a_frozen_component_combination():
    item = executable_algorithms()[0]
    frozen = resolve_algorithm(item["algorithm_id"], item["version"], item["parameters"])
    assert set(frozen) == {"signal", "selection", "allocation"}
    assert frozen == item["components"]
    with pytest.raises(DataError, match="version mismatch"):
        resolve_algorithm(item["algorithm_id"], "changed", {})
