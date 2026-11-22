"""命令行入口：validate / quote / demo。

demo 用合成演练资料完整走一遍门店一天：询价、成交、复测、活动更新、
价签冲突、班次核对与争议回查。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .inquiries import InquiryBook, WorkbenchError
from .ledger import Ledger
from .model import SourceError
from .quoting import Quote, QuoteRequest
from .views import dispute_replay, sales_view, shift_report

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FIXTURE = ROOT / "fixtures" / "sources.json"
SCENARIO_FIXTURE = ROOT / "fixtures" / "scenarios" / "day1.json"
STORE = "STORE-SYN-3"


def T(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _print_quote(quote: Quote) -> None:
    print(f"  报价 rev{quote.revision} @ {quote.request.quoted_at.isoformat()} 状态={quote.status}")
    for line in quote.lines:
        print(f"    {line.label}: {line.amount} 元（{line.detail}）")
    for issue in quote.issues:
        level = "阻断" if issue.blocking else "提示"
        print(f"    [{level}|{issue.code}] {issue.message}")


def _print_pending(title: str, pending: list[str]) -> None:
    print(title)
    if not pending:
        print("    无待办事项")
    for item in pending:
        print(f"    - {item}")


def _load_ledger(files: list[Path]) -> Ledger:
    ledger = Ledger()
    for path in files:
        ledger.add_file(path)
    return ledger


def cmd_validate(args: argparse.Namespace) -> int:
    files = [Path(p) for p in args.files] or [CONTRACT_FIXTURE]
    try:
        ledger = _load_ledger(files)
    except SourceError as exc:
        print(f"资料校验失败: {exc}")
        return 1
    print("资料校验通过：")
    for kind, count in ledger.counts_by_kind().items():
        print(f"  {kind}: {count} 条")
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    files = [Path(p) for p in args.files] or [SCENARIO_FIXTURE]
    try:
        ledger = _load_ledger(files)
    except SourceError as exc:
        print(f"资料校验失败: {exc}")
        return 1
    request = QuoteRequest(
        inquiry_ref="INQ-CLI",
        customer_ref=args.customer,
        store_ref=args.store,
        sku_ref=args.sku,
        quoted_at=T(args.at),
        weight_gram=Decimal(str(args.weight)) if args.weight is not None else None,
        member=args.member,
        exchange_item_ref=args.exchange_item,
        promotion_ref=args.promotion,
    )
    quote = InquiryBook(ledger).open(request)
    _print_quote(quote)
    return 0 if quote.status == "ready" else 2


def cmd_demo(args: argparse.Namespace) -> int:
    ledger = _load_ledger([SCENARIO_FIXTURE])
    book = InquiryBook(ledger)
    print("=== 珠宝门店询价资料工作台 · 合成演练（2026-11-20, 全部虚构数据）===")

    print("\n--- 1. 08:30 开业前询价：金价尚未生效 ---")
    book.open(QuoteRequest("INQ-0", "CUST-SYN-0", STORE, "SKU-14", T("2026-11-20T08:30:00+08:00")))
    _print_quote(book.get("INQ-0").latest_quote)
    _print_pending("  销售待办：", book.checklist("INQ-0", T("2026-11-20T08:30:00+08:00")))

    print("\n--- 2. 10:15 以旧换新询价（旧金抵扣与新饰品售价分开记录）---")
    book.open(
        QuoteRequest(
            "INQ-1", "CUST-SYN-1", STORE, "SKU-14", T("2026-11-20T10:15:00+08:00"),
            member=True, exchange_item_ref="ITEM-SYN-72",
        )
    )
    _print_quote(book.get("INQ-1").latest_quote)

    print("\n--- 3. 10:25 顾客留货成交 SALE-19（保存确认快照）---")
    conf = book.confirm("INQ-1", "SALE-19", "held", T("2026-11-20T10:25:00+08:00"))
    print(f"  成交快照：{conf.sale_ref} state={conf.state} 报价rev{conf.quote_revision} 应收差额 {conf.quote.amount_due} 元")

    print("\n--- 4. 10:00 另一笔会员询价；12:00 活动更新后需重新核对 ---")
    book.open(QuoteRequest("INQ-2", "CUST-SYN-2", STORE, "SKU-14", T("2026-11-20T10:00:00+08:00"), member=True))
    _print_quote(book.get("INQ-2").latest_quote)
    recheck = book.recheck("INQ-2", T("2026-11-20T12:30:00+08:00"))
    print(f"  12:30 核对结果：{recheck.status}")
    for change in recheck.changes:
        print(f"    - {change}")
    _print_pending("  销售待办：", book.checklist("INQ-2", T("2026-11-20T12:30:00+08:00")))
    _print_quote(book.requote("INQ-2", T("2026-11-20T12:35:00+08:00")))
    locked = book.recheck("INQ-1", T("2026-11-20T12:30:00+08:00"))
    print(f"  已成交 INQ-1 核对结果：{locked.status}（按确认记录解释，不受活动更新影响）")

    print("\n--- 5. 13:10 价签与系统不一致：阻断，不静默择低 ---")
    book.open(QuoteRequest("INQ-4", "CUST-SYN-4", STORE, "SKU-20", T("2026-11-20T13:10:00+08:00")))
    _print_quote(book.get("INQ-4").latest_quote)
    _print_pending("  销售待办：", book.checklist("INQ-4", T("2026-11-20T13:10:00+08:00")))

    print("\n--- 6. 13:20 价签过期：阻断 ---")
    book.open(QuoteRequest("INQ-5", "CUST-SYN-5", STORE, "SKU-30", T("2026-11-20T13:20:00+08:00")))
    _print_quote(book.get("INQ-5").latest_quote)

    print("\n--- 7. 14:30 午后金价生效，会员外顾客成交 SALE-20 ---")
    book.open(QuoteRequest("INQ-3", "CUST-SYN-3", STORE, "SKU-14", T("2026-11-20T14:30:00+08:00")))
    _print_quote(book.get("INQ-3").latest_quote)
    conf = book.confirm("INQ-3", "SALE-20", "paid", T("2026-11-20T14:35:00+08:00"))
    print(f"  成交快照：{conf.sale_ref} state={conf.state} 应收 {conf.quote.amount_due} 元")

    print("\n--- 8. 店长班次核对（09:00–15:00）---")
    report = shift_report(book, T("2026-11-20T09:00:00+08:00"), T("2026-11-20T15:00:00+08:00"))
    print(f"  班次成交 {report['confirmed_count']} 笔，应收合计 {report['total_amount_due']} 元")
    for row in report["rows"]:
        basis = row["basis"]
        print(
            f"  {row['sale_ref']} {row['state']} 应收 {row['amount_due']} 元"
            f" | 金价 {basis['market_price']['market_ref']} rev{basis['market_price']['revision']}"
            f" | 检测 {basis['assay']['assay_ref'] + ' rev' + str(basis['assay']['revision']) if basis['assay'] else '无'}"
            f" | 核验 {'；'.join(row['verification'])}"
        )
    for anomaly in report["anomalies"]:
        print(f"  异常 {anomaly['sale_ref']}：{anomaly['issue']}")

    print("\n--- 9. 顾客争议回查 SALE-19（复测不覆盖成交依据）---")
    replay = dispute_replay(book, "SALE-19")
    basis = replay["basis"]
    print(f"  成交于 {replay['confirmed_at']}，报价 rev{basis['quote_revision']}")
    print(f"  当时金价：{basis['market_price']['market_ref']} rev{basis['market_price']['revision']}"
          f" {basis['market_price']['price_per_gram_yuan']} 元/克")
    print(f"  当时检测：{basis['assay']['assay_ref']} rev{basis['assay']['revision']}"
          f" 成色 {basis['assay']['purity_per_mille']}‰（检测于 {basis['assay']['tested_at']}）")
    print(f"  当时活动：{basis['promotion']['promotion_ref']} 至 {basis['promotion']['ends_at']}")
    for later in replay["later_versions"]["assay"]:
        print(f"  后续复测（不影响本单）：rev{later['revision']} 成色 {later['purity_per_mille']}‰")
    print(f"  说明：{replay['note']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description="珠宝门店询价资料工作台（合成数据）")
    sub = parser.add_subparsers(dest="command")

    p_validate = sub.add_parser("validate", help="校验来源资料")
    p_validate.add_argument("files", nargs="*", help="来源 JSON，默认 fixtures/sources.json")
    p_validate.set_defaults(func=cmd_validate)

    p_quote = sub.add_parser("quote", help="按顾客询价计算有效报价")
    p_quote.add_argument("--sku", required=True)
    p_quote.add_argument("--at", required=True, help="询价时刻 ISO，如 2026-11-20T10:15:00+08:00")
    p_quote.add_argument("--store", default=STORE)
    p_quote.add_argument("--customer", default="CUST-SYN-CLI")
    p_quote.add_argument("--weight", type=float, default=None)
    p_quote.add_argument("--member", action="store_true")
    p_quote.add_argument("--exchange-item", default=None)
    p_quote.add_argument("--promotion", default=None)
    p_quote.add_argument("--files", nargs="*", default=[], help="来源 JSON，默认 fixtures/scenarios/day1.json")
    p_quote.set_defaults(func=cmd_quote)

    p_demo = sub.add_parser("demo", help="运行合成演练：门店一天")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except (WorkbenchError, SourceError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
