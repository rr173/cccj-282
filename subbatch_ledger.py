"""可按版本查询的子批放行账。

本模块只依赖 Python 标准库。核心不变量：

* 抽样计划、抽样框、子批有向关联和包装归属在一本账内固定；
* 检测项只增补、不物理删除，撤回通过 ``withdrawn_at_version`` 表示；
* 每个发布版本只追加被修改检测所在子批及其静态可达下游的节点记录；
* 严重证据始终以包装 ID 去重，并可回溯到唯一实际受检子批上的原始严重检测项。

正式判定口径：

* 传播/拒收阈值比较的是该子批当前持有的去重严重包装证据数；
* 普通缺陷按“存在有效普通缺陷项的本批包装数”计数，同一包装多项只计一件；
* 覆盖只统计固定抽样框内至少有一项有效检测的不同包装。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

NORMAL = "normal"
ORDINARY_DEFECT = "ordinary_defect"
SEVERE_DEFECT = "severe_defect"

RESULT_ALIASES: dict[str, str] = {
    "normal": NORMAL,
    "ok": NORMAL,
    "pass": NORMAL,
    "qualified": NORMAL,
    "合格": NORMAL,
    "正常": NORMAL,
    "ordinary": ORDINARY_DEFECT,
    "ordinary_defect": ORDINARY_DEFECT,
    "minor": ORDINARY_DEFECT,
    "minor_defect": ORDINARY_DEFECT,
    "普通": ORDINARY_DEFECT,
    "普通缺陷": ORDINARY_DEFECT,
    "一般缺陷": ORDINARY_DEFECT,
    "severe": SEVERE_DEFECT,
    "severe_defect": SEVERE_DEFECT,
    "critical": SEVERE_DEFECT,
    "critical_defect": SEVERE_DEFECT,
    "major_defect": SEVERE_DEFECT,
    "严重": SEVERE_DEFECT,
    "严重缺陷": SEVERE_DEFECT,
    "致命缺陷": SEVERE_DEFECT,
}

REJECTED = "rejected"
RELEASED = "released"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

DECISION_ZH = {
    REJECTED: "拒收",
    RELEASED: "放行",
    INSUFFICIENT_EVIDENCE: "证据不足",
}

RESULT_ZH = {
    NORMAL: "正常",
    ORDINARY_DEFECT: "普通缺陷",
    SEVERE_DEFECT: "严重缺陷",
}


class LedgerError(Exception):
    """账本相关错误基类。"""


class LedgerConfigError(LedgerError):
    """固定主数据不合法。"""


class RevisionRejected(LedgerError):
    """整批修订无效；异常中给出全部校验错误。"""

    def __init__(self, errors: str | Sequence[str]):
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


def normalize_result(value: Any) -> str:
    key = str(value).strip().lower()
    try:
        return RESULT_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"未知检测结果: {value!r}") from exc


def _require_int(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerConfigError(f"{name} 必须是整数，得到 {value!r}")
    if positive and value <= 0:
        raise LedgerConfigError(f"{name} 必须为正整数，得到 {value}")
    if nonnegative and value < 0:
        raise LedgerConfigError(f"{name} 不能为负数，得到 {value}")
    return value


_MISSING = object()


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = _MISSING) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    if default is _MISSING:
        raise KeyError(f"缺少字段，候选名: {', '.join(names)}")
    return default


def _index_named(value: Any) -> dict[str, dict[str, Any]]:
    """把 key->dict 或带 id 的 dict 列表统一成 id->dict。"""
    if value is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping):
                row = dict(item)
                row.setdefault("subbatch_id", key)
                row.setdefault("id", key)
                result[str(key)] = row
            elif isinstance(item, (list, tuple, set, frozenset)):
                result[str(key)] = {"subbatch_id": str(key), "packages": list(item)}
            else:
                result[str(key)] = {"subbatch_id": str(key), "value": item}
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if not isinstance(item, Mapping):
                raise LedgerConfigError(f"无法识别的命名配置: {item!r}")
            sid = str(_first(item, ["subbatch_id", "id", "batch_id"]))
            result[sid] = dict(item)
        return result
    raise LedgerConfigError(f"无法识别的命名配置集合: {value!r}")


@dataclass(frozen=True)
class SamplingPlan:
    """抽样方案及本批判定阈值。"""

    propagation_threshold: int
    rejection_threshold: int
    required_coverage: int
    max_ordinary_defects: int = 0

    def __post_init__(self) -> None:
        _require_int(self.propagation_threshold, "propagation_threshold", positive=True)
        _require_int(self.rejection_threshold, "rejection_threshold", positive=True)
        _require_int(self.required_coverage, "required_coverage", nonnegative=True)
        _require_int(self.max_ordinary_defects, "max_ordinary_defects", nonnegative=True)


@dataclass(frozen=True)
class SamplingFrame:
    """固定抽样框；complete 表示抽样框本身是否完整。"""

    packages: frozenset[str]
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "packages", frozenset(str(p) for p in self.packages))
        if not isinstance(self.complete, bool):
            raise LedgerConfigError("sampling_frame.complete 必须是布尔值")


@dataclass(frozen=True)
class SubbatchSpec:
    subbatch_id: str
    plan: SamplingPlan
    frame: SamplingFrame


@dataclass(frozen=True)
class NewInspection:
    item_id: str
    package_id: str
    result: str


@dataclass(frozen=True)
class InspectionItem:
    item_id: str
    package_id: str
    result: str
    added_version: int


@dataclass
class _PackageState:
    valid_item_ids: set[str] = field(default_factory=set)
    severe_item_ids: set[str] = field(default_factory=set)
    ordinary_item_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class NodeRecord:
    """某子批在某个发布版本中的不可变推导记录。"""

    version: int
    subbatch_id: str
    decision: str
    coverage: int
    ordinary_defects: int
    severe_packages: frozenset[str]
    covered_packages: frozenset[str]
    local_severe_items: Mapping[str, frozenset[str]]
    local_ordinary_items: Mapping[str, frozenset[str]]
    support: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class ItemView:
    item_id: str
    package_id: str
    inspected_subbatch_id: str
    result: str
    result_zh: str
    added_version: int
    withdrawn_version: int | None


@dataclass(frozen=True)
class PropagationEdgeView:
    from_subbatch_id: str
    to_subbatch_id: str
    from_severe_evidence_count: int
    propagation_threshold: int
    threshold_met: bool


@dataclass(frozen=True)
class SevereEvidenceView:
    package_id: str
    origin_subbatch_id: str
    inspected_subbatch_id: str
    source_found: bool
    severe_items: tuple[ItemView, ...]
    direct_predecessors: tuple[str, ...]
    propagation_edges: tuple[PropagationEdgeView, ...]
    propagating_subbatches: tuple[str, ...]


@dataclass(frozen=True)
class OrdinaryPackageView:
    package_id: str
    inspected_subbatch_id: str
    ordinary_items: tuple[ItemView, ...]


@dataclass(frozen=True)
class SubbatchQueryView:
    version: int
    subbatch_id: str
    decision: str
    decision_zh: str
    severe_evidence_count: int
    severe_packages: tuple[SevereEvidenceView, ...]
    coverage: int
    required_coverage: int
    coverage_met: bool
    covered_packages: tuple[str, ...]
    frame_complete: bool
    ordinary_defects: int
    max_ordinary_defects: int
    ordinary_limit_met: bool
    ordinary_defect_packages: tuple[OrdinaryPackageView, ...]
    propagation_threshold: int
    rejection_threshold: int

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


@dataclass(frozen=True)
class NodeChange:
    subbatch_id: str
    decision_before: str
    decision_after: str
    decision_changed: bool
    severe_basis_before: frozenset[str]
    severe_basis_after: frozenset[str]
    severe_basis_changed: bool
    severe_packages_before: frozenset[str]
    severe_packages_after: frozenset[str]
    coverage_before: int
    coverage_after: int
    ordinary_defects_before: int
    ordinary_defects_after: int

    @property
    def basis_or_decision_changed(self) -> bool:
        return self.decision_changed or self.severe_basis_changed


@dataclass(frozen=True)
class PublishResult:
    accepted: bool
    version: int
    published: bool
    kind: str = "revision"
    added_item_ids: tuple[str, ...] = ()
    withdrawn_item_ids: tuple[str, ...] = ()
    affected_subbatches: tuple[str, ...] = ()
    changes: tuple[NodeChange, ...] = ()
    errors: tuple[str, ...] = ()
    note: str | None = None

    @property
    def decision_changed_subbatches(self) -> tuple[str, ...]:
        return tuple(c.subbatch_id for c in self.changes if c.decision_changed)

    @property
    def severe_basis_changed_subbatches(self) -> tuple[str, ...]:
        return tuple(c.subbatch_id for c in self.changes if c.severe_basis_changed)

    @property
    def changed_subbatches(self) -> tuple[str, ...]:
        """判定或严重依据至少一项变化的子批。"""
        return tuple(c.subbatch_id for c in self.changes if c.basis_or_decision_changed)

    def to_dict(self) -> dict[str, Any]:
        data = to_plain(self)
        data["decision_changed_subbatches"] = list(self.decision_changed_subbatches)
        data["severe_basis_changed_subbatches"] = list(self.severe_basis_changed_subbatches)
        data["changed_subbatches"] = list(self.changed_subbatches)
        return data


def to_plain(obj: Any) -> Any:
    """转换成 JSON 友好结构；集合稳定排序。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        values = [to_plain(v) for v in obj]
        try:
            return sorted(values)
        except TypeError:
            return sorted(values, key=lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True))
    return obj


def spec_from_dict(
    raw: SubbatchSpec | Mapping[str, Any],
    *,
    plan_override: Mapping[str, Any] | None = None,
    frame_override: Mapping[str, Any] | Sequence[Any] | None = None,
    fallback_id: str | None = None,
) -> SubbatchSpec:
    if isinstance(raw, SubbatchSpec):
        return raw

    data = dict(raw)
    sid = data.get("subbatch_id") or data.get("id") or data.get("batch_id") or fallback_id
    if sid is None:
        raise LedgerConfigError(f"子批缺少 ID: {raw!r}")
    sid = str(sid)

    plan_data: dict[str, Any] = {}
    for source in (data.get("sampling_plan"), data.get("plan"), plan_override):
        if isinstance(source, Mapping):
            plan_data.update(source)
    # 扁平字段也可直接写在子批对象上。
    plan_sources = (data, plan_data)

    propagation = None
    rejection = None
    required_coverage = None
    max_ordinary = 0
    for names, target_name in [
        (["propagation_threshold", "propagate_threshold", "severe_propagation_threshold"], "prop"),
        (["rejection_threshold", "reject_threshold", "severe_rejection_threshold"], "rej"),
        (["required_coverage", "coverage_threshold", "min_coverage", "sample_size", "required_samples"], "cov"),
        (["max_ordinary_defects", "ordinary_defect_limit", "max_normal_defects", "accept_ordinary", "ac"], "ord"),
    ]:
        value = _MISSING
        for source in plan_sources:
            try:
                value = _first(source, names)
                break
            except KeyError:
                continue
        if value is _MISSING:
            if target_name == "ord":
                value = 0
            elif target_name == "cov":
                value = None
            else:
                raise LedgerConfigError(f"子批 {sid} 缺少 {names[0]}")
        if target_name == "prop":
            propagation = value
        elif target_name == "rej":
            rejection = value
        elif target_name == "cov":
            required_coverage = value
        else:
            max_ordinary = value

    frame_data: Any = None
    for candidate in (frame_override, data.get("sampling_frame"), data.get("frame")):
        if candidate is not None:
            frame_data = candidate
            break
    if frame_data is None:
        packages = data.get("packages") or data.get("frame_packages") or ()
        frame_complete = data.get("frame_complete", data.get("complete", True))
    elif isinstance(frame_data, Mapping):
        packages = (
            frame_data.get("packages")
            or frame_data.get("package_ids")
            or frame_data.get("sampled_packages")
            or ()
        )
        frame_complete = frame_data.get(
            "complete",
            frame_data.get(
                "frame_complete",
                frame_data.get("is_complete", data.get("frame_complete", data.get("complete", True))),
            ),
        )
    elif isinstance(frame_data, (list, tuple, set, frozenset)):
        packages = frame_data
        frame_complete = data.get("frame_complete", data.get("complete", True))
    else:
        raise LedgerConfigError(f"子批 {sid} 的抽样框格式无法识别: {frame_data!r}")

    if required_coverage is None:
        fraction = _MISSING
        for source in plan_sources:
            try:
                fraction = _first(source, ["coverage_fraction", "coverage_rate", "required_coverage_rate"])
                break
            except KeyError:
                continue
        if fraction is not _MISSING:
            fraction = float(fraction)
            if not 0 <= fraction <= 1:
                raise LedgerConfigError(f"子批 {sid} coverage_fraction 必须在 [0,1]")
            required_coverage = math.ceil(len(packages) * fraction)
        else:
            # 未另给覆盖标准时，以完整抽样框为严格默认值。
            required_coverage = len(packages)

    plan = SamplingPlan(
        propagation_threshold=propagation,
        rejection_threshold=rejection,
        required_coverage=required_coverage,
        max_ordinary_defects=max_ordinary,
    )
    frame = SamplingFrame(packages=frozenset(packages), complete=bool(frame_complete))
    return SubbatchSpec(subbatch_id=sid, plan=plan, frame=frame)


def _coerce_new_item(raw: NewInspection | Mapping[str, Any] | Sequence[Any]) -> NewInspection:
    if isinstance(raw, NewInspection):
        return raw
    if isinstance(raw, Mapping):
        item_id = _first(raw, ["item_id", "id", "inspection_item_id"])
        package_id = _first(raw, ["package_id", "package", "packaging_id"])
        result = normalize_result(_first(raw, ["result", "outcome", "defect_type", "level"]))
        return NewInspection(str(item_id), str(package_id), result)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 3:
        return NewInspection(str(raw[0]), str(raw[1]), normalize_result(raw[2]))
    raise ValueError(f"无法识别的检测项: {raw!r}")


def _coerce_package_owners(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for package_id, owner in value.items():
            if isinstance(owner, Mapping):
                owner_id = _first(owner, ["subbatch_id", "id", "owner", "subbatch"])
                result[str(package_id)] = str(owner_id)
            else:
                result[str(package_id)] = str(owner)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if all(isinstance(row, str) for row in value):
            return None  # 仅包名清单时，归属由各子批抽样框确定。
        result = {}
        for row in value:
            if not isinstance(row, Mapping):
                raise LedgerConfigError(f"无法识别的包装归属配置: {row!r}")
            package_id = _first(row, ["package_id", "package", "id"])
            owner = _first(row, ["subbatch_id", "owner_subbatch_id", "owner", "subbatch"])
            result[str(package_id)] = str(owner)
        return result
    raise LedgerConfigError("package_owners 必须是映射或对象列表")


class ReleaseLedger:
    """版本化子批放行账。

    版本 1 为初始账。之后每次成功的成批修订得到连续整数版本号。
    """

    def __init__(
        self,
        subbatches: Sequence[SubbatchSpec | Mapping[str, Any]],
        edges: Sequence[tuple[str, str] | Mapping[str, Any]] | None = None,
        initial_items: Sequence[NewInspection | Mapping[str, Any]] | None = None,
        package_owners: Mapping[str, str] | None = None,
    ) -> None:
        self.specs: dict[str, SubbatchSpec] = {}
        for raw in subbatches:
            spec = raw if isinstance(raw, SubbatchSpec) else spec_from_dict(raw)
            if spec.subbatch_id in self.specs:
                raise LedgerConfigError(f"重复子批 ID: {spec.subbatch_id}")
            self.specs[spec.subbatch_id] = spec
        if not self.specs:
            raise LedgerConfigError("至少需要一个子批")

        self.package_owner: dict[str, str] = {}
        for package_id, owner in (package_owners or {}).items():
            owner = str(owner)
            if owner not in self.specs:
                raise LedgerConfigError(f"包装 {package_id} 的归属子批 {owner} 不存在")
            self.package_owner[str(package_id)] = owner

        # 抽样框中的包装默认且必须归属于该子批，从而保证包装归属唯一。
        for sid, spec in self.specs.items():
            for package_id in spec.frame.packages:
                old = self.package_owner.get(package_id)
                if old is not None and old != sid:
                    raise LedgerConfigError(
                        f"包装 {package_id} 同时属于抽样框 {sid} 和已声明子批 {old}"
                    )
                self.package_owner[package_id] = sid

        self.successors: dict[str, set[str]] = {sid: set() for sid in self.specs}
        self.predecessors: dict[str, set[str]] = {sid: set() for sid in self.specs}
        for raw in edges or ():
            if isinstance(raw, Mapping):
                source = str(_first(raw, ["from_subbatch_id", "from", "source", "predecessor", "upstream"]))
                target = str(_first(raw, ["to_subbatch_id", "to", "target", "successor", "downstream"]))
            else:
                source, target = str(raw[0]), str(raw[1])
            if source not in self.specs or target not in self.specs:
                raise LedgerConfigError(f"关联 {source}->{target} 引用了不存在的子批")
            self.successors[source].add(target)
            self.predecessors[target].add(source)

        # 固定主数据对外只读。
        self.specs = MappingProxyType(self.specs)  # type: ignore[assignment]
        self.package_owner = MappingProxyType(self.package_owner)  # type: ignore[assignment]
        self.successors = MappingProxyType(  # type: ignore[assignment]
            {sid: frozenset(targets) for sid, targets in self.successors.items()}
        )
        self.predecessors = MappingProxyType(  # type: ignore[assignment]
            {sid: frozenset(sources) for sid, sources in self.predecessors.items()}
        )

        self.items: dict[str, InspectionItem] = {}
        self.withdrawn_at: dict[str, int] = {}
        self.states: dict[str, dict[str, _PackageState]] = {sid: {} for sid in self.specs}
        self.history: dict[str, list[NodeRecord]] = {sid: [] for sid in self.specs}
        self.publishes: dict[int, PublishResult] = {}
        self.current_version = 0

        initial = []
        errors: list[str] = []
        for index, raw in enumerate(initial_items or ()):
            try:
                item = _coerce_new_item(raw)
            except Exception as exc:  # 初始化时聚合可读错误
                errors.append(f"initial_items[{index}]: {exc}")
                continue
            if item.item_id in self.items or any(x.item_id == item.item_id for x in initial):
                errors.append(f"初始检测项 ID 重复: {item.item_id}")
                continue
            if item.package_id not in self.package_owner:
                errors.append(f"检测项 {item.item_id} 的包装 {item.package_id} 未声明归属子批")
                continue
            initial.append(item)
        if errors:
            raise LedgerConfigError("; ".join(errors))

        for new_item in initial:
            item = InspectionItem(
                item_id=new_item.item_id,
                package_id=new_item.package_id,
                result=new_item.result,
                added_version=1,
            )
            self.items[item.item_id] = item
            self._apply_add(item)

        records = self._compute_all_records(version=1)
        for sid in sorted(self.specs):
            self.history[sid].append(records[sid])
        self.current_version = 1
        affected = tuple(sorted(self.specs))
        self.publishes[1] = PublishResult(
            accepted=True,
            version=1,
            published=True,
            kind="initial",
            added_item_ids=tuple(item.item_id for item in initial),
            affected_subbatches=affected,
            note="初始判定版",
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReleaseLedger":
        if not isinstance(data, Mapping):
            raise LedgerConfigError("输入必须是 JSON 对象")

        plans = _index_named(data.get("sampling_plans") or data.get("plans"))
        frames = _index_named(data.get("sampling_frames") or data.get("frames"))
        raw_subbatches = data.get("subbatches") or data.get("sub_batches") or []

        base: dict[str, dict[str, Any]] = {}
        if isinstance(raw_subbatches, Mapping):
            for sid, row in raw_subbatches.items():
                base[str(sid)] = dict(row) if isinstance(row, Mapping) else {"subbatch_id": str(sid)}
        else:
            for row in raw_subbatches:
                if isinstance(row, str):
                    base[row] = {"subbatch_id": row}
                elif isinstance(row, Mapping):
                    sid = str(_first(row, ["subbatch_id", "id", "batch_id"]))
                    base[sid] = dict(row)
                else:
                    raise LedgerConfigError(f"无法识别的子批配置: {row!r}")

        all_ids = set(base) | set(plans) | set(frames)
        for edge in data.get("edges") or data.get("associations") or data.get("relations") or ():
            if isinstance(edge, Mapping):
                all_ids.add(str(_first(edge, ["from", "source", "predecessor", "upstream"])))
                all_ids.add(str(_first(edge, ["to", "target", "successor", "downstream"])))
            else:
                all_ids.add(str(edge[0]))
                all_ids.add(str(edge[1]))

        specs: list[SubbatchSpec] = []
        for sid in sorted(all_ids):
            merged = dict(base.get(sid, {}))
            merged.setdefault("subbatch_id", sid)
            specs.append(
                spec_from_dict(
                    merged,
                    plan_override=plans.get(sid),
                    frame_override=frames.get(sid),
                    fallback_id=sid,
                )
            )

        return cls(
            subbatches=specs,
            edges=data.get("edges") or data.get("associations") or data.get("relations") or (),
            initial_items=(
                data.get("initial_items")
                or data.get("items")
                or data.get("inspection_items")
                or data.get("detections")
                or ()
            ),
            package_owners=_coerce_package_owners(
                data.get("package_owners") or data.get("packages")
            ),
        )

    # ---------- 状态变更 ----------

    def _package_state(self, node: str, package_id: str, *, create: bool = False) -> _PackageState | None:
        states = self.states[node]
        state = states.get(package_id)
        if state is None and create:
            state = _PackageState()
            states[package_id] = state
        return state

    def _apply_add(self, item: InspectionItem) -> None:
        owner = self.package_owner[item.package_id]
        state = self._package_state(owner, item.package_id, create=True)
        assert state is not None
        state.valid_item_ids.add(item.item_id)
        if item.result == SEVERE_DEFECT:
            state.severe_item_ids.add(item.item_id)
        elif item.result == ORDINARY_DEFECT:
            state.ordinary_item_ids.add(item.item_id)

    def _apply_withdraw(self, item_id: str, version: int) -> None:
        item = self.items[item_id]
        owner = self.package_owner[item.package_id]
        state = self._package_state(owner, item.package_id)
        if state is None:
            raise LedgerError(f"内部错误：检测项 {item_id} 没有包装状态")
        state.valid_item_ids.discard(item_id)
        state.severe_item_ids.discard(item_id)
        state.ordinary_item_ids.discard(item_id)
        self.withdrawn_at[item_id] = version

    def _parse_revision(
        self,
        add: Sequence[NewInspection | Mapping[str, Any] | Sequence[Any]] | None,
        withdraw: Sequence[str] | None,
    ) -> tuple[list[NewInspection], list[str], list[str]]:
        additions: list[NewInspection] = []
        withdrawal_ids: list[str] = []
        errors: list[str] = []

        seen_add_ids: set[str] = set()
        for index, raw in enumerate(add or ()):
            try:
                item = _coerce_new_item(raw)
            except Exception as exc:
                errors.append(f"add[{index}]: {exc}")
                continue
            if item.item_id in seen_add_ids:
                errors.append(f"同一修订中增补 ID 重复: {item.item_id}")
            if item.item_id in self.items:
                errors.append(f"增补 ID 已存在（检测项 ID 不可复用）: {item.item_id}")
            if item.package_id not in self.package_owner:
                errors.append(f"add[{index}] 的包装 {item.package_id} 未声明归属子批")
            seen_add_ids.add(item.item_id)
            additions.append(item)

        seen_withdraw: set[str] = set()
        for index, raw_id in enumerate(withdraw or ()):
            item_id = str(raw_id)
            if item_id in seen_withdraw:
                errors.append(f"同一修订中撤回 ID 重复: {item_id}")
            if item_id not in self.items:
                errors.append(f"撤回不存在的检测项: {item_id}")
            elif item_id in self.withdrawn_at:
                errors.append(f"检测项 {item_id} 已撤回，不能重复撤回")
            if item_id in seen_add_ids:
                errors.append(f"检测项 {item_id} 不能在同一修订中既增补又撤回")
            seen_withdraw.add(item_id)
            withdrawal_ids.append(item_id)

        return additions, withdrawal_ids, errors

    def try_publish_revision(
        self,
        add: Sequence[NewInspection | Mapping[str, Any] | Sequence[Any]] | None = None,
        withdraw: Sequence[str] | None = None,
        *,
        note: str | None = None,
    ) -> PublishResult:
        """不抛 RevisionRejected 的包装；失败结果中包含全部错误。"""
        try:
            return self.publish_revision(add, withdraw, note=note)
        except RevisionRejected as exc:
            return PublishResult(
                accepted=False,
                version=self.current_version,
                published=False,
                errors=tuple(exc.errors),
                note=note,
            )

    def publish_revision(
        self,
        add: Sequence[NewInspection | Mapping[str, Any] | Sequence[Any]] | None = None,
        withdraw: Sequence[str] | None = None,
        *,
        note: str | None = None,
    ) -> PublishResult:
        additions, withdrawal_ids, errors = self._parse_revision(add, withdraw)
        if errors:
            # 关键：任何状态修改之前完成整批校验。
            raise RevisionRejected(errors)

        if not additions and not withdrawal_ids:
            current_result = self.publishes[self.current_version]
            return PublishResult(
                accepted=True,
                version=self.current_version,
                published=False,
                kind="empty",
                note=note,
                affected_subbatches=current_result.affected_subbatches,
            )

        old_version = self.current_version
        new_version = old_version + 1
        roots: set[str] = set()
        for item in additions:
            roots.add(self.package_owner[item.package_id])
        for item_id in withdrawal_ids:
            roots.add(self.package_owner[self.items[item_id].package_id])

        affected = self._reachable_downstream(roots)
        ordered_affected = tuple(sorted(affected))

        # 只快照实际修改的本批包装状态，用于罕见发布异常时回滚。
        state_snapshot = {
            sid: {
                p: _PackageState(
                    set(state.valid_item_ids),
                    set(state.severe_item_ids),
                    set(state.ordinary_item_ids),
                )
                for p, state in self.states[sid].items()
            }
            for sid in roots
        }
        history_length = {sid: len(self.history[sid]) for sid in ordered_affected}
        applied_added: list[str] = []
        applied_withdrawn: list[str] = []
        try:
            for new_item in additions:
                item = InspectionItem(
                    item_id=new_item.item_id,
                    package_id=new_item.package_id,
                    result=new_item.result,
                    added_version=new_version,
                )
                self.items[item.item_id] = item
                self._apply_add(item)
                applied_added.append(item.item_id)

            for item_id in withdrawal_ids:
                self._apply_withdraw(item_id, new_version)
                applied_withdrawn.append(item_id)

            old_records = {sid: self._latest_record(sid) for sid in affected}
            held, support = self._propagate(ordered_affected, boundary=True)
            new_records = {
                sid: self._build_record(sid, new_version, held[sid], support[sid])
                for sid in ordered_affected
            }

            changes: list[NodeChange] = []
            for sid in ordered_affected:
                old_rec = old_records[sid]
                new_rec = new_records[sid]
                basis_before = self._canonical_basis(old_rec, old_version)
                basis_after = self._canonical_basis(new_rec, new_version, candidate=new_records)
                changes.append(
                    NodeChange(
                        subbatch_id=sid,
                        decision_before=old_rec.decision,
                        decision_after=new_rec.decision,
                        decision_changed=old_rec.decision != new_rec.decision,
                        severe_basis_before=basis_before,
                        severe_basis_after=basis_after,
                        severe_basis_changed=basis_before != basis_after,
                        severe_packages_before=old_rec.severe_packages,
                        severe_packages_after=new_rec.severe_packages,
                        coverage_before=old_rec.coverage,
                        coverage_after=new_rec.coverage,
                        ordinary_defects_before=old_rec.ordinary_defects,
                        ordinary_defects_after=new_rec.ordinary_defects,
                    )
                )

            # 所有新记录和差异构造成功后才追加不可变历史。
            for sid in ordered_affected:
                self.history[sid].append(new_records[sid])

            result = PublishResult(
                accepted=True,
                version=new_version,
                published=True,
                kind="revision",
                added_item_ids=tuple(item.item_id for item in additions),
                withdrawn_item_ids=tuple(withdrawal_ids),
                affected_subbatches=ordered_affected,
                changes=tuple(changes),
                note=note,
            )
            self.publishes[new_version] = result
            self.current_version = new_version
            return result
        except Exception:
            for sid, length in history_length.items():
                del self.history[sid][length:]
            for sid, snapshot in state_snapshot.items():
                self.states[sid] = snapshot
            for item_id in applied_added:
                self.items.pop(item_id, None)
            for item_id in applied_withdrawn:
                self.withdrawn_at.pop(item_id, None)
            raise

    # ---------- 推导核心 ----------

    def _reachable_downstream(self, roots: set[str]) -> frozenset[str]:
        """只遍历修改检测所在子批的有向可达下游，不扫描全图。"""
        seen: set[str] = set(roots)
        queue: deque[str] = deque(sorted(roots))
        while queue:
            sid = queue.popleft()
            for target in sorted(self.successors[sid]):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return frozenset(seen)

    def _initial_held_support(self, nodes: Sequence[str]) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
        held: dict[str, set[str]] = {}
        support: dict[str, dict[str, set[str]]] = {}
        for sid in nodes:
            held[sid] = {
                package_id
                for package_id, state in self.states[sid].items()
                if state.severe_item_ids
            }
            support[sid] = {}
        return held, support

    def _compute_all_records(self, version: int) -> dict[str, NodeRecord]:
        nodes = tuple(sorted(self.specs))
        held, support = self._initial_held_support(nodes)
        self._propagate_into(nodes, held, support, boundary=False)
        return {sid: self._build_record(sid, version, held[sid], support[sid]) for sid in nodes}

    def _propagate(
        self, affected: frozenset[str] | Sequence[str], *, boundary: bool
    ) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
        nodes = tuple(sorted(affected))
        held, support = self._initial_held_support(nodes)
        self._propagate_into(nodes, held, support, boundary=boundary)
        return held, support

    def _propagate_into(
        self,
        nodes: Sequence[str],
        held: dict[str, set[str]],
        support: dict[str, dict[str, set[str]]],
        *,
        boundary: bool,
    ) -> None:
        node_set = set(nodes)

        if boundary:
            # 区域外前驱不会被本次修订改变；其当前不可变记录可直接作为边界事实。
            for sid in nodes:
                for upstream in sorted(self.predecessors[sid]):
                    if upstream in node_set:
                        continue
                    upstream_record = self._latest_record(upstream)
                    threshold = self.specs[upstream].plan.propagation_threshold
                    if len(upstream_record.severe_packages) >= threshold:
                        held[sid].update(upstream_record.severe_packages)
                        package_support = support[sid]
                        for package_id in upstream_record.severe_packages:
                            package_support.setdefault(package_id, set()).add(upstream)

        # 最小不动点：证据只能从实际本批检测或区域外有效边界事实生长。
        # 因此空环不会自我维持；同一 package_id 始终在 set 中去重。
        while True:
            added_new_evidence = False
            for upstream in nodes:
                evidence = held[upstream]
                threshold = self.specs[upstream].plan.propagation_threshold
                if len(evidence) < threshold:
                    continue
                for downstream in sorted(self.successors[upstream]):
                    if downstream not in node_set or downstream == upstream:
                        continue
                    target_held = held[downstream]
                    target_support = support[downstream]
                    for package_id in evidence:
                        target_support.setdefault(package_id, set()).add(upstream)
                        if package_id not in target_held:
                            target_held.add(package_id)
                            added_new_evidence = True
            if not added_new_evidence:
                return

    def _decide(
        self, spec: SubbatchSpec, severe_count: int, coverage: int, ordinary_defects: int
    ) -> str:
        plan = spec.plan
        if severe_count >= plan.rejection_threshold:
            return REJECTED
        if (
            spec.frame.complete
            and coverage >= plan.required_coverage
            and ordinary_defects <= plan.max_ordinary_defects
        ):
            return RELEASED
        return INSUFFICIENT_EVIDENCE

    def _build_record(
        self,
        sid: str,
        version: int,
        severe_packages: set[str],
        support_map: Mapping[str, set[str]],
    ) -> NodeRecord:
        spec = self.specs[sid]
        states = self.states[sid]

        covered = frozenset(
            package_id
            for package_id in spec.frame.packages
            if (state := states.get(package_id)) is not None and state.valid_item_ids
        )
        local_severe = {
            package_id: frozenset(state.severe_item_ids)
            for package_id, state in states.items()
            if state.severe_item_ids
        }
        local_ordinary = {
            package_id: frozenset(state.ordinary_item_ids)
            for package_id, state in states.items()
            if state.ordinary_item_ids
        }
        immutable_support = MappingProxyType(
            {package_id: frozenset(upstreams) for package_id, upstreams in support_map.items() if upstreams}
        )

        severe_frozen = frozenset(severe_packages)
        decision = self._decide(
            spec,
            severe_count=len(severe_frozen),
            coverage=len(covered),
            ordinary_defects=len(local_ordinary),
        )
        return NodeRecord(
            version=version,
            subbatch_id=sid,
            decision=decision,
            coverage=len(covered),
            ordinary_defects=len(local_ordinary),
            severe_packages=severe_frozen,
            covered_packages=covered,
            local_severe_items=MappingProxyType(local_severe),
            local_ordinary_items=MappingProxyType(local_ordinary),
            support=immutable_support,
        )

    def _latest_record(self, sid: str) -> NodeRecord:
        return self.history[sid][-1]

    def _record_at(self, sid: str, version: int) -> NodeRecord:
        history = self.history[sid]
        if not history or history[0].version > version:
            raise LedgerError(f"子批 {sid} 没有版本 {version} 或更早的判定记录")
        left, right = 0, len(history)
        while left < right:
            mid = (left + right) // 2
            if history[mid].version <= version:
                left = mid + 1
            else:
                right = mid
        return history[left - 1]

    def _canonical_basis(
        self,
        record: NodeRecord,
        version: int,
        *,
        candidate: Mapping[str, NodeRecord] | None = None,
    ) -> frozenset[str]:
        """返回去重后的、支撑该记录全部严重包装的原始严重检测项集合。"""
        item_ids: set[str] = set()
        for package_id in record.severe_packages:
            origin = self.package_owner[package_id]
            origin_record = candidate.get(origin) if candidate else None
            if origin_record is None:
                origin_record = self._record_at(origin, version)
            item_ids.update(origin_record.local_severe_items.get(package_id, frozenset()))
        return frozenset(item_ids)

    # ---------- 查询/审计 ----------

    def list_versions(self) -> list[dict[str, Any]]:
        return [self.publishes[v].to_dict() for v in sorted(self.publishes)]

    def query(self, version: int, subbatch_id: str) -> SubbatchQueryView:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise LedgerError("version 必须是从 1 开始的整数")
        if version > self.current_version:
            raise LedgerError(f"版本 {version} 尚未发布；当前版本为 {self.current_version}")
        if subbatch_id not in self.specs:
            raise LedgerError(f"未知子批: {subbatch_id}")
        record = self._record_at(subbatch_id, version)
        return self._build_query_view(record, version)

    def query_dict(self, version: int, subbatch_id: str) -> dict[str, Any]:
        return self.query(version, subbatch_id).to_dict()

    def version_report(self, version: int) -> list[dict[str, Any]]:
        if version < 1 or version > self.current_version:
            raise LedgerError(f"版本 {version} 不存在")
        return [self.query(version, sid).to_dict() for sid in sorted(self.specs)]

    def _item_view(self, item_id: str, version: int) -> ItemView:
        item = self.items[item_id]
        withdrawn_version = self.withdrawn_at.get(item_id)
        # 旧版本查询必须呈现该版本时点的状态；未来才发生的撤回不能泄漏进结果。
        if withdrawn_version is not None and withdrawn_version > version:
            withdrawn_version = None
        return ItemView(
            item_id=item.item_id,
            package_id=item.package_id,
            inspected_subbatch_id=self.package_owner[item.package_id],
            result=item.result,
            result_zh=RESULT_ZH[item.result],
            added_version=item.added_version,
            withdrawn_version=withdrawn_version,
        )

    def _propagation_audit(
        self, target_record: NodeRecord, package_id: str, version: int
    ) -> tuple[bool, tuple[PropagationEdgeView, ...], tuple[str, ...]]:
        edges: set[tuple[str, str]] = set()
        seen: set[str] = set()
        stack = [target_record.subbatch_id]
        record_cache: dict[str, NodeRecord] = {target_record.subbatch_id: target_record}

        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            record = record_cache.get(sid)
            if record is None:
                record = self._record_at(sid, version)
                record_cache[sid] = record
            for upstream in record.support.get(package_id, frozenset()):
                edges.add((upstream, sid))
                if upstream not in seen and upstream not in record_cache:
                    stack.append(upstream)

        edge_views: list[PropagationEdgeView] = []
        propagating: set[str] = set()
        source_found = False
        for upstream, downstream in sorted(edges):
            upstream_record = record_cache.get(upstream) or self._record_at(upstream, version)
            record_cache[upstream] = upstream_record
            count = len(upstream_record.severe_packages)
            threshold = self.specs[upstream].plan.propagation_threshold
            edge_views.append(
                PropagationEdgeView(
                    from_subbatch_id=upstream,
                    to_subbatch_id=downstream,
                    from_severe_evidence_count=count,
                    propagation_threshold=threshold,
                    threshold_met=count >= threshold,
                )
            )
            propagating.add(upstream)

        for sid in seen:
            record = record_cache.get(sid) or self._record_at(sid, version)
            if package_id in record.local_severe_items:
                source_found = True

        origin = self.package_owner[package_id]
        if origin in seen:
            source_found = source_found or package_id in (
                record_cache.get(origin) or self._record_at(origin, version)
            ).local_severe_items
        return source_found, tuple(edge_views), tuple(sorted(propagating | ({origin} if source_found else set())))

    def _build_query_view(self, record: NodeRecord, version: int) -> SubbatchQueryView:
        sid = record.subbatch_id
        spec = self.specs[sid]

        severe_views: list[SevereEvidenceView] = []
        for package_id in sorted(record.severe_packages):
            origin = self.package_owner[package_id]
            origin_record = self._record_at(origin, version)
            item_ids = sorted(origin_record.local_severe_items.get(package_id, frozenset()))
            source_found, edge_views, propagating = self._propagation_audit(record, package_id, version)
            direct_predecessors = tuple(sorted(record.support.get(package_id, frozenset())))
            severe_views.append(
                SevereEvidenceView(
                    package_id=package_id,
                    origin_subbatch_id=origin,
                    inspected_subbatch_id=origin,
                    source_found=source_found and bool(item_ids),
                    severe_items=tuple(self._item_view(item_id, version) for item_id in item_ids),
                    direct_predecessors=direct_predecessors,
                    propagation_edges=edge_views,
                    propagating_subbatches=propagating,
                )
            )

        ordinary_views: list[OrdinaryPackageView] = []
        for package_id in sorted(record.local_ordinary_items):
            item_ids = sorted(record.local_ordinary_items[package_id])
            owner = self.package_owner[package_id]
            ordinary_views.append(
                OrdinaryPackageView(
                    package_id=package_id,
                    inspected_subbatch_id=owner,
                    ordinary_items=tuple(self._item_view(item_id, version) for item_id in item_ids),
                )
            )

        return SubbatchQueryView(
            version=version,
            subbatch_id=sid,
            decision=record.decision,
            decision_zh=DECISION_ZH[record.decision],
            severe_evidence_count=len(record.severe_packages),
            severe_packages=tuple(severe_views),
            coverage=record.coverage,
            required_coverage=spec.plan.required_coverage,
            coverage_met=record.coverage >= spec.plan.required_coverage,
            covered_packages=tuple(sorted(record.covered_packages)),
            frame_complete=spec.frame.complete,
            ordinary_defects=record.ordinary_defects,
            max_ordinary_defects=spec.plan.max_ordinary_defects,
            ordinary_limit_met=record.ordinary_defects <= spec.plan.max_ordinary_defects,
            ordinary_defect_packages=tuple(ordinary_views),
            propagation_threshold=spec.plan.propagation_threshold,
            rejection_threshold=spec.plan.rejection_threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "versions": self.list_versions(),
        }


def run_request(data: Mapping[str, Any]) -> dict[str, Any]:
    """处理一个可包含多批修订和多个查询的 JSON 请求。"""
    ledger = ReleaseLedger.from_dict(data)
    publish_results: list[dict[str, Any]] = [ledger.publishes[1].to_dict()]

    revisions = data.get("revisions") or data.get("revision_batches") or data.get("batches") or ()
    for index, revision in enumerate(revisions):
        if not isinstance(revision, Mapping):
            publish_results.append(
                PublishResult(
                    accepted=False,
                    version=ledger.current_version,
                    published=False,
                    errors=(f"revisions[{index}] 必须是对象",),
                ).to_dict()
            )
            continue
        result = ledger.try_publish_revision(
            add=revision.get("add") or revision.get("add_items") or revision.get("insert") or (),
            withdraw=revision.get("withdraw") or revision.get("withdraw_items") or revision.get("remove") or (),
            note=revision.get("note") or revision.get("label"),
        )
        payload = result.to_dict()
        payload["request_index"] = index
        publish_results.append(payload)

    query_results: list[dict[str, Any]] = []
    queries = data.get("queries") or ()
    for index, query in enumerate(queries):
        label = None
        try:
            if isinstance(query, str):
                version = ledger.current_version
                sid = query
            elif isinstance(query, Mapping):
                raw_version = query.get("version")
                version = ledger.current_version if raw_version is None else int(raw_version)
                sid = str(_first(query, ["subbatch_id", "subbatch", "id"]))
                label = query.get("label")
            else:
                query_results.append({"request_index": index, "ok": False, "error": "查询格式无法识别"})
                continue
            view = ledger.query_dict(version, sid)
            item: dict[str, Any] = {"request_index": index, "ok": True, "result": view}
            if label is not None:
                item["label"] = label
            query_results.append(item)
        except (LedgerError, KeyError, ValueError, TypeError) as exc:
            query_results.append({"request_index": index, "ok": False, "error": str(exc)})

    output: dict[str, Any] = {
        "current_version": ledger.current_version,
        "publish_results": publish_results,
        "query_results": query_results,
    }
    if data.get("include_version_reports"):
        output["version_reports"] = {
            str(version): ledger.version_report(version) for version in sorted(ledger.publishes)
        }
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="版本化子批放行账")
    parser.add_argument("request", nargs="?", default="-", help="JSON 请求文件，- 表示标准输入")
    parser.add_argument("--output", "-o", help="输出 JSON 文件；默认标准输出")
    args = parser.parse_args(argv)

    try:
        if args.request == "-":
            data = json.loads(sys.stdin.read())
        else:
            data = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = run_request(data)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            sys.stdout.write(text + "\n")
        return 0
    except (OSError, json.JSONDecodeError, LedgerConfigError) as exc:
        sys.stderr.write(f"请求处理失败: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
