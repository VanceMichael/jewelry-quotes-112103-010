"""工作台业务规则测试（全部为虚构合成数据）。"""
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench import (  # noqa: E402
    Envelope,
    InquiryBook,
    Ledger,
    QuoteEngine,
    QuoteRequest,
    SourceError,
    WorkbenchError,
    dispute_replay,
    sales_view,
    shift_report,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "fixtures" / "scenarios" / "day1.json"
CONTRACT = ROOT / "fixtures" / "sources.json"
STORE = "STORE-SYN-3"


def T(value: str) -> datetime:
    return datetime.fromisoformat(value)


def make_ledger() -> Ledger:
    ledger = Ledger()
    ledger.add_file(SCENARIO)
    return ledger


def request(inquiry_ref, sku="SKU-14", at="2026-11-20T10:15:00+08:00", **kwargs):
    return QuoteRequest(inquiry_ref, "CUST-SYN-T", STORE, sku, T(at), **kwargs)


class IngestTests(unittest.TestCase):
    def test_contract_fixture_loads(self):
        ledger = Ledger()
        count = ledger.add_file(CONTRACT)
        self.assertEqual(count, 5)
        self.assertEqual(
            ledger.counts_by_kind(),
            {"assay": 1, "counter_price": 1, "market_price": 1, "promotion": 1, "sale": 1},
        )

    def test_scenario_fixture_loads(self):
        ledger = make_ledger()
        self.assertEqual(ledger.counts_by_kind()["assay"], 2)
        self.assertEqual(ledger.counts_by_kind()["promotion"], 2)

    def test_bad_envelope_rejected(self):
        ledger = Ledger()
        with self.assertRaises(SourceError):
            ledger.add(Envelope.from_dict({"record_kind": "sale", "collected_at": "2026-11-20T09:00:00+08:00", "payload": {}}))

    def test_duplicate_source_id_rejected(self):
        ledger = make_ledger()
        with self.assertRaises(SourceError):
            ledger.add_file(SCENARIO)


class QuoteRuleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        self.engine = QuoteEngine(self.ledger)

    def test_exchange_lines_recorded_separately(self):
        quote = self.engine.build(request("INQ-X", member=True, exchange_item_ref="ITEM-SYN-72"))
        self.assertEqual(quote.status, "ready")
        # 新饰品：12.5 × (832 + 55) × 0.6 = 6652.50
        self.assertEqual(quote.new_item_net, Decimal("6652.50"))
        # 旧金抵扣：20.4 × 990‰ × 832 = 16803.07（单独成行）
        self.assertEqual(quote.exchange_deduction, Decimal("16803.07"))
        self.assertEqual(quote.amount_due, Decimal("-10150.57"))
        labels = [line.label for line in quote.lines]
        self.assertIn("新饰品应付", labels)
        self.assertIn("旧金抵扣", labels)
        new_line = next(line for line in quote.lines if line.label == "新饰品应付")
        old_line = next(line for line in quote.lines if line.label == "旧金抵扣")
        self.assertNotEqual(new_line.detail, old_line.detail)

    def test_stacking_decided_by_promotion_rules(self):
        # 以旧换新：活动规定不同享 → 活动与会员折扣均被移除
        quote = self.engine.build(request("INQ-X", member=True, exchange_item_ref="ITEM-SYN-72"))
        self.assertEqual(quote.discounts_applied, ("柜台折扣 0.6",))
        notes = {i.code for i in quote.issues if not i.blocking}
        self.assertIn("EXCHANGE_NOT_STACKABLE", notes)
        # 纯会员：活动允许与会员叠加 → 6652.50 × 0.95 × 0.98 = 6193.48
        member_quote = self.engine.build(request("INQ-Y", at="2026-11-20T10:00:00+08:00", member=True))
        self.assertEqual(member_quote.amount_due, Decimal("6193.48"))
        self.assertEqual(len(member_quote.discounts_applied), 3)

    def test_effective_windows(self):
        # 金价 09:00 才生效
        early = self.engine.build(request("INQ-A", at="2026-11-20T08:30:00+08:00"))
        self.assertEqual(early.status, "blocked")
        self.assertIn("MISSING_MARKET_PRICE", {i.code for i in early.blocking_issues})
        self.assertIsNone(early.amount_due)
        # 价签前一日 18:00 已失效
        expired = self.engine.build(request("INQ-B", sku="SKU-30", at="2026-11-20T13:20:00+08:00"))
        self.assertEqual(expired.status, "blocked")
        self.assertIn("TAG_EXPIRED", {i.code for i in expired.blocking_issues})
        # 午后金价 14:00 起 845 元/克
        afternoon = self.engine.build(request("INQ-C", at="2026-11-20T14:30:00+08:00"))
        self.assertEqual(afternoon.basis.market_price.revision, 11)
        self.assertEqual(afternoon.new_item_net, Decimal("6547.50"))  # 12.5×900×0.6×0.97

    def test_tag_conflict_never_silently_picks_lower(self):
        quote = self.engine.build(request("INQ-D", sku="SKU-20", at="2026-11-20T13:10:00+08:00"))
        self.assertEqual(quote.status, "blocked")
        self.assertIsNone(quote.amount_due)
        conflict = next(i for i in quote.blocking_issues if i.code == "TAG_CONFLICT")
        self.assertIn("0.6", conflict.message)
        self.assertIn("0.65", conflict.message)
        # 台账解析层同样不得给出单一值
        res = self.ledger.resolve_counter_tag(STORE, "SKU-20", T("2026-11-20T13:10:00+08:00"))
        self.assertEqual(res.status, "conflict")
        self.assertIsNone(res.record)
        self.assertEqual(len(res.candidates), 2)

    def test_market_price_conflict_blocks(self):
        ledger = make_ledger()
        ledger.add(Envelope.from_dict({
            "source_id": "market-b-1",
            "record_kind": "market_price",
            "collected_at": "2026-11-20T09:05:00+08:00",
            "payload": {"market_ref": "MKT-SYN-B", "metal": "gold", "price_per_gram_yuan": 828.0,
                        "valid_from": "2026-11-20T09:00:00+08:00", "revision": 1},
        }))
        quote = QuoteEngine(ledger).build(request("INQ-E"))
        self.assertEqual(quote.status, "blocked")
        self.assertIn("PRICE_CONFLICT", {i.code for i in quote.blocking_issues})

    def test_ambiguous_promotions_block(self):
        ledger = make_ledger()
        ledger.add(Envelope.from_dict({
            "source_id": "promo-9-r1",
            "record_kind": "promotion",
            "collected_at": "2026-11-20T09:40:00+08:00",
            "payload": {"promotion_ref": "PROMO-9", "discount": 0.9,
                        "starts_at": "2026-11-20T00:00:00+08:00", "ends_at": "2026-11-20T23:59:00+08:00"},
        }))
        quote = QuoteEngine(ledger).build(request("INQ-F"))
        self.assertEqual(quote.status, "blocked")
        self.assertIn("PROMO_AMBIGUOUS", {i.code for i in quote.blocking_issues})

    def test_missing_assay_blocks_exchange(self):
        quote = self.engine.build(request("INQ-G", exchange_item_ref="ITEM-SYN-404"))
        self.assertEqual(quote.status, "blocked")
        self.assertIn("ASSAY_MISSING", {i.code for i in quote.blocking_issues})

    def test_assay_bound_to_item_and_time(self):
        # 检测于 10:09，10:05 询价时还没有有效检测
        early = self.engine.build(request("INQ-H", at="2026-11-20T10:05:00+08:00", exchange_item_ref="ITEM-SYN-72"))
        self.assertIn("ASSAY_MISSING", {i.code for i in early.blocking_issues})
        # 复测后（11:20）新询价用新版本
        later = self.engine.build(request("INQ-I", at="2026-11-20T11:30:00+08:00", exchange_item_ref="ITEM-SYN-72"))
        self.assertEqual(later.basis.assay.revision, 2)
        self.assertEqual(later.exchange_deduction, Decimal("16887.94"))  # 20.4×0.995×832


class InquiryFlowTests(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        self.book = InquiryBook(self.ledger)

    def _confirm_inq1(self):
        self.book.open(QuoteRequest(
            "INQ-1", "CUST-SYN-1", STORE, "SKU-14", T("2026-11-20T10:15:00+08:00"),
            member=True, exchange_item_ref="ITEM-SYN-72",
        ))
        return self.book.confirm("INQ-1", "SALE-19", "held", T("2026-11-20T10:25:00+08:00"))

    def test_blocked_quote_cannot_confirm(self):
        self.book.open(request("INQ-B", sku="SKU-20", at="2026-11-20T13:10:00+08:00"))
        with self.assertRaises(WorkbenchError):
            self.book.confirm("INQ-B", "SALE-X", "paid", T("2026-11-20T13:11:00+08:00"))

    def test_promotion_update_requires_recheck_for_open_quotes(self):
        self.book.open(QuoteRequest("INQ-2", "CUST-SYN-2", STORE, "SKU-14",
                                    T("2026-11-20T10:00:00+08:00"), member=True))
        result = self.book.recheck("INQ-2", T("2026-11-20T12:30:00+08:00"))
        self.assertEqual(result.status, "recheck_needed")
        self.assertTrue(any("PROMO-8" in change for change in result.changes))
        # 重新报价后依据更新为新活动版本
        quote = self.book.requote("INQ-2", T("2026-11-20T12:35:00+08:00"))
        self.assertEqual(quote.revision, 2)
        self.assertEqual(quote.basis.promotion.source_id, "promo-8-r2")
        self.assertEqual(quote.amount_due, Decimal("6323.87"))  # 6652.50×0.97×0.98
        self.assertEqual(self.book.recheck("INQ-2", T("2026-11-20T12:40:00+08:00")).status, "current")

    def test_confirmed_order_locked_and_reassay_does_not_rewrite(self):
        self._confirm_inq1()
        # 已成交：活动更新、金价变动、复测都按确认记录解释
        self.assertEqual(self.book.recheck("INQ-1", T("2026-11-20T12:30:00+08:00")).status, "locked")
        replay = dispute_replay(self.book, "SALE-19")
        self.assertEqual(replay["basis"]["assay"]["revision"], 1)
        self.assertEqual(replay["basis"]["assay"]["purity_per_mille"], 990)
        self.assertEqual(replay["basis"]["market_price"]["revision"], 10)
        self.assertEqual(replay["basis"]["promotion"]["source_id"], "promo-8-r1")
        # 后续复测仅作提示
        later = replay["later_versions"]["assay"]
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["revision"], 2)
        # 快照不被后续台账写入改变
        self.book.ledger  # 台账仍在，但确认快照独立
        conf = self.book.confirmation("SALE-19")
        self.assertEqual(conf.quote.basis.assay.purity_per_mille, 990)

    def test_checklist_shows_missing_confirmations(self):
        self.book.open(request("INQ-0", at="2026-11-20T08:30:00+08:00"))
        items = self.book.checklist("INQ-0", T("2026-11-20T08:30:00+08:00"))
        self.assertTrue(any("金价" in item for item in items))
        self.book.open(QuoteRequest("INQ-2", "CUST-SYN-2", STORE, "SKU-14",
                                    T("2026-11-20T10:00:00+08:00"), member=True))
        self.assertEqual(self.book.checklist("INQ-2", T("2026-11-20T10:05:00+08:00")), ["等待顾客确认"])
        recheck_items = self.book.checklist("INQ-2", T("2026-11-20T12:30:00+08:00"))
        self.assertTrue(any(item.startswith("需重新核对") for item in recheck_items))
        self._confirm_inq1()
        self.assertEqual(self.book.checklist("INQ-1", T("2026-11-20T12:30:00+08:00")), [])


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        self.book = InquiryBook(self.ledger)
        self.book.open(QuoteRequest("INQ-1", "CUST-SYN-1", STORE, "SKU-14",
                                    T("2026-11-20T10:15:00+08:00"), member=True,
                                    exchange_item_ref="ITEM-SYN-72"))
        self.book.confirm("INQ-1", "SALE-19", "held", T("2026-11-20T10:25:00+08:00"))
        self.book.open(QuoteRequest("INQ-3", "CUST-SYN-3", STORE, "SKU-14",
                                    T("2026-11-20T14:30:00+08:00")))
        self.book.confirm("INQ-3", "SALE-20", "paid", T("2026-11-20T14:35:00+08:00"))

    def test_sales_view(self):
        view = sales_view(self.book, "INQ-1", T("2026-11-20T12:30:00+08:00"))
        self.assertEqual(view["status"], "confirmed")
        self.assertEqual(view["confirmed_sale_ref"], "SALE-19")
        self.assertEqual(view["quote"]["amount_due"], "-10150.57")
        self.assertEqual(view["basis"]["assay"]["assay_ref"], "ASSAY-22")

    def test_shift_report_reconciles_with_counter_records(self):
        report = shift_report(self.book, T("2026-11-20T09:00:00+08:00"), T("2026-11-20T15:00:00+08:00"))
        self.assertEqual(report["confirmed_count"], 2)
        by_ref = {row["sale_ref"]: row for row in report["rows"]}
        self.assertEqual(by_ref["SALE-19"]["verification"], ["一致"])
        self.assertEqual(by_ref["SALE-20"]["verification"], ["一致"])
        self.assertEqual(by_ref["SALE-19"]["exchange_deduction"], "16803.07")
        self.assertIsNone(by_ref["SALE-20"]["exchange_deduction"])
        # SALE-21 柜台有记录但工作台无快照 → 异常
        self.assertEqual([a["sale_ref"] for a in report["anomalies"]], ["SALE-21"])
        self.assertEqual(report["total_amount_due"], str(Decimal("-10150.57") + Decimal("6547.50")))

    def test_dispute_replay_shows_as_of_versions(self):
        replay = dispute_replay(self.book, "SALE-19")
        self.assertEqual(replay["state"], "held")
        self.assertEqual(replay["basis"]["market_price"]["price_per_gram_yuan"], "832.0")
        self.assertEqual(replay["basis"]["counter_tag"]["discount"], "0.6")
        self.assertEqual(replay["basis"]["promotion"]["ends_at"], "2026-11-22T23:59:00+08:00")
        self.assertEqual(replay["amounts"]["new_item_net"], "6652.50")
        self.assertEqual(replay["amounts"]["exchange_deduction"], "16803.07")
        with self.assertRaises(WorkbenchError):
            dispute_replay(self.book, "SALE-404")


if __name__ == "__main__":
    unittest.main()
