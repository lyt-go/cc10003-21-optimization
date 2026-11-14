"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
预警裁决相关的表（alert_*）在一次服务生命周期内持久保存在内存里，刷新/重新进入
页面读到的都是同一份已落库结论；Store.reset() 只在测试隔离时使用。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        # 进程内锁：并发修改规则、重复计算同一周期时，临界区由 service 层加锁
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """恢复到种子数据。仅测试隔离与显式重置接口调用，业务流程不允许清库。"""
        with self.lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """在表内分配下一个自增 id，调用方需持有 store.lock。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
