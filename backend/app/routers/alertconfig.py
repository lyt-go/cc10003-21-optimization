"""指标预警接口：规则维护 + 冲突裁决引擎入口。

裁决相关接口都挂在本路由下：
- /arbitration/run        执行全部周期裁决（触发/吸收/复用/冲突的唯一入口）
- /arbitration/periods    周期观测登记与查询
- /arbitration/states     每指标每周期唯一预警状态（含裁决版本、吸收身份、连续周期）
- /arbitration/notifications 通知清单（与状态读取同一裁决版本）
- /arbitration/conflicts  同级规则同时命中的冲突留痕
- /arbitration/hits       命中消费记录（先持久化，失败不丢）
- /arbitration/evaluations 规则评估缓存（口径版本未变即复用）
- /arbitration/consistency 一致性自检
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert_arbitration import ArbitrationError, engine
from app.services.alertconfig import AlertconfigService

router = APIRouter(prefix="/api/alertconfig", tags=["指标预警"])

service = AlertconfigService()

LIST_FIELDS = [
    "预警编号", "关联指标", "口径版本", "比较运算符", "阈值数值",
    "持续周期数", "预警级别", "通知接收人", "静默时段", "生效时间", "修订版本",
]
STATUSES = ["草稿", "规则评审中", "已生效", "已关闭"]


# ----------------------------------------------------------------------
# 裁决引擎固定路径（必须在 /{entry_id} 之前声明）
# ----------------------------------------------------------------------
@router.post("/arbitration/run")
def run_arbitration(payload: EntryPayload | None = None) -> dict[str, Any]:
    """对全部已登记周期执行裁决；重复调用幂等归并，失败周期不丢命中记录。"""
    force = False
    if payload is not None:
        force = bool(payload.values.get("force"))
    try:
        return engine.run_all(force=force)
    except ArbitrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/arbitration/periods")
def ingest_period(payload: EntryPayload) -> ActionResult:
    """登记/覆盖一条指标周期观测值，随后即可对该周期发起裁决。"""
    values = payload.values
    try:
        entry = engine.ingest_period(
            str(values.get("指标编号") or "").strip(),
            str(values.get("周期") or "").strip(),
            values.get("观测值"),
        )
    except ArbitrationError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="周期观测已登记", entry=entry)


@router.get("/arbitration/periods")
def list_periods(metric: str | None = Query(default=None, description="按指标编号过滤")) -> dict[str, Any]:
    items = engine.list_periods(metric)
    return {"total": len(items), "items": items}


@router.post("/arbitration/periods/{metric_code}/{period}")
def arbitrate_one(metric_code: str, period: str, payload: EntryPayload | None = None) -> dict[str, Any]:
    """对单个 (指标, 周期) 裁决；并发重复调用在分段锁内归并为同一份状态。"""
    force = bool(payload.values.get("force")) if payload is not None else False
    try:
        return engine.arbitrate_period(metric_code, period, force=force)
    except ArbitrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/arbitration/reevaluate/{metric_code}")
def reevaluate_metric(metric_code: str, payload: EntryPayload | None = None) -> dict[str, Any]:
    """口径版本变化后重评：只重评引用当前版本的规则，旧版本结论继续复用。"""
    force = bool(payload.values.get("force")) if payload is not None else False
    try:
        return service.reevaluate_metric(metric_code, force=force)
    except ArbitrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/arbitration/states")
def list_states(metric: str | None = Query(default=None)) -> dict[str, Any]:
    items = engine.list_states(metric)
    return {"total": len(items), "items": items}


@router.get("/arbitration/notifications")
def list_notifications() -> dict[str, Any]:
    items = engine.list_notifications()
    return {"total": len(items), "items": items}


@router.get("/arbitration/conflicts")
def list_conflicts() -> dict[str, Any]:
    items = engine.list_conflicts()
    return {"total": len(items), "items": items}


@router.get("/arbitration/hits")
def list_hits(metric: str | None = Query(default=None)) -> dict[str, Any]:
    items = engine.list_hits(metric)
    return {"total": len(items), "items": items}


@router.get("/arbitration/evaluations")
def list_evaluations() -> dict[str, Any]:
    items = engine.list_evaluations()
    return {"total": len(items), "items": items}


@router.get("/arbitration/consistency")
def consistency_report() -> dict[str, Any]:
    """刷新/重新进入页面前后可调用：校验状态、通知裁决版本与口径版本的一致性。"""
    return engine.consistency_report()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出指标预警清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alertconfig", "total": total, "items": items}


# ----------------------------------------------------------------------
# 规则维护
# ----------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按预警编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按预警编号与状态过滤指标预警列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预警规则，规则必须绑定关联指标当前口径版本；缺字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"校验未通过：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预警规则已登记并绑定口径版本", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修订未生效规则；阈值类字段变更会递增修订版本并使评估缓存失效。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预警规则执行提交规则评审、生效规则、关闭规则；生效时回写口径版本。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/bind-version", response_model=ActionResult)
def bind_metric_version(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把规则显式迁移绑定到指标当前口径版本，旧评估缓存随即失效。"""
    caliber_version = str(payload.values.get("口径版本") or "").strip()
    entry, message = service.bind_metric_version(entry_id, caliber_version)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
