# 深度登记 中转行李清分台

中转主管逐牌核对航段窗口与动作末态。跨日同航班号不能替代航段实例。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 扫描原件采信 | 查看射频重报和隔离行 | `/desk/scans` `/api/stage/scans` | `app/modules/scans/service.py` | `tests/test_scans.py` |
| M2 航段候选关联 | 核对候选段和提示 | `/desk/legs` `/api/stage/legs` | `app/modules/legs/service.py` | `tests/test_legs.py` |
| M3 扫件有序回放 | 按生效时刻核对动作 | `/desk/replay` `/api/stage/replay` | `app/modules/replay/service.py` | `tests/test_replay.py` |
| M4 错载与漏扫归因 | 按牌追踪待证与错装 | `/desk/exceptions` `/api/stage/exceptions` | `app/modules/exceptions/service.py` | `tests/test_exceptions.py` |
| M5 行李去向对账 | 勾稽计划段与去向件数 | `/` `/api/stage/ledger` | `app/modules/ledger/service.py` | `tests/test_ledger.py` |
| M6 批次留档复跑 | 保存并复算原件 | `/desk/archive` `POST /api/archive` | `app/modules/archive/service.py` `app/modules/archive/store.py` | `tests/test_archive.py` |
| M7 联程证据签发 | 验摘要并下载联程证据 | `/desk/evidence` `/api/evidence/<run>/export` | `app/modules/evidence/bundle.py` | `tests/test_evidence.py` |
| M8 查找工单派查 | 候选点及时派查、按点撤回、双核实结单 | `/desk/orders` `/api/orders` | `app/modules/orders/service.py` `app/modules/orders/store.py` `app/modules/orders/routes.py` | `tests/test_orders.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 脏扫描首列 BOM | 扫描号丢失 | 解码时去首 BOM | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_bom` |
| TRAP-04 | 脏扫描第 16 行重复表头 | 误计为扫描 | 计独立分段 | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_sections` |
| TRAP-05 | E17 多余分隔列 | 宽度误收 | 原行隔离 | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_bad_width` |
| TRAP-07 | E18 时间 N/A | 默认零时 | 空值隔离 | `app/modules/scans/normalize.py` | `tests/test_scans.py::test_sentinel` |
| TRAP-08 | 清单 000101 | 强转丢零 | 字符串关联 | `app/modules/legs/contracts.py` | `tests/test_legs.py::test_tag_preserved` |
| TRAP-11 | E03 特殊偏移重复 | 误认成不同事件 | UTC 后比较 | `app/modules/scans/normalize.py` | `tests/test_scans.py::test_equivalent_offset_duplicate` |
| TRAP-13 | E02 迟到撤下 | 收件顺序末态错误 | 标迟到后按生效回放 | `app/modules/replay/radio.py` | `tests/test_replay.py::test_late_offload_before_reload` |
| TRAP-14 | E12 同键相反动作 | 后写覆盖 | 全组隔离 | `app/modules/scans/service.py` | `tests/test_scans.py::test_conflicting_id` |
| TRAP-23 | E04 恰逢跨日换窗 | 同时匹配两段 | 左闭右开 | `app/modules/legs/service.py` | `tests/test_legs.py::test_boundary_handoff` |

## 对账规则

脏样例清单 12 = 3 已装 + 8 待证 + 1 明确未装，错装 2 件另列。扫描原件记录 24 = 17 采信 + 4 隔离 + 1 重报 + 1 重复表头 + 1 空行。

## 查找工单规则（M8）

- 航段归属拿不准（`LEG_AMBIGUOUS`/`LEG_MISSING`）的扫件在回放 `pending` 中保持 `PENDING`/待核，候选航段不派查。
- 派查点资格 = 回放末态 `LOADED` 且航段 `end`（到站时刻）不晚于当前 UTC；可用请求参数 `now` 或 `app.clock.now` 注入时间。各候选点独立放行，不等另一处核齐。
- 迟到撤下按生效时刻回放；重算后某点末态不再是 `LOADED` 时，`POST /api/orders/<id>/reconcile` 只把该点 target 置 `withdrawn`，其他 `dispatched` 点不动。
- 找到回报必须同时核实行李牌（`tag_verified`）与实物特征（`feature_verified`）才触发结单。结单用条件 `UPDATE ... WHERE status!='closed'` 保证并发下只有一个调用拿到 `closed=true`，整张工单只结一次。
- 工单与派查点以 `(run_id, tag)`、`(order_id, leg)` 唯一；归档重跑得到同一 run id，重复生成/派查为幂等 no-op，已结单工单不再派查。工单状态存于同一 SQLite 的 `wo_*` 表，不进入归档结果散列。
- 页面 `/desk/orders` 的表单走原生 POST→303→GET，无脚本也能完成生成、派查、撤回、双核实回报与结单。

## 已知范围

文件内容通过限长 JSON 提交。样例为机场单站观测，时间窗由机场清单提供。扫描尾播放已采信记录的收件流。
