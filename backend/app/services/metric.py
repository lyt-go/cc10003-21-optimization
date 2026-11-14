"""指标口径业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "口径评审中", "已发布", "已下线"]
ACTION_RULES = {"提交口径评审": "口径评审中", "发布指标": "已发布", "下线指标": "已下线"}
NEGATIVE_ACTIONS = ["下线指标"]

class MetricService:
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
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标口径可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def bump_caliber_version(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发布新口径版本：vN -> v(N+1)。引用旧版本的预警规则不会被静默改写。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"指标定义 {entry_id} 不存在或已归档"
            current = str(entry.get("口径版本") or "v0")
            try:
                serial = int(current.removeprefix("v")) + 1
            except ValueError:
                serial = 1
            entry["口径版本"] = f"v{serial}"
            return entry, f"指标 {entry.get('指标编号')} 口径版本已升级为 v{serial}"
