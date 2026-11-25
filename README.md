# 版本化子批放行账

纯 Python 标准库实现，无第三方依赖。核心模块是
[`subbatch_ledger.py`](./subbatch_ledger.py)，[`solution.py`](./solution.py)
是等价 CLI 入口。

## 1. 判定口径

一本账内固定：

- 子批及其抽样计划；
- 每个子批的抽样框；
- 子批之间的有向关联 `上游 -> 下游`；
- 包装 ID 到唯一实际受检子批的归属。

检测结果使用：

- `normal` / `正常`
- `ordinary_defect` / `普通缺陷`
- `severe_defect` / `严重缺陷`

判定规则：

1. 同一包装只要有任意有效严重项，就形成 **一份** 严重包装证据；同包装多个严重项不重复计证据，但全部原始严重检测项保留在审计依据中。
2. 同一包装只要有任意有效普通缺陷项，就计 **一件** 本批普通缺陷；同包装多个普通项只计一件。
3. 覆盖数只统计该子批抽样框内、至少有一项有效检测的不同包装；传播来的包装不计覆盖。
4. 传播阈值和拒收阈值均比较该子批当前持有的去重严重包装证据数（含本地实际检测和有效传播证据）。
5. 持有严重证据数达到传播阈值的子批，把其全部严重包装证据发给所有后继；传播与该子批自身是否拒收相互独立。
6. 严重证据达到拒收阈值即 `rejected / 拒收`。
7. 未拒收时，只有同时满足：
   - 抽样框完整；
   - 覆盖数达到抽样计划要求；
   - 普通缺陷包装数不超过限值；

   才 `released / 放行`，否则为 `insufficient_evidence / 证据不足`。

> “抽样框完整”在题目中没有给出额外推导公式，因此建模为抽样框上的固定布尔属性 `complete` / `frame_complete`，默认 `true`。

## 2. 快速使用

```python
from subbatch_ledger import ReleaseLedger

ledger = ReleaseLedger(
    subbatches=[
        {
            "subbatch_id": "A",
            "sampling_frame": ["A1", "A2"],
            "propagation_threshold": 1,
            "rejection_threshold": 1,
            "required_coverage": 2,
            "max_ordinary_defects": 0,
        },
        {
            "subbatch_id": "B",
            "sampling_frame": ["B1"],
            "propagation_threshold": 1,
            "rejection_threshold": 1,
            "required_coverage": 1,
            "max_ordinary_defects": 0,
        },
    ],
    edges=[("A", "B")],
    initial_items=[
        {"id": "I1", "package_id": "A1", "result": "severe_defect"},
        {"id": "I2", "package_id": "A2", "result": "normal"},
        {"id": "I3", "package_id": "B1", "result": "normal"},
    ],
)

print(ledger.query(1, "B").decision)       # rejected

publish = ledger.publish_revision(withdraw=["I1"])
print(publish.changed_subbatches)           # ('A', 'B')
print(ledger.query(2, "B").decision)        # released
print(ledger.query(1, "B").decision)        # rejected，旧版不变
```

整批校验失败时使用：

```python
result = ledger.try_publish_revision(
    add=[{"id": "I9", "package_id": "A2", "result": "normal"}],
    withdraw=["NOT_VALID"],
)
assert result.accepted is False
assert ledger.current_version == 1
```

也可以调用会抛 `RevisionRejected` 的 `publish_revision(...)`。所有校验都在修改状态前完成，因此无效批次不会部分生效。

## 3. 版本存储与增量重算

- 版本 1 是初始判定版，保存所有子批的不可变 `NodeRecord`。
- 每次成功修订得到连续新版本。
- 修订先找到被修改检测项实际归属的子批，再用 BFS 求这些根节点的 **有向可达下游集合**。
- 新版本只为该集合中的子批追加 `NodeRecord`；未受影响子批不重算、不追加、不复制。
- 查询 `(version, subbatch_id)` 时，在该子批的 append-only 记录上二分查找 `record.version <= version` 的最新记录。
- 检测项不物理删除：增补项记录 `added_version`，撤回记录 `withdrawn_at[item_id] = version`。旧版审计视图按所查询版本呈现当时的有效性，未来撤回不会改变旧结果。

### 区域外证据

重算受影响集合时，对每个受影响节点检查其直接前驱：

- 前驱也在受影响集合内：参与本次最小不动点传播；
- 前驱不在受影响集合内：它不可能被本次修订改变，直接读取其当前不可变记录；若它达到传播阈值，就把其严重包装集合作为边界证据并入。

因此区域外传入证据不会丢失。

### 环和多路径

传播从本地有效严重项和区域外有效边界事实开始，迭代到最小不动点：

```text
E(v) = 本地严重包装 ∪ 合格区域外前驱传入
       ∪ ⋃ { E(u) | u -> v 且 |E(u)| >= propagation_threshold(u) }
```

- 包装 ID 始终放在集合中，多条路径只保留一次；
- 不按路径复制证据对象，只在接收节点保存直接前驱支持集合；
- 空环从空集合开始，不会自我维持；
- 撤回唯一实际来源后重算，环中残留的旧传播结果不会被当作输入。

随机差分测试会把每次增量账的当前有效检测项重新建成一本全新账，并逐子批比较判定、覆盖、普通缺陷、严重包装、本地依据和传播支持关系。

## 4. 变化报告

`publish_revision()` 返回 `PublishResult`，其中：

- `decision_changed_subbatches`
- `severe_basis_changed_subbatches`
- `changed_subbatches`：判定或严重依据变化的并集
- `changes`：每个受影响子批的前后详细字段

严重依据比较的是：

```python
frozenset(
    item_id
    for package_id in record.severe_packages
    for item_id in origin_record.local_severe_items[package_id]
)
```

即所有可达严重包装背后的 **原始严重检测项 ID 集合**。因此：

- 同包装撤回一个严重项但还有另一个严重项：包装证据仍在，但严重依据集合变化，会报告依据变化；
- 传播路径或遍历顺序变化但原始严重项集合不变：不报严重依据变化。

## 5. 查询审计结构

`ledger.query(version, subbatch_id)` 返回不可变 dataclass，`query_dict(...)` / `.to_dict()` 可直接 JSON 序列化。

每个严重包装证据包含：

- `package_id`：去重后的包装；
- `origin_subbatch_id` / `inspected_subbatch_id`：唯一实际受检子批；
- `severe_items`：该包装背后可核对的全部原始严重检测项；
- `direct_predecessors`：在查询版本中直接把该包装传入本子批的前驱；
- `propagation_edges`：逆向展开支持图得到的传播边，并附上游当时的严重证据数和传播阈值；
- `propagating_subbatches`：传播链上的相关子批；
- `source_found`：是否回溯到了实际本地严重检测来源。

同时返回：

- `decision` / `decision_zh`
- `coverage`、`required_coverage`、`coverage_met`、`covered_packages`
- `ordinary_defects`、`max_ordinary_defects`、`ordinary_limit_met`
- `ordinary_defect_packages` 及其检测项
- `frame_complete`
- 两个阈值和 `severe_evidence_count`

传播审计保存的是支持 DAG/图，而不是枚举所有指数级路径；调用方可依据边集合复核所有有效路径。

## 6. JSON CLI

示例请求见 [`examples/request.json`](./examples/request.json)：

```bash
python3 -I /workspace/subbatch_ledger.py \
  /workspace/examples/request.json \
  --output /workspace/examples/output.json
```

普通环境中也可使用等价入口 `python3 /workspace/solution.py ...`。

顶层 JSON 支持：

```json
{
  "subbatches": [],
  "sampling_plans": [],
  "sampling_frames": [],
  "package_owners": {},
  "edges": [],
  "initial_items": [],
  "revisions": [
    {"add": [], "withdraw": [], "note": "可选"}
  ],
  "queries": [
    {"version": 1, "subbatch_id": "A"}
  ]
}
```

`sampling_plans` 和 `sampling_frames` 也支持以子批 ID 为键的对象形式。

## 7. 测试

```bash
python3 /workspace/test_subbatch_ledger.py -v
python3 /workspace/differential_check.py
```

当前覆盖：

- 覆盖和普通缺陷按包装去重；
- 拒收阈值；
- 单路/多路径传播与审计；
- 环不能维持无来源证据；
- 撤回来源后的下游变化；
- 区域外边界证据；
- 同包装多严重项撤一留一；
- 无效整批修订原子拒绝；
- 旧版查询不可变；
- 未受影响子批不追加新版本记录；
- JSON 请求端到端；
- 200 本随机账、每本 25 次修订的从头推导差分检查。
