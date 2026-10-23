"""阶段三最小数据服务适配（S1）：业务请求 ↔ 数据上下文绑定。

每个阶段三业务请求**必须**显式绑定完整上下文：``source + data_version_id +
as_of + purpose + account_revision + rule_bundle_id``（可选股票池快照）。缺少
任一项既不允许绑定，更不允许退化成"从当前库最新数据"猜测出的上下文。本模块
只做只读绑定与验证，不选候选、不产生建议或成交：

- 复用阶段二 ``VersionView``（固定版本读取、来源绑定、归档拒绝）、
  用途资格（``check_eligibility``）与股票池快照读取，不另写一套 SQL；
- 来源与版本不一致、版本缺失/已归档、规则组合未知、股票池快照指向其他数据
  版本或 as_of 不一致时，直接拒绝（``DataError``），不做静默降级；
- 真实模式（signal_input 等需要选股的用途）缺少必要的市场范围/风险状态证据
  （显式登记核验的合格股票池快照，且快照不携带未决的幸存者偏差/历史全集缺失
  旗标）时显式受限，绝不从未经核验的证券全集挑选候选；也绝不从证券代码前缀
  推断板块或 ST 状态（S1 只有"有/无证据"两种状态）；
- 陈旧/缺失价格按阶段二资格接口的稳定原因码带标签返回，不静默降级；
- 受限元数据（代码+详情）显式持久可用，供建议快照的 restriction 字段使用。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..data.chunks import VersionView
from ..data.eligibility import EligibilityResult, check_eligibility
from ..data.digests import hash_bytes
from ..data.errors import DataError
from ..data.prices import PRICE_PURPOSES
from ..data.timeutil import require_yyyymmdd
from ..data.universe import load_pool_snapshot
from .rule_bundles import load_rule_bundle

__all__ = [
    "DataSelectionContext",
    "BoundDataContext",
    "bind_data_context",
    "UNRESOLVED_SCOPE_FLAGS",
    "register_pool_scope_evidence",
    "load_pool_scope_evidence",
    "SIGNAL_INPUT_PURPOSE",
    "EXECUTION_REFERENCE_PURPOSE",
    "VALUATION_REFERENCE_PURPOSE",
    "RESTRICTION_POOL_VERSION_MISMATCH",
]

SIGNAL_INPUT_PURPOSE = "signal_input"
EXECUTION_REFERENCE_PURPOSE = "execution_reference"
VALUATION_REFERENCE_PURPOSE = "valuation_reference"

# 需要合格股票池证据才能进行的用途；缺失证据时只能受限，不得进入候选选择。
_REQUIRES_UNIVERSE_EVIDENCE = (SIGNAL_INPUT_PURPOSE,)

RESTRICTION_UNIVERSE_UNVERIFIED = "universe_unverified"
RESTRICTION_DATA_NOT_ELIGIBLE = "data_not_eligible_for_purpose"
# DataError reason 码：快照与上下文版本/时点不一致 → 直接拒绝，而不是受限放行。
RESTRICTION_POOL_VERSION_MISMATCH = "pool_snapshot_version_mismatch"

# 阶段二股票池生成器目前总是输出的未决范围旗标：历史全集不完整、幸存者偏差
# 未消除。携带任一旗标的快照不构成"市场范围与风险状态已核验"的证据。
UNRESOLVED_SCOPE_FLAGS: frozenset[str] = frozenset(
    {"historical_universe_incomplete", "survivorship_bias_unresolved"}
)


@dataclass(frozen=True)
class DataSelectionContext:
    """阶段三业务上下文（不可变；记录到建议/模拟主记录的引用字段）。

    ``data_version_id``、``account_revision``、``rule_bundle_id`` 是必填项：
    阶段三契约要求显式的来源、版本、时点、用途、账户修订与规则组合。
    """

    source_code: str
    as_of: str
    purpose: str
    data_version_id: str
    account_revision: int
    rule_bundle_id: str
    pool_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not self.data_version_id or not isinstance(self.data_version_id, str):
            raise ValueError("data_version_id 必须是非空字符串（阶段三上下文必填）")
        if not isinstance(self.account_revision, int) or isinstance(self.account_revision, bool) or self.account_revision < 1:
            raise ValueError("account_revision 必须是正整数（阶段三上下文必填）")
        if not self.rule_bundle_id or not isinstance(self.rule_bundle_id, str):
            raise ValueError("rule_bundle_id 必须是非空字符串（阶段三上下文必填）")
        if not self.source_code or not isinstance(self.source_code, str):
            raise ValueError("source_code 必须是非空字符串")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_code": self.source_code,
            "as_of": self.as_of,
            "purpose": self.purpose,
            "data_version_id": self.data_version_id,
            "pool_snapshot_id": self.pool_snapshot_id,
            "account_revision": self.account_revision,
            "rule_bundle_id": self.rule_bundle_id,
        }


@dataclass(frozen=True)
class BoundDataContext:
    """绑定结果：只读视图 + 显式资格/受限元数据。"""

    context: DataSelectionContext
    view: VersionView | None
    eligibility: EligibilityResult | None
    restricted: bool
    restriction_code: str | None
    restriction_detail: str | None
    limits: list[str] = field(default_factory=list)
    latest_data_date: str | None = None
    risk_status: dict[str, Any] = field(default_factory=dict)
    rule_bundle: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context.as_dict(),
            "restricted": self.restricted,
            "restriction_code": self.restriction_code,
            "restriction_detail": self.restriction_detail,
            "eligibility": self.eligibility.as_dict() if self.eligibility is not None else None,
            "latest_data_date": self.latest_data_date,
            "limits": list(self.limits),
            "risk_status": dict(self.risk_status),
        }


def register_pool_scope_evidence(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    verification_source: str,
    verified_by: str,
    evidence_payload: str,
    evidence_sha256: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """登记一次显式的市场范围/风险状态核验记录（阶段三证据表）。

    两个核验位（板块/上市范围、风险状态）都必须为真才能登记成功。登记必须附
    带可独立复核的凭据：

    - ``verification_source``：证据来源说明（谁的文件/哪份配置，可供人工复核）；
    - ``verified_by``：登记责任方；
    - ``evidence_payload``：原样保存的证据材料（文本/JSON），读取时重算摘要核对，
      任何篡改都会使读取显式失败（``evidence_corrupt``）；
    - ``evidence_sha256``：证据摘要；省略时按 payload 现算，但显式传入时必须一致。

    S1 没有真实核验工作流：登记状态只能是 ``pending_external_review``，
    **不构成**"市场规则已核验"的声明，也不据此解除任何限制。解除 signal_input
    限制需要工作流显式写 ``confirmed_by_workflow``（schema CHECK 要求同时登记
    confirmed_at/confirmation_source）——S1 不提供该写入路径。
    """
    row = conn.execute(
        "SELECT snapshot_id, as_of, data_version_id FROM pool_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise DataError("pool_snapshot_not_found", f"股票池快照 {snapshot_id} 不存在")
    source = (verification_source or "").strip()
    by = (verified_by or "").strip()
    if not source:
        raise DataError(
            "pool_scope_evidence_provenance_required",
            "登记核验证据必须给出 verification_source（证据来源说明）",
        )
    if not by:
        raise DataError(
            "pool_scope_evidence_provenance_required",
            "登记核验证据必须给出 verified_by（登记责任方）",
        )
    if not evidence_payload or not evidence_payload.strip():
        raise DataError(
            "pool_scope_evidence_provenance_required",
            "登记核验证据必须原样保存证据材料 evidence_payload（非空）",
        )
    computed = hash_bytes(evidence_payload.encode("utf-8"))
    if evidence_sha256 is not None:
        claimed = evidence_sha256.strip().lower()
        if claimed != computed:
            raise DataError(
                "pool_scope_evidence_digest_mismatch",
                f"证据摘要不一致：声明 {claimed}，按 payload 重算 {computed}",
            )
    conn.execute(
        "INSERT OR REPLACE INTO pool_scope_evidence"
        "(snapshot_id, exchange_board_verified, risk_status_verified, verification_source,"
        " verified_by, evidence_payload, evidence_sha256, verification_status,"
        " confirmed_at, confirmation_source, notes, registered_at)"
        " VALUES (?, 1, 1, ?, ?, ?, ?, 'pending_external_review', NULL, NULL,"
        " ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (snapshot_id, source, by, evidence_payload, computed, notes),
    )
    return {
        "snapshot_id": snapshot_id,
        "registered": True,
        "verification_status": "pending_external_review",
        "evidence_sha256": computed,
    }


def load_pool_scope_evidence(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any] | None:
    """读取核验证据；重算证据摘要，篡改的材料显式失败而不是静默采信。"""
    row = conn.execute(
        "SELECT snapshot_id, exchange_board_verified, risk_status_verified, verification_source,"
        " verified_by, evidence_payload, evidence_sha256, verification_status,"
        " confirmed_at, confirmation_source, notes, registered_at"
        " FROM pool_scope_evidence WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    computed = hash_bytes(str(row[5]).encode("utf-8"))
    if computed != str(row[6]):
        raise DataError(
            "evidence_corrupt",
            f"股票池范围证据 {snapshot_id} 的 evidence_sha256 与保存材料不一致："
            f"存储 {row[6]}，重算 {computed}；拒绝采信",
        )
    return {
        "snapshot_id": row[0],
        "exchange_board_verified": bool(row[1]),
        "risk_status_verified": bool(row[2]),
        "verification_source": row[3],
        "verified_by": row[4],
        "evidence_payload": row[5],
        "evidence_sha256": row[6],
        "verification_status": row[7],
        "confirmed_at": row[8],
        "confirmation_source": row[9],
        "notes": row[10],
        "registered_at": row[11],
    }


def _to_restriction(reason_code: str, reasons: list[str]) -> tuple[str, str]:
    return reason_code, "；".join(reasons) if reasons else reason_code


def bind_data_context(conn: sqlite3.Connection, context: DataSelectionContext) -> BoundDataContext:
    """绑定（校验）一个阶段三数据上下文；只读，不修改数据库。"""
    if context.purpose not in PRICE_PURPOSES:
        raise DataError("unknown_data_purpose", f"未知数据用途: {context.purpose}")
    as_of = require_yyyymmdd(context.as_of)
    validated = DataSelectionContext(
        source_code=context.source_code,
        as_of=as_of,
        purpose=context.purpose,
        data_version_id=context.data_version_id,
        account_revision=context.account_revision,
        rule_bundle_id=context.rule_bundle_id,
        pool_snapshot_id=context.pool_snapshot_id,
    )

    # 规则组合必须真实存在；未知/无效组合直接拒绝，而不是匿名放行。
    rule_bundle = load_rule_bundle(conn, validated.rule_bundle_id)
    if rule_bundle is None:
        raise DataError(
            "rule_bundle_not_found",
            f"规则组合 {validated.rule_bundle_id} 不存在；上下文的 rule_bundle_id 必须先登记",
        )

    # 固定版本读取：VersionView 内部拒绝版本缺失 / 归档离线 / 来源与声明不一致。
    view = VersionView(conn, validated.data_version_id, expected_source_code=validated.source_code)

    restricted = False
    restriction_code: str | None = None
    restriction_detail: str | None = None

    pool_meta: dict[str, Any] | None = None
    risk_status: dict[str, Any] = {}
    if validated.pool_snapshot_id:
        pool_meta = load_pool_snapshot(conn, validated.pool_snapshot_id)
        if pool_meta is None:
            raise DataError("pool_snapshot_not_found", f"股票池快照 {validated.pool_snapshot_id} 不存在")
        if pool_meta["data_version_id"] != validated.data_version_id:
            raise DataError(
                "pool_snapshot_version_mismatch",
                f"股票池快照 {validated.pool_snapshot_id} 基于 {pool_meta['data_version_id']}，"
                f"与上下文 {validated.data_version_id} 不一致；该快照不得用于本次绑定",
            )
        if pool_meta["as_of"] != validated.as_of:
            raise DataError(
                "pool_snapshot_as_of_mismatch",
                f"股票池快照 {validated.pool_snapshot_id} 的时点是 {pool_meta['as_of']}，"
                f"与上下文 as_of={validated.as_of} 不一致；该快照不得用于本次绑定",
            )
        risk_status = {
            "pool_snapshot_id": pool_meta["snapshot_id"],
            "pool_data_version_id": pool_meta["data_version_id"],
            "member_count": pool_meta["member_count"],
            "limit_flags": list(pool_meta["limit_flags"]),
            "as_of": pool_meta["as_of"],
        }

    if validated.purpose in _REQUIRES_UNIVERSE_EVIDENCE and not restricted:
        if pool_meta is None:
            restricted = True
            restriction_code = RESTRICTION_UNIVERSE_UNVERIFIED
            restriction_detail = (
                "真实模式缺少已核验的市场范围与风险状态证据（合格股票池快照）；"
                "不得从未经核验的证券全集选择候选"
            )
        else:
            unresolved_flags = [
                flag
                for flag in pool_meta["limit_flags"]
                if flag in UNRESOLVED_SCOPE_FLAGS
            ]
            evidence = load_pool_scope_evidence(conn, validated.pool_snapshot_id)
            risk_status["unresolved_scope_flags"] = unresolved_flags
            risk_status["scope_evidence_registered"] = evidence is not None
            risk_status["scope_evidence_status"] = (
                evidence["verification_status"] if evidence is not None else None
            )
            risk_status["survivorship_bias_unresolved"] = (
                "survivorship_bias_unresolved" in unresolved_flags
            )
            if unresolved_flags:
                restricted = True
                restriction_code = RESTRICTION_UNIVERSE_UNVERIFIED
                restriction_detail = (
                    "股票池快照仍携带未决的范围旗标（"
                    + "、".join(unresolved_flags)
                    + "）；不能声称幸存者偏差已消除或历史全集完整，"
                    "禁止用于候选选择"
                )
            elif evidence is None:
                restricted = True
                restriction_code = RESTRICTION_UNIVERSE_UNVERIFIED
                restriction_detail = (
                    "股票池快照缺少显式登记的市场范围与风险状态核验证据；"
                    "禁止用于候选选择"
                )
            elif evidence["verification_status"] != "confirmed_by_workflow":
                # 登记凭据不等于核验结论：S1 没有真实核验工作流，
                # pending_external_review 一律不能解除候选选择的限制。
                restricted = True
                restriction_code = RESTRICTION_UNIVERSE_UNVERIFIED
                restriction_detail = (
                    "市场范围与风险状态核验证据仍处于 pending_external_review；"
                    "确认后的核验工作流尚未存在（S1），禁止用于候选选择"
                )

    eligibility: EligibilityResult | None = check_eligibility(
        conn,
        purpose=validated.purpose,
        as_of=as_of,
        source_code=validated.source_code,
        data_version_id=validated.data_version_id,
        business_date=validated.as_of,
    )
    latest_data_date = eligibility.latest_data_date
    limits = list(eligibility.source_limits)

    if not restricted and not eligibility.eligible:
        restricted = True
        restriction_code, restriction_detail = _to_restriction(
            f"{RESTRICTION_DATA_NOT_ELIGIBLE}:{eligibility.reason_code}",
            eligibility.reasons,
        )

    return BoundDataContext(
        context=validated,
        view=view,
        eligibility=eligibility,
        restricted=restricted,
        restriction_code=restriction_code,
        restriction_detail=restriction_detail,
        limits=limits,
        latest_data_date=latest_data_date,
        risk_status=risk_status,
        rule_bundle=rule_bundle,
    )
