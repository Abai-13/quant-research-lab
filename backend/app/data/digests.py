"""规范化内容摘要与清单哈希。

- 业务记录的内容摘要覆盖规范化后的全部业务字段（不含修订号、时间戳），用于判断
  修订：内容一致则重复导入不新增修订；
- 数据版本清单摘要按稳定序列化生成：相同键值集合必然得到相同摘要。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def canonical_json_bytes(obj: Any) -> bytes:
    """构造与键序无关的稳定序列化结果。"""

    def _to_json(value: Any) -> Any:
        if isinstance(value, Decimal):
            return format(value, "f")
        raise TypeError(f"无法序列化清单对象: {type(value)!r}")

    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_to_json,
    ).encode("utf-8")


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_of_object(obj: Any) -> str:
    return hash_bytes(canonical_json_bytes(obj))


def content_hash(payload_dict: dict) -> str:
    """业务内容的哈希；调用方传入不含修订元信息的字段字典。"""
    return sha256_of_object(payload_dict)


def short_digest(hex_digest: str, length: int = 16) -> str:
    return hex_digest[:length]


def version_id_from(digest: str) -> str:
    return "dv_" + digest[:16]


def snapshot_id_from(digest: str) -> str:
    return "pool_" + digest[:16]


__all__ = [
    "canonical_json_bytes",
    "hash_bytes",
    "sha256_of_object",
    "content_hash",
    "short_digest",
    "version_id_from",
    "snapshot_id_from",
]
