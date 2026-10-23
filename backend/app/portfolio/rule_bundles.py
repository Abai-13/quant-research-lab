"""不可变、显式版本的规则/费用组合（阶段三 S1）。

设计决定（派工冻结）：

- 规则与费用组合一旦创建不可改写；内容变化必须使用新版本；
- ``assumption_scope`` 区分为 ``synthetic_simulation``（合成模拟假设）与
  ``verified_configuration``（经核验的配置）；合成费率显式标注仅为模拟假设，
  绝不能当作真实市场/券商费用，也不得冒充已核验配置；
- ``same_day_reusable=false`` 只是在官方规则核验未决时的保守模拟默认，
  必须连同 ``verification_status`` 一起以显式版本保存；
- ``bundle_id`` 由 (kind, version, content) 的规范化摘要确定，不含随机 nonce。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from ..data.digests import canonical_json_bytes, sha256_of_object
from ..data.errors import DataError
from ..data.timeutil import utc_now_iso

__all__ = [
    "BUNDLE_KINDS",
    "ASSUMPTION_SCOPES",
    "DEFAULT_PARAMETER_BUNDLE",
    "DEFAULT_FEE_BUNDLE",
    "DEFAULT_BUNDLE_VERSIONS",
    "create_rule_bundle",
    "load_rule_bundle",
    "ensure_default_bundles",
    "require_assumption_scope",
]

BUNDLE_KINDS = ("parameter", "fee", "execution", "corporate_action")
ASSUMPTION_SCOPES = ("synthetic_simulation", "verified_configuration")

DEFAULT_BUNDLE_VERSIONS = {
    # 1.2：新增保守保留缓冲与默认建议有效期（内容变化必须换版本）。
    "parameter": "p3-param-sim-1.2",
    # 1.1：修正费率注记的数学表述（rate_bp=5 即 0.05%/万分之5），内容变化换版本。
    "fee": "p3-fee-sim-1.1",
}

# 策略基线沿用阶段三 §3.4 的研究候选：L=60、s=5、K=120（研究配置，非投资建议）。
# ``same_day_reusable=false`` 是官方规则核验未决时的保守模拟默认，绝非已核验规则。
DEFAULT_PARAMETER_BUNDLE: dict[str, Any] = {
    "bundle_kind": "parameter",
    "assumption_scope": "synthetic_simulation",
    "selection_baseline": {"lookback_days": 60, "reverse_offset_days": 5, "trend_window_days": 120},
    "target_preferences": {
        "max_holdings_hint": 10,
        "per_stock_weight_hint_bp": 1000,
        "hint_only": True,
    },
    "execution_assumptions": {
        "same_day_sale_proceeds_reusable": False,
        "verification_status": "unverified",
        "basis": "conservative_simulation_default_pending_official_rule_check",
    },
    # 未入选目标的持仓不自动清仓；只有显式版本化规则才能关闭该保留缓冲。
    "rebalance_buffer": {
        "retain_unselected_holdings": True,
        "min_trade_notional_cents": 0,
        "basis": "synthetic_conservative_retention_buffer",
    },
    # 建议默认 24 小时有效；上限 7 天，过期只影响读取状态，不改写工件。
    "recommendation_validity": {
        "default_hours": 24,
        "max_hours": 168,
        "basis": "synthetic_simulation_default",
    },
    "money_unit": "cny_cents",
    "shares_unit": "shares",
}

# 全部费率均为合成演示假设（synthetic_simulation），不代表任何真实市场或券商费率；
# 真实费率未来必须经 ``verified_configuration`` 的券商配置接入并按日期版本化。
# 数学约定：``rate_bp`` 是基点（1bp = 0.01% = 万分之1），注记必须与 rate_bp 一致。
DEFAULT_FEE_BUNDLE: dict[str, Any] = {
    "bundle_kind": "fee",
    "assumption_scope": "synthetic_simulation",
    "currency": "CNY",
    "components": [
        {
            "code": "commission",
            "rate_bp": 5,
            "minimum_cents": 500,
            "charging": "per_side",
            "note": "合成佣金假设：万分之5（0.05%）、单边最低5元；纯模拟演示值，"
            "与任何真实券商费率无关",
        },
        {
            "code": "stamp_duty_sell",
            "rate_bp": 5,
            "charging": "sell_only",
            "note": "合成印花税假设；实际税率存在历史日期变化，不得用于任何历史真实结算",
        },
        {
            "code": "transfer_fee",
            "rate_bp": 1,
            "charging": "both_sides",
            "note": "合成过户费假设",
        },
        {
            "code": "slippage",
            "rate_bp": 10,
            "charging": "adverse_direction_model",
            "note": "合成滑点假设：不利方向万分之10，是成交假设而非行情事实",
        },
    ],
    "fee_accounting": {
        "minimum_charge_once_per_order": True,
        "reserved_fee_release_required": True,
        "note": "最低收费每订单最多计一次；预留费用差额必须释放或补记",
    },
}


def bundle_id_for(kind: str, version: str, content_sha256: str) -> str:
    return "rb_" + sha256_of_object([kind, version, content_sha256])[:16]


def create_rule_bundle(
    conn: sqlite3.Connection,
    *,
    kind: str,
    version: str,
    assumption_scope: str,
    content: Mapping[str, Any],
    notes: str | None = None,
) -> dict[str, Any]:
    """创建（或原样返回既有）不可变规则组合；同 kind+version 内容不同则拒绝。

    调用方负责事务边界；本函数不提交。
    """
    if kind not in BUNDLE_KINDS:
        raise DataError("bundle_kind_unknown", f"规则组合类别非法: {kind}")
    if assumption_scope not in ASSUMPTION_SCOPES:
        raise DataError("bundle_scope_unknown", f"规则组合假设范围非法: {assumption_scope}")
    if kind == "fee" and assumption_scope == "verified_configuration":
        raise DataError(
            "bundle_scope_conflict",
            "费用组合不能在创建时直接登记为 verified_configuration；"
            "真实费率必须经券商配置核验流程另行接入",
        )
    content_text = canonical_json_bytes(dict(content)).decode("utf-8")
    content_sha256 = sha256_of_object(dict(content))
    bundle_id = bundle_id_for(kind, version, content_sha256)
    row = conn.execute(
        "SELECT bundle_id, assumption_scope, content_sha256 FROM rule_bundle WHERE kind=? AND version=?",
        (kind, version),
    ).fetchone()
    if row is not None:
        if str(row[2]) != content_sha256:
            raise DataError(
                "bundle_version_conflict",
                f"规则组合 {kind}/{version} 已存在但内容不一致；不可变组合必须换新版本",
            )
        if str(row[1]) != assumption_scope:
            raise DataError(
                "bundle_scope_conflict",
                f"规则组合 {kind}/{version} 已存在且内容相同，但登记的假设范围是 "
                f"{row[1]}，与本次请求的 {assumption_scope} 不同；"
                "组合的假设范围属于身份，不得在复用时改用不同范围",
            )
        return {
            "bundle_id": str(row[0]),
            "kind": kind,
            "version": version,
            "assumption_scope": str(row[1]),
            "reused": True,
        }
    conn.execute(
        "INSERT INTO rule_bundle(bundle_id, kind, version, assumption_scope, content,"
        " content_sha256, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle_id,
            kind,
            version,
            assumption_scope,
            content_text,
            content_sha256,
            notes,
            utc_now_iso(),
        ),
    )
    return {
        "bundle_id": bundle_id,
        "kind": kind,
        "version": version,
        "assumption_scope": assumption_scope,
        "reused": False,
    }


def load_rule_bundle(conn: sqlite3.Connection, bundle_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT bundle_id, kind, version, assumption_scope, content, content_sha256, notes, created_at"
        " FROM rule_bundle WHERE bundle_id=?",
        (bundle_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row[4])
    except (TypeError, ValueError) as exc:
        raise DataError("bundle_content_invalid", f"规则组合 {bundle_id} 内容不是有效 JSON") from exc
    stored_sha256 = str(row[5])
    recomputed = sha256_of_object(content)
    if recomputed != stored_sha256:
        raise DataError(
            "bundle_content_corrupt",
            f"规则组合 {bundle_id} 内容摘要不一致：存储 {stored_sha256}，"
            f"重算 {recomputed}；拒绝使用未经验证的内容",
        )
    return {
        "bundle_id": row[0],
        "kind": row[1],
        "version": row[2],
        "assumption_scope": row[3],
        "content": content,
        "content_sha256": stored_sha256,
        "notes": row[6],
        "created_at": row[7],
    }


def require_assumption_scope(bundle: Mapping[str, Any], required_scope: str) -> None:
    """显式校验组合假设范围；合成的费用/执行假设不得冒充已核验配置。"""
    if required_scope not in ASSUMPTION_SCOPES:
        raise DataError("bundle_scope_unknown", f"要求的假设范围非法: {required_scope}")
    actual = str(bundle.get("assumption_scope") or "")
    if actual != required_scope:
        raise DataError(
            "bundle_scope_mismatch",
            f"规则组合 {bundle.get('bundle_id')} 的假设范围是 "
            f"{actual or 'unknown'}，不满足 {required_scope}",
        )


def ensure_default_bundles(conn: sqlite3.Connection) -> dict[str, str]:
    """幂等创建默认模拟参数/费用组合；返回 bundle_id。"""
    parameter = create_rule_bundle(
        conn,
        kind="parameter",
        version=DEFAULT_BUNDLE_VERSIONS["parameter"],
        assumption_scope="synthetic_simulation",
        content=DEFAULT_PARAMETER_BUNDLE,
        notes="阶段三 S1 默认模拟参数组合；same_day_reusable 默认值在官方核验前为保守 false",
    )
    fee = create_rule_bundle(
        conn,
        kind="fee",
        version=DEFAULT_BUNDLE_VERSIONS["fee"],
        assumption_scope="synthetic_simulation",
        content=DEFAULT_FEE_BUNDLE,
        notes="阶段三 S1 默认合成费用组合；仅模拟假设，不代表真实市场或券商费率",
    )
    return {"parameter": parameter["bundle_id"], "fee": fee["bundle_id"]}
