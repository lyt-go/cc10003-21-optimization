"""指标预警业务规则：规则维护、口径版本绑定与裁决引擎联动。

预警规则必须绑定指标口径版本：登记/生效时回写当前指标的「口径版本」，
规则的任何阈值类修订都会递增「修订版本」使旧评估缓存失效；
指标口径版本变化时只重评引用新版本的规则，其余结论继续复用。
"""
from __future__ import annotations

from typing import Any

from app.services.alert_arbitration import (
    ACTIVE_STATUS,
    OPERATORS,
    ArbitrationError,
    engine,
)
from app.store import store

MODULE = "alertconfig"
METRIC_MODULE = "metric"
REQUIRED_FIELDS = ["预警编号", "关联指标", "比较运算符"]
# 阈值类字段：任何一项被修订都要递增修订版本并让评估缓存失效
MATERIAL_FIELDS = ["比较运算符", "阈值数值", "持续周期数", "预警级别", "生效时间", "通知接收人", "静默时段"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已关闭"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "关闭规则": "已关闭"}
NEGATIVE_ACTIONS = []


class AlertconfigService:
    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("预警编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------------
    # 规则登记与修订
    # ------------------------------------------------------------------
    def _metric(self, metric_code: str) -> dict[str, Any] | None:
        for row in store.rows(METRIC_MODULE):
            if row.get("指标编号") == metric_code:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        metric_code = str(values.get("关联指标") or "").strip()
        metric = self._metric(metric_code)
        if metric is None:
            return None, [f"关联指标 {metric_code} 不存在，规则无法绑定口径版本"]
        operator = str(values.get("比较运算符") or "").strip()
        if operator not in OPERATORS:
            return None, [f"比较运算符「{operator}」不受支持，可选 {sorted(OPERATORS)}"]
        with store.lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in MATERIAL_FIELDS:
                if values.get(field) is not None:
                    entry[field] = values.get(field)
            # 规则绑定指标当前口径版本
            entry["口径版本"] = str(metric.get("口径版本") or "")
            entry["修订版本"] = 1
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """并发修改规则在 store 锁内归并；阈值类字段变更递增修订版本并失效缓存。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"预警规则 {entry_id} 不存在或已归档"
            if entry.get("status") == ACTIVE_STATUS:
                return None, "已生效规则不允许直接修改，请先关闭规则或登记新版本"
            changed_material = False
            for field in MATERIAL_FIELDS:
                if field in values and values.get(field) is not None:
                    if str(entry.get(field, "")) != str(values.get(field)):
                        changed_material = True
                    entry[field] = values.get(field)
            operator = str(entry.get("比较运算符") or "").strip()
            if operator and operator not in OPERATORS:
                return None, f"比较运算符「{operator}」不受支持"
            if changed_material:
                entry["修订版本"] = int(entry.get("修订版本", 1)) + 1
            return entry, "预警规则已修订，修订版本已递增" if changed_material else "预警规则已更新"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"预警规则 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于指标预警可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if target == ACTIVE_STATUS:
                # 生效时回写指标当前口径版本，保证规则始终绑定一个存在的口径版本
                metric = self._metric(str(entry.get("关联指标") or ""))
                if metric is None:
                    return None, f"关联指标 {entry.get('关联指标')} 不存在，规则无法生效"
                entry["口径版本"] = str(metric.get("口径版本") or "")
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"预警规则已{action}"

    # ------------------------------------------------------------------
    # 口径版本联动
    # ------------------------------------------------------------------
    def bind_metric_version(self, entry_id: int, caliber_version: str) -> tuple[dict[str, Any] | None, str]:
        """把规则显式绑定到指定口径版本（指标发布新版本后的迁移入口）。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"预警规则 {entry_id} 不存在或已归档"
            metric = self._metric(str(entry.get("关联指标") or ""))
            if metric is None:
                return None, f"关联指标 {entry.get('关联指标')} 不存在"
            caliber_version = str(caliber_version or "").strip()
            if caliber_version != str(metric.get("口径版本") or ""):
                return None, (
                    f"口径版本 {caliber_version} 不是指标 {entry.get('关联指标')} "
                    f"当前版本 {metric.get('口径版本')}"
                )
            if entry.get("口径版本") != caliber_version:
                entry["口径版本"] = caliber_version
                entry["修订版本"] = int(entry.get("修订版本", 1)) + 1
            return entry, f"规则已绑定口径版本 {caliber_version}，旧评估缓存已失效"

    def reevaluate_metric(self, metric_code: str, *, force: bool = False) -> dict[str, Any]:
        """指标口径版本变化后的重评入口：只重评引用该指标当前版本的规则周期。

        仍引用旧口径版本的规则：其规则指纹与口径版本都未变，评估缓存继续复用，
        对应预警状态保持原裁决版本不动。
        """
        metric = self._metric(metric_code)
        if metric is None:
            raise ArbitrationError(f"指标 {metric_code} 不存在")
        current_version = str(metric.get("口径版本") or "")
        periods = [
            str(row["周期"])
            for row in engine.list_periods(metric_code)
            if str(row.get("口径版本") or "") == current_version or force
        ]
        results = [
            engine.arbitrate_period(metric_code, period, force=force) for period in periods
        ]
        reused_states = [
            row
            for row in engine.list_states(metric_code)
            if row.get("口径版本") != current_version
        ]
        return {
            "指标编号": metric_code,
            "当前口径版本": current_version,
            "重评周期数": len(results),
            "复用状态数": len(reused_states),
            "重评状态": results,
            "复用状态": reused_states,
        }
