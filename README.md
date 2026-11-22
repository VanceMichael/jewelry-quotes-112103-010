# 珠宝门店询价资料

本目录包含合成市场价格、门店价签、促销活动、饰品检测和成交记录。资料保留来源与生效时间，便于离线开发报价采集能力。

使用 Python 3.11 或更新版本运行基线检查：

```sh
python3 -m unittest discover -s tests
```

演练资料位于 `fixtures/sources.json`。价格、商品、顾客和交易均为虚构值。项目不需要联网或第三方依赖。

## 询价资料工作台（workbench）

`workbench/` 是门店可复用的询价资料工作台，接入上述来源信封格式的合成资料，支持销售人员按顾客询价查看有效报价及其来源。

```sh
python3 -m workbench validate            # 校验来源资料（默认 fixtures/sources.json）
python3 -m workbench demo                # 合成演练：门店一天（fixtures/scenarios/day1.json）
python3 -m workbench quote --sku SKU-14 --at 2026-11-20T10:15:00+08:00 \
    --member --exchange-item ITEM-SYN-72 # 按顾客询价计算有效报价
```

### 模块

- `workbench/model.py`：来源信封与不可变领域记录（金价、价签、活动、检测、商品、成交）。
- `workbench/ledger.py`：只增不改台账。复测、调价、活动更新都是新增版本；按业务时刻回查当时生效且已知的版本；不同来源数值不一致判定为冲突，绝不静默挑选较低值。
- `workbench/quoting.py`：报价引擎。价格与活动按生效时段取当时版本；旧金抵扣与新饰品售价分开成行；优惠可否叠加由活动规则（`stackable_with_member` / `stackable_with_exchange`）决定；价格来源缺失、价签过期或价签与系统不一致时报价阻断。
- `workbench/inquiries.py`：询价会话。成交（收款/留货）保存确认快照，之后按确认记录解释；活动更新、金价变动、价签更新或旧金复测后，未成交报价需重新核对；销售待办清单列出还缺哪项确认。
- `workbench/views.py`：角色视图。`sales_view`（销售待办与报价来源）、`shift_report`（店长按班次核对成交报价与依据，并与柜台系统成交记录交叉核验）、`dispute_replay`（顾客争议时回查称重、检测、活动、价格的当时版本）。

### 业务规则摘要

- 价格、活动都有生效时段；旧金检测结果只适用于对应饰品及检测时间。
- 重新检测产生新 revision，不覆盖已成交订单当时使用的依据。
- 活动更新后，未成交报价标记为需重新核对；已收款或留货订单按确认快照解释。
- 价格来源缺失或门店价签与系统版本不一致时，报价阻断并提示人工核实，不自动择低成交。
