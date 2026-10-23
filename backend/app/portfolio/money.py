"""金额与股数边界：整数分（cents）与整数股。

阶段三边界契约（S0 冻结基础，派工决定）：

- 金额、费用与账面成本一律使用整数最小货币单位（人民币分）或 ``Decimal``，
  不用二进制浮点累计现金；
- 股票数量为非负整数股；不存在 0.01 股；
- JSON 精确金额采用十进制字符串；行情侧既有浮点存储不在本边界内重写，
  转换只发生在服务边界并按明示规则（``ROUND_HALF_UP`` 到分）进行；
- 未知金额保持 ``None``，绝不用 0 冒充已知。

直接拒绝 ``float`` 输入：调用方必须先显式决定二进制浮点值到分的舍入规则，
再以 ``str``/``Decimal``/``int`` 进入本模块，避免无意中传播二进制误差。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..data.errors import DataError

__all__ = [
    "MONEY_UNIT_VERSION",
    "parse_money_to_cents",
    "cents_to_decimal_string",
    "parse_shares",
    "derive_per_share_cost",
]

# 舍入模式与最小货币单位作为版本化契约记录；规则变更必须换版本。
MONEY_UNIT_VERSION = "money-unit-1.0"

_ZERO = Decimal("0")


def parse_money_to_cents(value: int | str | Decimal, *, field: str = "amount") -> int:
    """把十进制金额解析为整数分；拒绝二进制浮点与非法输入。

    ``value`` 必须是 ``int``（视为整数分）、十进制字符串或 ``Decimal``；
    ``float`` 被显式拒绝（服务边界先把行情浮点按 MONEY_UNIT_VERSION 规则转换）。
    字符串与 ``Decimal`` 走 ROUND_HALF_UP 到分的明示转换。
    """
    if isinstance(value, float) or isinstance(value, bool):
        raise DataError(
            "money_boundary_unit_error",
            f"金额 {field} 不能用二进制浮点/布尔输入；请先按 {MONEY_UNIT_VERSION} 规则"
            "把行情浮点转换为 Decimal/字符串再进入账目边界",
        )
    if not isinstance(value, (int, str, Decimal)):
        raise DataError("money_boundary_unit_error", f"金额 {field} 类型非法: {type(value)!r}")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise DataError("money_boundary_unit_error", f"金额 {field} 不能为空字符串")
        if any(ch not in "+-0123456789." for ch in text):
            raise DataError(
                "money_boundary_unit_error",
                f"金额 {field} 必须是十进制字符串（含可选正负号），"
                f"不接受科学计数法/分隔符/空白: {value!r}",
            )
        try:
            dec = Decimal(text)
        except ArithmeticError as exc:
            raise DataError(
                "money_boundary_unit_error", f"金额 {field} 不是有效十进制数: {value!r}"
            ) from exc
    else:
        dec = value
    cents = (dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def cents_to_decimal_string(cents: int | None) -> str | None:
    """把整数字分序列化为十进制字符串（JSON 精确金额）；未知保持 None。"""
    if cents is None:
        return None
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise DataError("money_boundary_unit_error", "整数分金额必须是 int")
    return _format_cents(cents)


def _format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    absolute = abs(int(cents))
    yuan, fen = divmod(absolute, 100)
    return f"{sign}{yuan}.{fen:02d}"


def parse_shares(value: int | str, *, field: str = "shares") -> int:
    """解析并校验股数：非负整数股；拒绝浮点、布尔与小数点文本。

    允许 ``0``（合法的空持仓）；增减量（delta）由调用方使用带符号整数并在业务层另行校验。
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise DataError(
            "shares_boundary_unit_error",
            f"股数 {field} 必须以整数（int/整数字符串）输入；不接受浮点/布尔",
        )
    if not isinstance(value, (int, str)):
        raise DataError("shares_boundary_unit_error", f"股数 {field} 类型非法: {type(value)!r}")
    text = str(value).strip()
    if not text.isdigit():
        raise DataError(
            "shares_boundary_unit_error",
            f"股数 {field} 必须是非负整数字符串，当前值 {value!r}",
        )
    return int(text)


def derive_per_share_cost(total_cost_cents: int | None, shares: int) -> Decimal | None:
    """由权威的持仓总成本（整数分）派生每股成本（Decimal 分）。

    换手/补仓后每股成本可能是非整数分，库里只保存总成本；这里做无损除法，
    绝不把近似每股成本写回存储。``total_cost_cents`` 为 None（未知）或 ``shares``
    为 0 时返回 None。``total_cost_cents`` 为负或 ``shares`` 为负也返回 None——
    这种输入只会来自被 schema CHECK 拒绝的坏行，派生函数不猜测含义。
    """
    if total_cost_cents is None or shares is None:
        return None
    if not isinstance(total_cost_cents, int) or isinstance(total_cost_cents, bool):
        raise DataError("money_boundary_unit_error", "总成本必须是整数分")
    if not isinstance(shares, int) or isinstance(shares, bool):
        raise DataError("shares_boundary_unit_error", "股数必须是整数")
    if shares <= 0 or total_cost_cents < 0:
        return None
    return Decimal(total_cost_cents) / Decimal(shares)
