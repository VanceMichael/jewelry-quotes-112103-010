"""资料台账：只增不改地保存全部采集版本，支持按时刻回查。

核心约束：
- 记录一旦入账不可修改，复测/调价/活动更新都是新增版本；
- 同一时刻若不同来源（金价流、价签 vs 系统）数值不一致，判定为冲突，
  绝不静默挑选较低值；
- 查询均按「业务时刻」回查当时生效且当时已知的版本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .model import (
    Assay,
    CounterTag,
    Envelope,
    MarketPrice,
    Product,
    Promotion,
    SaleRecord,
    SourceError,
)

PARSERS: dict[str, Callable[[Envelope], Any]] = {
    "market_price": MarketPrice.from_envelope,
    "counter_price": CounterTag.from_envelope,
    "promotion": Promotion.from_envelope,
    "assay": Assay.from_envelope,
    "product": Product.from_envelope,
    "sale": SaleRecord.from_envelope,
}


@dataclass(frozen=True)
class Resolution:
    """单值解析结果：ok / missing / conflict。conflict 时 candidates 保留全部候选。"""

    status: str
    record: Any | None = None
    candidates: tuple = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Ledger:
    """只增不改台账。add 是唯一的写入入口。"""

    def __init__(self) -> None:
        self._records: dict[str, list] = {}
        self._seen_ids: set[str] = set()

    # ---- 写入 ----

    def add(self, envelope: Envelope) -> Any:
        if envelope.source_id in self._seen_ids:
            raise SourceError(f"重复的 source_id: {envelope.source_id}")
        parser = PARSERS.get(envelope.record_kind)
        if parser is None:
            raise SourceError(f"未知记录类型: {envelope.record_kind!r}")
        record = parser(envelope)
        self._seen_ids.add(envelope.source_id)
        self._records.setdefault(envelope.record_kind, []).append(record)
        return record

    def add_file(self, path: str | Path) -> int:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        sources = document.get("sources")
        if not isinstance(sources, list):
            raise SourceError(f"{path}: 缺少 sources 数组")
        for raw in sources:
            self.add(Envelope.from_dict(raw))
        return len(sources)

    # ---- 基础查询 ----

    def records(self, record_kind: str) -> tuple:
        return tuple(self._records.get(record_kind, ()))

    def counts_by_kind(self) -> dict[str, int]:
        return {kind: len(items) for kind, items in sorted(self._records.items())}

    # ---- 金价 ----

    def resolve_market_price(self, moment: datetime, metal: str = "gold") -> Resolution:
        """解析某时刻有效金价。不同金价流同一时刻数值不一致 → conflict。"""
        valid = [
            r
            for r in self.records("market_price")
            if r.metal == metal
            and r.valid_from <= moment
            and (r.valid_until is None or moment <= r.valid_until)
        ]
        if not valid:
            return Resolution("missing", detail=f"{moment.isoformat()} 无生效金价来源（metal={metal}）")
        by_stream: dict[str, MarketPrice] = {}
        for rec in valid:
            current = by_stream.get(rec.market_ref)
            if current is None or (rec.revision, rec.collected_at) > (current.revision, current.collected_at):
                by_stream[rec.market_ref] = rec
        prices = {rec.price_per_gram for rec in by_stream.values()}
        if len(prices) > 1:
            detail = "；".join(
                f"{rec.market_ref} rev{rec.revision}={rec.price_per_gram}元/克" for rec in by_stream.values()
            )
            return Resolution("conflict", candidates=tuple(by_stream.values()), detail=f"金价来源不一致：{detail}")
        chosen = max(by_stream.values(), key=lambda r: (r.revision, r.collected_at))
        return Resolution("ok", record=chosen)

    # ---- 柜台价签 ----

    def resolve_counter_tag(self, store_ref: str, sku_ref: str, moment: datetime) -> Resolution:
        """解析某时刻有效价签。价签与系统版本不一致 → conflict，绝不取较低值。"""
        tags = [
            t
            for t in self.records("counter_price")
            if t.store_ref == store_ref and t.sku_ref == sku_ref
        ]
        if not tags:
            return Resolution("missing", detail=f"{store_ref}/{sku_ref} 未采集到柜台价签")
        valid = [
            t
            for t in tags
            if t.effective_from <= moment and (t.valid_until is None or moment <= t.valid_until)
        ]
        if not valid:
            latest = max(tags, key=lambda t: t.collected_at)
            if latest.effective_from > moment:
                detail = f"{sku_ref} 价签尚未生效（{latest.effective_from.isoformat()} 起生效）"
            else:
                until = latest.valid_until.isoformat() if latest.valid_until else "未知"
                detail = f"{sku_ref} 价签已于 {until} 失效，需重新采集"
            return Resolution("missing", candidates=(latest,), detail=detail)
        by_channel: dict[str, CounterTag] = {}
        for tag in valid:
            current = by_channel.get(tag.channel)
            if current is None or tag.collected_at > current.collected_at:
                by_channel[tag.channel] = tag
        signatures = {(t.making_fee_per_gram, t.discount) for t in by_channel.values()}
        if len(signatures) > 1:
            detail = "；".join(
                f"渠道[{t.channel}] 工费{t.making_fee_per_gram}元/克 折扣{t.discount}"
                for t in by_channel.values()
            )
            return Resolution(
                "conflict",
                candidates=tuple(by_channel.values()),
                detail=f"价签与系统版本不一致：{detail}",
            )
        chosen = max(by_channel.values(), key=lambda t: t.collected_at)
        return Resolution("ok", record=chosen)

    # ---- 活动 ----

    def promotion_head(self, promotion_ref: str, at: datetime | None = None) -> Promotion | None:
        """某活动截至 at 已采集的最新版本。"""
        versions = [
            p
            for p in self.records("promotion")
            if p.promotion_ref == promotion_ref and (at is None or p.collected_at <= at)
        ]
        return max(versions, key=lambda p: p.collected_at) if versions else None

    def active_promotions(self, moment: datetime, knowledge_at: datetime | None = None) -> tuple[Promotion, ...]:
        """moment 生效、且截至 knowledge_at（默认= moment）已知的活动最新版本。"""
        knowledge_at = knowledge_at or moment
        refs = {p.promotion_ref for p in self.records("promotion")}
        active = []
        for ref in sorted(refs):
            head = self.promotion_head(ref, at=knowledge_at)
            if head and head.starts_at <= moment <= head.ends_at:
                active.append(head)
        return tuple(active)

    # ---- 旧金检测 ----

    def assay_history(self, item_ref: str) -> tuple[Assay, ...]:
        history = [a for a in self.records("assay") if a.item_ref == item_ref]
        return tuple(sorted(history, key=lambda a: (a.tested_at, a.revision)))

    def assay_for(self, item_ref: str, at: datetime) -> Assay | None:
        """某饰品在 at 时点有效的检测版本（检测时间 <= at 的最新一版）。"""
        history = [a for a in self.assay_history(item_ref) if a.tested_at <= at]
        return history[-1] if history else None

    # ---- 商品 / 成交 ----

    def product(self, sku_ref: str, at: datetime | None = None) -> Product | None:
        items = [
            p
            for p in self.records("product")
            if p.sku_ref == sku_ref and (at is None or p.collected_at <= at)
        ]
        return max(items, key=lambda p: p.collected_at) if items else None

    def sale_by_ref(self, sale_ref: str) -> SaleRecord | None:
        for sale in self.records("sale"):
            if sale.sale_ref == sale_ref:
                return sale
        return None

    def sales_between(self, start: datetime, end: datetime) -> tuple[SaleRecord, ...]:
        return tuple(
            s
            for s in self.records("sale")
            if s.confirmed_at is not None and start <= s.confirmed_at <= end
        )
