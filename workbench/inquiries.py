"""询价会话与成交确认。

- 每次报价生成一个报价版本（revision），询价历史全部保留；
- 成交（收款 paid / 留货 held）时保存确认快照，之后一律按确认记录解释，
  复测、调价、活动更新都不回写已成交订单；
- 未成交的报价在依据更新（活动、金价、价签、复测）后需重新核对。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime

from .ledger import Ledger
from .quoting import Quote, QuoteEngine, QuoteRequest


class WorkbenchError(RuntimeError):
    """工作台操作错误。"""


@dataclass(frozen=True)
class Confirmation:
    """成交确认快照：报价与依据的不可变副本。"""

    sale_ref: str
    inquiry_ref: str
    quote_revision: int
    state: str  # "paid" | "held"
    confirmed_at: datetime
    quote: Quote


@dataclass(frozen=True)
class RecheckResult:
    """核对结果：locked=已成交按确认记录解释；current=依据未变；recheck_needed=需重新核对。"""

    status: str
    changes: tuple[str, ...] = ()


@dataclass
class Inquiry:
    request: QuoteRequest
    quotes: list[Quote] = field(default_factory=list)
    confirmation: Confirmation | None = None

    @property
    def latest_quote(self) -> Quote | None:
        return self.quotes[-1] if self.quotes else None


class InquiryBook:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.engine = QuoteEngine(ledger)
        self._inquiries: dict[str, Inquiry] = {}
        self._confirmations: dict[str, Confirmation] = {}

    # ---- 询价与报价 ----

    def open(self, request: QuoteRequest) -> Quote:
        if request.inquiry_ref in self._inquiries:
            raise WorkbenchError(f"询价单已存在: {request.inquiry_ref}")
        inquiry = Inquiry(request=request)
        self._inquiries[request.inquiry_ref] = inquiry
        quote = self.engine.build(request, revision=1)
        inquiry.quotes.append(quote)
        return quote

    def requote(self, inquiry_ref: str, quoted_at: datetime) -> Quote:
        """依据更新后重新报价，生成新的报价版本。"""
        inquiry = self.get(inquiry_ref)
        if inquiry.confirmation is not None:
            raise WorkbenchError(f"{inquiry_ref} 已成交（{inquiry.confirmation.sale_ref}），按确认记录解释，不能重报")
        request = replace(inquiry.request, quoted_at=quoted_at)
        inquiry.request = request
        quote = self.engine.build(request, revision=len(inquiry.quotes) + 1)
        inquiry.quotes.append(quote)
        return quote

    def get(self, inquiry_ref: str) -> Inquiry:
        try:
            return self._inquiries[inquiry_ref]
        except KeyError:
            raise WorkbenchError(f"未知询价单: {inquiry_ref}") from None

    # ---- 成交确认 ----

    def confirm(self, inquiry_ref: str, sale_ref: str, state: str, confirmed_at: datetime) -> Confirmation:
        if state not in ("paid", "held"):
            raise WorkbenchError(f"未知成交状态: {state!r}（应为 paid 或 held）")
        inquiry = self.get(inquiry_ref)
        if inquiry.confirmation is not None:
            raise WorkbenchError(f"{inquiry_ref} 已成交，不能重复确认")
        quote = inquiry.latest_quote
        if quote is None or quote.status != "ready":
            raise WorkbenchError(f"{inquiry_ref} 报价存在未解决事项，不能成交")
        confirmation = Confirmation(
            sale_ref=sale_ref,
            inquiry_ref=inquiry_ref,
            quote_revision=quote.revision,
            state=state,
            confirmed_at=confirmed_at,
            quote=copy.deepcopy(quote),
        )
        inquiry.confirmation = confirmation
        self._confirmations[sale_ref] = confirmation
        return confirmation

    def confirmation(self, sale_ref: str) -> Confirmation | None:
        return self._confirmations.get(sale_ref)

    def confirmations_between(self, start: datetime, end: datetime) -> tuple[Confirmation, ...]:
        return tuple(
            sorted(
                (c for c in self._confirmations.values() if start <= c.confirmed_at <= end),
                key=lambda c: c.confirmed_at,
            )
        )

    # ---- 事后核对 ----

    def recheck(self, inquiry_ref: str, check_time: datetime) -> RecheckResult:
        """对照台账当前版本核对最新报价的依据是否仍然有效。"""
        inquiry = self.get(inquiry_ref)
        if inquiry.confirmation is not None:
            return RecheckResult("locked")
        quote = inquiry.latest_quote
        if quote is None:
            return RecheckResult("current")
        basis = quote.basis
        request = inquiry.request
        changes: list[str] = []

        if basis.market_price is not None:
            current = self.ledger.resolve_market_price(check_time)
            if current.status == "conflict":
                changes.append(f"金价来源出现冲突：{current.detail}")
            elif current.status == "missing":
                changes.append("当前无生效金价来源")
            elif current.record.source_id != basis.market_price.source_id:
                changes.append(
                    f"金价已更新：报价依据 rev{basis.market_price.revision}"
                    f"（{basis.market_price.price_per_gram}元/克），当前 rev{current.record.revision}"
                    f"（{current.record.price_per_gram}元/克）"
                )

        if basis.counter_tag is not None:
            current = self.ledger.resolve_counter_tag(request.store_ref, request.sku_ref, check_time)
            if current.status == "conflict":
                changes.append(f"价签与系统出现冲突：{current.detail}")
            elif current.status == "missing":
                changes.append(f"价签已失效：{current.detail}")
            elif current.record.source_id != basis.counter_tag.source_id:
                changes.append(f"价签已更新：报价依据 {basis.counter_tag.source_id}，当前 {current.record.source_id}")

        if basis.promotion is not None:
            head = self.ledger.promotion_head(basis.promotion.promotion_ref, at=check_time)
            if head is None:
                changes.append(f"活动 {basis.promotion.promotion_ref} 已撤回")
            elif head.source_id != basis.promotion.source_id:
                changes.append(
                    f"活动 {basis.promotion.promotion_ref} 已更新：报价依据采集于"
                    f" {basis.promotion.collected_at.isoformat()}，新版本采集于 {head.collected_at.isoformat()}"
                )

        if basis.assay is not None:
            current = self.ledger.assay_for(basis.assay.item_ref, check_time)
            if current is not None and current.source_id != basis.assay.source_id:
                changes.append(
                    f"旧金 {basis.assay.item_ref} 已复测：报价依据 {basis.assay.assay_ref}"
                    f" rev{basis.assay.revision}，最新 rev{current.revision}"
                )

        return RecheckResult("recheck_needed" if changes else "current", tuple(changes))

    # ---- 销售待办 ----

    def checklist(self, inquiry_ref: str, as_of: datetime) -> list[str]:
        """销售人员视角：该询价还缺哪项确认。"""
        inquiry = self.get(inquiry_ref)
        if inquiry.confirmation is not None:
            return []
        quote = inquiry.latest_quote
        if quote is None:
            return ["尚未生成报价"]
        items = [issue.message for issue in quote.blocking_issues]
        if quote.status == "ready":
            recheck = self.recheck(inquiry_ref, as_of)
            if recheck.status == "recheck_needed":
                items.extend(f"需重新核对：{change}" for change in recheck.changes)
            else:
                items.append("等待顾客确认")
        return items
