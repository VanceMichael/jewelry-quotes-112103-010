"""来源记录模型：把采集信封解析为不可变的领域记录。

每条记录都保留 source_id 与 collected_at，报价依据可回查到具体采集版本。
全部数据为虚构合成数据。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class SourceError(ValueError):
    """来源资料不符合约定格式。"""


def _require(payload: Mapping[str, Any], field: str, kind: str) -> Any:
    if field not in payload or payload[field] is None:
        raise SourceError(f"{kind} 缺少字段 {field!r}")
    return payload[field]


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"时间字段 {field!r} 必须是 ISO 字符串，得到 {value!r}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(f"时间字段 {field!r} 无法解析: {value!r}") from exc
    if moment.tzinfo is None:
        raise SourceError(f"时间字段 {field!r} 必须带时区: {value!r}")
    return moment


def parse_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SourceError(f"数值字段 {field!r} 无法解析: {value!r}") from exc


def parse_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceError(f"整数字段 {field!r} 无法解析: {value!r}")
    return value


@dataclass(frozen=True)
class Envelope:
    """采集信封：source_id / record_kind / collected_at / payload。"""

    source_id: str
    record_kind: str
    collected_at: datetime
    payload: dict

    @classmethod
    def from_dict(cls, raw: Any) -> "Envelope":
        if not isinstance(raw, Mapping):
            raise SourceError(f"来源记录必须是对象，得到 {type(raw).__name__}")
        source_id = _require(raw, "source_id", "source")
        record_kind = _require(raw, "record_kind", "source")
        collected_at = parse_time(_require(raw, "collected_at", "source"), "collected_at")
        payload = _require(raw, "payload", "source")
        if not isinstance(raw["payload"], Mapping):
            raise SourceError("payload 必须是对象")
        return cls(str(source_id), str(record_kind), collected_at, dict(payload))


@dataclass(frozen=True)
class MarketPrice:
    """市场金价（按生效时间起算，revision 递增）。"""

    source_id: str
    collected_at: datetime
    market_ref: str
    metal: str
    price_per_gram: Decimal
    valid_from: datetime
    revision: int
    valid_until: datetime | None

    @classmethod
    def from_envelope(cls, env: Envelope) -> "MarketPrice":
        p = env.payload
        valid_until = p.get("valid_until")
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            market_ref=str(_require(p, "market_ref", env.record_kind)),
            metal=str(_require(p, "metal", env.record_kind)),
            price_per_gram=parse_decimal(_require(p, "price_per_gram_yuan", env.record_kind), "price_per_gram_yuan"),
            valid_from=parse_time(_require(p, "valid_from", env.record_kind), "valid_from"),
            revision=parse_int(_require(p, "revision", env.record_kind), "revision"),
            valid_until=parse_time(valid_until, "valid_until") if valid_until else None,
        )


@dataclass(frozen=True)
class CounterTag:
    """柜台价签/系统价：工费与折扣，channel 区分「价签」与「系统」。"""

    source_id: str
    collected_at: datetime
    store_ref: str
    sku_ref: str
    making_fee_per_gram: Decimal
    discount: Decimal
    channel: str
    valid_from: datetime | None
    valid_until: datetime | None

    @property
    def effective_from(self) -> datetime:
        """价签生效起点：无显式 valid_from 时以采集时间为准。"""
        return self.valid_from or self.collected_at

    @classmethod
    def from_envelope(cls, env: Envelope) -> "CounterTag":
        p = env.payload
        valid_from = p.get("valid_from")
        valid_until = p.get("valid_until")
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            store_ref=str(_require(p, "store_ref", env.record_kind)),
            sku_ref=str(_require(p, "sku_ref", env.record_kind)),
            making_fee_per_gram=parse_decimal(
                _require(p, "making_fee_per_gram_yuan", env.record_kind), "making_fee_per_gram_yuan"
            ),
            discount=parse_decimal(_require(p, "discount", env.record_kind), "discount"),
            channel=str(p.get("channel", "system")),
            valid_from=parse_time(valid_from, "valid_from") if valid_from else None,
            valid_until=parse_time(valid_until, "valid_until") if valid_until else None,
        )


@dataclass(frozen=True)
class Promotion:
    """品牌活动：生效时段 + 可否叠加规则；同活动更新即新版本。"""

    source_id: str
    collected_at: datetime
    promotion_ref: str
    starts_at: datetime
    ends_at: datetime
    stackable_with_member: bool
    stackable_with_exchange: bool
    discount: Decimal | None

    @classmethod
    def from_envelope(cls, env: Envelope) -> "Promotion":
        p = env.payload
        discount = p.get("discount")
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            promotion_ref=str(_require(p, "promotion_ref", env.record_kind)),
            starts_at=parse_time(_require(p, "starts_at", env.record_kind), "starts_at"),
            ends_at=parse_time(_require(p, "ends_at", env.record_kind), "ends_at"),
            stackable_with_member=bool(p.get("stackable_with_member", False)),
            stackable_with_exchange=bool(p.get("stackable_with_exchange", False)),
            discount=parse_decimal(discount, "discount") if discount is not None else None,
        )


@dataclass(frozen=True)
class Assay:
    """旧金检测：只适用于对应饰品与检测时间；复测产生新 revision，不覆盖旧版本。"""

    source_id: str
    collected_at: datetime
    assay_ref: str
    item_ref: str
    weight_gram: Decimal
    purity_per_mille: int
    tested_at: datetime
    revision: int

    @classmethod
    def from_envelope(cls, env: Envelope) -> "Assay":
        p = env.payload
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            assay_ref=str(_require(p, "assay_ref", env.record_kind)),
            item_ref=str(_require(p, "item_ref", env.record_kind)),
            weight_gram=parse_decimal(_require(p, "weight_gram", env.record_kind), "weight_gram"),
            purity_per_mille=parse_int(_require(p, "purity_per_mille", env.record_kind), "purity_per_mille"),
            tested_at=parse_time(_require(p, "tested_at", env.record_kind), "tested_at"),
            revision=parse_int(_require(p, "revision", env.record_kind), "revision"),
        )


@dataclass(frozen=True)
class Product:
    """商品档案（合成）：默认克重等。"""

    source_id: str
    collected_at: datetime
    sku_ref: str
    name: str
    default_weight_gram: Decimal | None

    @classmethod
    def from_envelope(cls, env: Envelope) -> "Product":
        p = env.payload
        weight = p.get("default_weight_gram")
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            sku_ref=str(_require(p, "sku_ref", env.record_kind)),
            name=str(p.get("name", "")),
            default_weight_gram=parse_decimal(weight, "default_weight_gram") if weight is not None else None,
        )


@dataclass(frozen=True)
class SaleRecord:
    """柜台系统成交记录（用于与工作台确认快照互相核对）。"""

    source_id: str
    collected_at: datetime
    sale_ref: str
    sku_ref: str
    state: str
    quote_revision: int
    assay_ref: str | None
    inquiry_ref: str | None
    customer_ref: str | None
    confirmed_at: datetime | None

    @classmethod
    def from_envelope(cls, env: Envelope) -> "SaleRecord":
        p = env.payload
        confirmed_at = p.get("confirmed_at")
        assay_ref = p.get("assay_ref")
        inquiry_ref = p.get("inquiry_ref")
        customer_ref = p.get("customer_ref")
        return cls(
            source_id=env.source_id,
            collected_at=env.collected_at,
            sale_ref=str(_require(p, "sale_ref", env.record_kind)),
            sku_ref=str(_require(p, "sku_ref", env.record_kind)),
            state=str(_require(p, "state", env.record_kind)),
            quote_revision=parse_int(_require(p, "quote_revision", env.record_kind), "quote_revision"),
            assay_ref=str(assay_ref) if assay_ref else None,
            inquiry_ref=str(inquiry_ref) if inquiry_ref else None,
            customer_ref=str(customer_ref) if customer_ref else None,
            confirmed_at=parse_time(confirmed_at, "confirmed_at") if confirmed_at else None,
        )
