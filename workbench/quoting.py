"""报价引擎：按询价时刻解析依据，计算报价并列出待确认事项。

规则要点：
- 价格、活动按生效时段取当时版本；旧金检测按饰品与检测时间取当时版本；
- 以旧换新时，旧金抵扣与新饰品售价分开记录，互不合行；
- 优惠可否叠加由活动规则决定（stackable_with_member / stackable_with_exchange）；
- 价格来源缺失或价签与系统不一致时，报价阻断，绝不静默挑选较低值。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .ledger import Ledger
from .model import Assay, CounterTag, MarketPrice, Product, Promotion

CENT = Decimal("0.01")
MEMBER_DISCOUNT = Decimal("0.98")  # 门店会员折扣（合成设定）


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class QuoteRequest:
    """一次顾客询价的输入。"""

    inquiry_ref: str
    customer_ref: str
    store_ref: str
    sku_ref: str
    quoted_at: datetime
    weight_gram: Decimal | None = None  # 未称重时取商品档案默认克重
    member: bool = False
    exchange_item_ref: str | None = None  # 以旧换新的旧金饰品
    promotion_ref: str | None = None  # 指定活动；不指定则按生效活动判定


@dataclass(frozen=True)
class Issue:
    """报价事项。blocking=True 表示未解决前不能成交。"""

    code: str
    blocking: bool
    message: str


@dataclass(frozen=True)
class MoneyLine:
    """金额行：label 区分新饰品与旧金抵扣，detail 记录计算依据。"""

    label: str
    amount: Decimal
    detail: str


@dataclass(frozen=True)
class Basis:
    """报价依据快照：引用的都是台账中的不可变记录。"""

    market_price: MarketPrice | None
    counter_tag: CounterTag | None
    promotion: Promotion | None
    assay: Assay | None
    product: Product | None


@dataclass(frozen=True)
class Quote:
    inquiry_ref: str
    revision: int
    request: QuoteRequest
    status: str  # "ready" | "blocked"
    lines: tuple[MoneyLine, ...]
    new_item_net: Decimal | None
    exchange_deduction: Decimal | None
    amount_due: Decimal | None
    discounts_applied: tuple[str, ...]
    issues: tuple[Issue, ...]
    basis: Basis

    @property
    def blocking_issues(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.blocking)


class QuoteEngine:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def build(self, request: QuoteRequest, revision: int = 1) -> Quote:
        moment = request.quoted_at
        issues: list[Issue] = []

        # 1) 市场金价
        market: MarketPrice | None = None
        price_res = self.ledger.resolve_market_price(moment)
        if price_res.ok:
            market = price_res.record
        elif price_res.status == "conflict":
            issues.append(Issue("PRICE_CONFLICT", True, f"金价来源冲突，需人工核实：{price_res.detail}"))
        else:
            issues.append(Issue("MISSING_MARKET_PRICE", True, f"缺少有效金价：{price_res.detail}"))

        # 2) 柜台价签
        tag: CounterTag | None = None
        tag_res = self.ledger.resolve_counter_tag(request.store_ref, request.sku_ref, moment)
        if tag_res.ok:
            tag = tag_res.record
        elif tag_res.status == "conflict":
            issues.append(
                Issue("TAG_CONFLICT", True, f"价签与系统不一致，不能自动择低成交，需核实：{tag_res.detail}")
            )
        else:
            code = "TAG_EXPIRED" if tag_res.candidates else "TAG_MISSING"
            issues.append(Issue(code, True, f"缺少有效价签：{tag_res.detail}"))

        # 3) 商品与克重
        product = self.ledger.product(request.sku_ref, at=moment)
        weight = request.weight_gram if request.weight_gram is not None else (
            product.default_weight_gram if product else None
        )
        if weight is None:
            issues.append(Issue("MISSING_WEIGHT", True, f"{request.sku_ref} 未称重且商品档案无默认克重"))

        # 4) 活动
        promotion: Promotion | None = None
        active = self.ledger.active_promotions(moment)
        if request.promotion_ref:
            promotion = next((p for p in active if p.promotion_ref == request.promotion_ref), None)
            if promotion is None:
                issues.append(
                    Issue("PROMO_INACTIVE", True, f"活动 {request.promotion_ref} 在 {moment.isoformat()} 不生效")
                )
        elif len(active) == 1:
            promotion = active[0]
        elif len(active) > 1:
            refs = "、".join(p.promotion_ref for p in active)
            issues.append(Issue("PROMO_AMBIGUOUS", True, f"多个活动同时生效（{refs}），需指定其一，不能自动挑选"))
        else:
            issues.append(Issue("NO_ACTIVE_PROMO", False, "询价时无生效活动，按柜台价执行"))

        # 5) 旧金检测（以旧换新时必需，且取检测时间 <= 询价时刻的版本）
        assay: Assay | None = None
        if request.exchange_item_ref:
            assay = self.ledger.assay_for(request.exchange_item_ref, moment)
            if assay is None:
                issues.append(
                    Issue("ASSAY_MISSING", True, f"{request.exchange_item_ref} 在询价时无有效检测结果，需安排检测")
                )

        basis = Basis(market, tag, promotion, assay, product)
        if any(i.blocking for i in issues):
            return Quote(
                inquiry_ref=request.inquiry_ref,
                revision=revision,
                request=request,
                status="blocked",
                lines=(),
                new_item_net=None,
                exchange_deduction=None,
                amount_due=None,
                discounts_applied=(),
                issues=tuple(issues),
                basis=basis,
            )

        # ---- 计算（此时 market/tag/weight 齐备）----
        assert market is not None and tag is not None and weight is not None
        lines: list[MoneyLine] = []
        applied: list[str] = []

        gross = money(weight * (market.price_per_gram + tag.making_fee_per_gram))
        lines.append(
            MoneyLine(
                "新饰品毛额",
                gross,
                f"{weight}克 × (金价{market.price_per_gram} + 工费{tag.making_fee_per_gram}) 元/克",
            )
        )

        net = money(gross * tag.discount)
        applied.append(f"柜台折扣 {tag.discount}")

        # 活动规则：以旧换新不与活动/会员优惠同享时，移除相应折扣
        exchange_blocked = bool(
            request.exchange_item_ref and promotion and not promotion.stackable_with_exchange
        )
        if promotion and promotion.discount is not None:
            if exchange_blocked:
                issues.append(
                    Issue(
                        "EXCHANGE_NOT_STACKABLE",
                        False,
                        f"活动 {promotion.promotion_ref} 规则：以旧换新不与活动折扣同享，已移除活动折扣 {promotion.discount}",
                    )
                )
            else:
                net = money(net * promotion.discount)
                applied.append(f"活动折扣 {promotion.discount}（{promotion.promotion_ref}）")
        if request.member:
            if exchange_blocked:
                issues.append(
                    Issue(
                        "EXCHANGE_NOT_STACKABLE",
                        False,
                        f"活动 {promotion.promotion_ref} 规则：以旧换新不与会员折扣同享，已移除会员折扣 {MEMBER_DISCOUNT}",
                    )
                )
            elif promotion and not promotion.stackable_with_member:
                issues.append(
                    Issue("MEMBER_NOT_STACKABLE", False, f"活动 {promotion.promotion_ref} 规则：会员折扣不与活动同享")
                )
            else:
                net = money(net * MEMBER_DISCOUNT)
                applied.append(f"会员折扣 {MEMBER_DISCOUNT}")

        lines.append(MoneyLine("新饰品应付", net, "；".join(applied)))

        # 旧金抵扣单独成行
        deduction: Decimal | None = None
        if request.exchange_item_ref and assay is not None:
            deduction = money(
                assay.weight_gram * (Decimal(assay.purity_per_mille) / Decimal(1000)) * market.price_per_gram
            )
            lines.append(
                MoneyLine(
                    "旧金抵扣",
                    -deduction,
                    f"{assay.item_ref}：{assay.weight_gram}克 × {assay.purity_per_mille}‰ × 金价{market.price_per_gram}"
                    f"（检测单 {assay.assay_ref} rev{assay.revision}）",
                )
            )

        due = money(net - (deduction or Decimal("0")))
        lines.append(MoneyLine("应收差额", due, "新饰品应付 − 旧金抵扣"))
        if due < 0:
            issues.append(Issue("NEGATIVE_DUE", False, "旧金抵扣超过新饰品应付，差额需门店退还顾客"))

        return Quote(
            inquiry_ref=request.inquiry_ref,
            revision=revision,
            request=request,
            status="ready",
            lines=tuple(lines),
            new_item_net=net,
            exchange_deduction=deduction,
            amount_due=due,
            discounts_applied=tuple(applied),
            issues=tuple(issues),
            basis=basis,
        )
