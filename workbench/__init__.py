"""珠宝门店询价资料工作台（全部为虚构合成数据）。

模块：
- model：来源信封与领域记录
- ledger：只增不改台账，按时刻回查，冲突不静默择低
- quoting：报价引擎（生效时段、叠加规则、新旧分列）
- inquiries：询价会话、成交确认快照、事后核对
- views：销售待办 / 店长班次核对 / 顾客争议回查
"""
from .inquiries import Confirmation, InquiryBook, RecheckResult, WorkbenchError
from .ledger import Ledger, Resolution
from .model import Envelope, SourceError
from .quoting import MEMBER_DISCOUNT, Quote, QuoteEngine, QuoteRequest
from .views import dispute_replay, sales_view, shift_report

__all__ = [
    "Confirmation",
    "Envelope",
    "InquiryBook",
    "Ledger",
    "MEMBER_DISCOUNT",
    "Quote",
    "QuoteEngine",
    "QuoteRequest",
    "RecheckResult",
    "Resolution",
    "SourceError",
    "WorkbenchError",
    "dispute_replay",
    "sales_view",
    "shift_report",
]
