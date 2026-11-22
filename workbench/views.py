"""面向角色的视图：销售待办、店长班次核对、顾客争议回查。

视图输出均为 JSON 友好的字典（时间为 ISO 字符串，金额为字符串），
便于接入门店界面或打印。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .inquiries import Confirmation, InquiryBook, WorkbenchError
from .model import Assay, CounterTag, MarketPrice, Promotion
from .quoting import Quote


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _yuan(amount: Decimal | None) -> str | None:
    return str(amount) if amount is not None else None


def _market_dict(rec: MarketPrice) -> dict[str, Any]:
    return {
        "source_id": rec.source_id,
        "market_ref": rec.market_ref,
        "revision": rec.revision,
        "price_per_gram_yuan": str(rec.price_per_gram),
        "valid_from": _iso(rec.valid_from),
    }


def _tag_dict(rec: CounterTag) -> dict[str, Any]:
    return {
        "source_id": rec.source_id,
        "channel": rec.channel,
        "making_fee_per_gram_yuan": str(rec.making_fee_per_gram),
        "discount": str(rec.discount),
        "valid_until": _iso(rec.valid_until),
    }


def _promotion_dict(rec: Promotion) -> dict[str, Any]:
    return {
        "source_id": rec.source_id,
        "promotion_ref": rec.promotion_ref,
        "discount": _yuan(rec.discount),
        "stackable_with_member": rec.stackable_with_member,
        "stackable_with_exchange": rec.stackable_with_exchange,
        "starts_at": _iso(rec.starts_at),
        "ends_at": _iso(rec.ends_at),
        "collected_at": _iso(rec.collected_at),
    }


def _assay_dict(rec: Assay) -> dict[str, Any]:
    return {
        "source_id": rec.source_id,
        "assay_ref": rec.assay_ref,
        "revision": rec.revision,
        "item_ref": rec.item_ref,
        "weight_gram": str(rec.weight_gram),
        "purity_per_mille": rec.purity_per_mille,
        "tested_at": _iso(rec.tested_at),
    }


def _quote_dict(quote: Quote) -> dict[str, Any]:
    return {
        "revision": quote.revision,
        "status": quote.status,
        "quoted_at": _iso(quote.request.quoted_at),
        "new_item_net": _yuan(quote.new_item_net),
        "exchange_deduction": _yuan(quote.exchange_deduction),
        "amount_due": _yuan(quote.amount_due),
        "discounts_applied": list(quote.discounts_applied),
        "lines": [
            {"label": line.label, "amount": str(line.amount), "detail": line.detail}
            for line in quote.lines
        ],
        "issues": [
            {"code": issue.code, "blocking": issue.blocking, "message": issue.message}
            for issue in quote.issues
        ],
    }


def _basis_dict(quote: Quote) -> dict[str, Any]:
    basis = quote.basis
    return {
        "quoted_at": _iso(quote.request.quoted_at),
        "quote_revision": quote.revision,
        "market_price": _market_dict(basis.market_price) if basis.market_price else None,
        "counter_tag": _tag_dict(basis.counter_tag) if basis.counter_tag else None,
        "promotion": _promotion_dict(basis.promotion) if basis.promotion else None,
        "assay": _assay_dict(basis.assay) if basis.assay else None,
    }


def sales_view(book: InquiryBook, inquiry_ref: str, as_of: datetime) -> dict[str, Any]:
    """销售人员视角：最新报价、报价来源与还缺哪项确认。"""
    inquiry = book.get(inquiry_ref)
    quote = inquiry.latest_quote
    pending = book.checklist(inquiry_ref, as_of)
    if inquiry.confirmation is not None:
        status = "confirmed"
    elif quote is None:
        status = "no_quote"
    elif quote.status == "blocked":
        status = "blocked"
    elif any(item.startswith("需重新核对") for item in pending):
        status = "needs_recheck"
    else:
        status = "waiting_customer"
    return {
        "inquiry_ref": inquiry_ref,
        "customer_ref": inquiry.request.customer_ref,
        "sku_ref": inquiry.request.sku_ref,
        "exchange_item_ref": inquiry.request.exchange_item_ref,
        "status": status,
        "pending_confirmations": pending,
        "quote": _quote_dict(quote) if quote else None,
        "basis": _basis_dict(quote) if quote else None,
        "confirmed_sale_ref": inquiry.confirmation.sale_ref if inquiry.confirmation else None,
    }


def shift_report(book: InquiryBook, start: datetime, end: datetime) -> dict[str, Any]:
    """店长视角：班次内成交报价与依据核对，以及柜台系统记录交叉核验。"""
    rows = []
    confirmed_refs = set()
    for conf in book.confirmations_between(start, end):
        confirmed_refs.add(conf.sale_ref)
        checks = _cross_check(book, conf)
        quote = conf.quote
        rows.append(
            {
                "sale_ref": conf.sale_ref,
                "inquiry_ref": conf.inquiry_ref,
                "state": conf.state,
                "confirmed_at": _iso(conf.confirmed_at),
                "sku_ref": quote.request.sku_ref,
                "customer_ref": quote.request.customer_ref,
                "new_item_net": _yuan(quote.new_item_net),
                "exchange_deduction": _yuan(quote.exchange_deduction),
                "amount_due": _yuan(quote.amount_due),
                "basis": _basis_dict(quote),
                "verification": checks,
            }
        )
    anomalies = []
    for sale in book.ledger.sales_between(start, end):
        if sale.sale_ref not in confirmed_refs:
            anomalies.append(
                {
                    "sale_ref": sale.sale_ref,
                    "issue": "柜台系统有成交记录，但工作台无确认快照，需补录核实",
                    "state": sale.state,
                    "confirmed_at": _iso(sale.confirmed_at),
                }
            )
    total_due = sum((conf.quote.amount_due or Decimal("0")) for conf in book.confirmations_between(start, end))
    return {
        "shift": {"start": _iso(start), "end": _iso(end)},
        "confirmed_count": len(rows),
        "total_amount_due": str(total_due),
        "rows": rows,
        "anomalies": anomalies,
    }


def _cross_check(book: InquiryBook, conf: Confirmation) -> list[str]:
    sale = book.ledger.sale_by_ref(conf.sale_ref)
    if sale is None:
        return ["柜台系统缺少该成交记录"]
    checks = []
    if sale.state != conf.state:
        checks.append(f"状态不一致：柜台 {sale.state} / 工作台 {conf.state}")
    if sale.quote_revision != conf.quote_revision:
        checks.append(f"报价版本不一致：柜台 rev{sale.quote_revision} / 工作台 rev{conf.quote_revision}")
    return checks or ["一致"]


def dispute_replay(book: InquiryBook, sale_ref: str) -> dict[str, Any]:
    """顾客争议回查：称重、检测、活动、价格的当时版本。

    后续若有复测、调价或活动更新，仅列在 later_versions 中提示，
    本单依据不变。
    """
    conf = book.confirmation(sale_ref)
    if conf is None:
        raise WorkbenchError(f"无成交确认记录: {sale_ref}")
    quote = conf.quote
    basis = quote.basis

    later: dict[str, Any] = {"assay": [], "promotion": [], "market_price": []}
    if basis.assay is not None:
        later["assay"] = [
            _assay_dict(a)
            for a in book.ledger.assay_history(basis.assay.item_ref)
            if (a.tested_at, a.revision) > (basis.assay.tested_at, basis.assay.revision)
        ]
    if basis.promotion is not None:
        later["promotion"] = [
            _promotion_dict(p)
            for p in book.ledger.records("promotion")
            if p.promotion_ref == basis.promotion.promotion_ref
            and p.collected_at > basis.promotion.collected_at
        ]
    if basis.market_price is not None:
        later["market_price"] = [
            _market_dict(m)
            for m in book.ledger.records("market_price")
            if m.metal == basis.market_price.metal and m.valid_from > basis.market_price.valid_from
        ]

    return {
        "sale_ref": conf.sale_ref,
        "inquiry_ref": conf.inquiry_ref,
        "state": conf.state,
        "confirmed_at": _iso(conf.confirmed_at),
        "customer_ref": quote.request.customer_ref,
        "amounts": {
            "new_item_net": _yuan(quote.new_item_net),
            "exchange_deduction": _yuan(quote.exchange_deduction),
            "amount_due": _yuan(quote.amount_due),
        },
        "lines": _quote_dict(quote)["lines"],
        "basis": _basis_dict(quote),
        "later_versions": later,
        "note": "成交后如有复测、调价或活动更新，不影响本单当时依据",
    }
