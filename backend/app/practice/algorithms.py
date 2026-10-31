"""Controlled algorithm catalog and deterministic practice target service."""
from __future__ import annotations

from typing import Any, Mapping
from itertools import product

from app.data.errors import DataError
from app.portfolio.allocation import ALLOCATION_POLICY_REGISTRY, ALLOCATION_BASELINE_NAME
from app.portfolio.selection import SELECTION_POLICY_REGISTRY, SELECTION_BASELINE_NAME
from app.portfolio.signals import SIGNAL_CALCULATOR_REGISTRY, SIGNAL_BASELINE_NAME
from app.portfolio.targeting import compute_ideal_target


def _catalog_entry(kind: str, name: str, implementation: Any) -> dict[str, Any]:
    default_descriptions = {
        "signal": "使用固定数据版本和 as_of 计算逐证券信号。",
        "selection": "依据已计算信号确定性筛选候选并保留排除理由。",
        "allocation": "按版本化规则约束将整数分现金分配到目标证券。",
    }
    return {
        "algorithm_id": name,
        "version": str(implementation.version),
        "name": str(getattr(implementation, "display_name", name)),
        "description": str(getattr(implementation, "description", default_descriptions[kind])),
        "parameters": dict(getattr(implementation, "parameters", {})),
        "limitations": list(getattr(implementation, "limitations", [
            "研究分析用途；不保证收益，不构成自动下单或成交。"
        ])),
        "component": kind,
    }


def registered_algorithms() -> list[dict[str, Any]]:
    """Return actual registered components in stable order."""
    groups = (
        ("signal", SIGNAL_CALCULATOR_REGISTRY),
        ("selection", SELECTION_POLICY_REGISTRY),
        ("allocation", ALLOCATION_POLICY_REGISTRY),
    )
    return [
        _catalog_entry(kind, name, registry.require(name))
        for kind, registry in groups
        for name in registry.names()
    ]


def executable_algorithms() -> list[dict[str, Any]]:
    """Enumerate executable frozen component combinations for observation plans."""
    signals = SIGNAL_CALCULATOR_REGISTRY.names()
    selections = SELECTION_POLICY_REGISTRY.names()
    allocations = ALLOCATION_POLICY_REGISTRY.names()
    rows = []
    for signal, selection, allocation in product(signals, selections, allocations):
        config = _configuration_for(signal, selection, allocation)
        versions = [config[kind]["version"] for kind in ("signal", "selection", "allocation")]
        algorithm_id = "+".join((signal, selection, allocation))
        rows.append({
            "algorithm_id": algorithm_id,
            "version": "+".join(versions),
            "name": f"{selection} with {signal} and {allocation}",
            "description": "组合调用已登记信号、筛选和分配组件；不执行订单。",
            "parameters": {},
            "limitations": ["只读研究目标分析；不推进练习、不写账本、不保证收益。"],
            "components": config,
        })
    return rows


def _configuration_for(signal: str, selection: str, allocation: str) -> dict[str, Any]:
    registries = {"signal": SIGNAL_CALCULATOR_REGISTRY, "selection": SELECTION_POLICY_REGISTRY, "allocation": ALLOCATION_POLICY_REGISTRY}
    return {kind: {"code": code, "version": registries[kind].require(code).version, "parameters": {}}
            for kind, code in (("signal", signal), ("selection", selection), ("allocation", allocation))}


def resolve_algorithm(algorithm_id: str, version: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve an observation-plan pin into the exact frozen three-component config."""
    if not isinstance(parameters, Mapping) or dict(parameters):
        raise DataError("algorithm_parameters_invalid", "registered combinations accept no runtime parameters")
    for item in executable_algorithms():
        if item["algorithm_id"] == algorithm_id:
            if item["version"] != version:
                raise DataError("algorithm_version_mismatch", f"algorithm version mismatch for {algorithm_id!r}")
            return {kind: dict(component) for kind, component in item["components"].items()}
    raise DataError("algorithm_unknown", f"unknown executable algorithm: {algorithm_id!r}")


def baseline_configuration() -> dict[str, Any]:
    """Return a serializable, version-pinned baseline component selection."""
    return {
        "signal": {"code": SIGNAL_BASELINE_NAME, "version": SIGNAL_CALCULATOR_REGISTRY.require(SIGNAL_BASELINE_NAME).version, "parameters": {}},
        "selection": {"code": SELECTION_BASELINE_NAME, "version": SELECTION_POLICY_REGISTRY.require(SELECTION_BASELINE_NAME).version, "parameters": {}},
        "allocation": {"code": ALLOCATION_BASELINE_NAME, "version": ALLOCATION_POLICY_REGISTRY.require(ALLOCATION_BASELINE_NAME).version, "parameters": {}},
    }


def calculate_practice_targets(conn, context: Any, account_state: Mapping[str, Any], configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Run pinned components against a bound VersionView; read-only, no ledger writes.

    Result is the targeting report: component versions, candidates/signals,
    exclusions, selection and allocation (integer cents). Caller must supply a
    context bound to this practice's fixed data version and revealed_date.
    """
    expected = {"signal": SIGNAL_CALCULATOR_REGISTRY, "selection": SELECTION_POLICY_REGISTRY, "allocation": ALLOCATION_POLICY_REGISTRY}
    if not isinstance(configuration, Mapping) or set(configuration) != set(expected):
        raise DataError("algorithm_configuration_invalid", "configuration must contain signal, selection, and allocation")
    names: dict[str, str] = {}
    for kind, registry in expected.items():
        selected = configuration[kind]
        if not isinstance(selected, Mapping) or set(selected) != {"code", "version", "parameters"}:
            raise DataError("algorithm_configuration_invalid", f"{kind} selection must pin code, version, and parameters")
        code, version, parameters = selected["code"], selected["version"], selected["parameters"]
        implementation = registry.require(code) if isinstance(code, str) else None
        if implementation is None or version != implementation.version:
            raise DataError("algorithm_version_mismatch", f"unknown {kind} algorithm or version mismatch: {code!r}@{version!r}")
        if not isinstance(parameters, Mapping) or dict(parameters):
            raise DataError("algorithm_parameters_invalid", f"{kind} does not accept runtime parameters")
        names[kind] = code
    bound = getattr(context, "context", None)
    if getattr(context, "view", None) is None or bound is None or bound.purpose != "signal_input":
        raise DataError("algorithm_context_invalid", "a fixed VersionView and revealed_date are required")
    report = compute_ideal_target(conn, context, account_state=account_state,
        signal_name=names["signal"], selection_name=names["selection"], allocation_name=names["allocation"])
    report["frozen_configuration"] = {kind: dict(value) for kind, value in configuration.items()}
    report["units"] = {"allocation_targets": "cents", "cash": "cents", "shares": "shares"}
    return report


def calculate_registered_algorithm(conn, context: Any, account_state: Mapping[str, Any], *, algorithm_id: str, algorithm_version: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Observation-executor entry point using its frozen algorithm pin fields."""
    configuration = resolve_algorithm(algorithm_id, algorithm_version, parameters)
    return calculate_practice_targets(conn, context, account_state, configuration)
