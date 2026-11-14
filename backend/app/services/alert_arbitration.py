"""预警指标口径冲突裁决引擎。

设计要点（对应验收口径）：
- 预警规则必须绑定指标口径版本（alertconfig.口径版本），只评估引用当前观测版本的规则；
- 同一指标同一周期只形成一条预警状态（alert_state 按 指标编号+周期 唯一）；
- 优先级按「预警级别 -> 阈值边界 -> 持续周期 -> 生效时间 -> 规则 id」构成确定性字典序，
  同级规则同时命中时以已持久化、生效时间更早的规则为准并记录冲突（alert_conflict）；
- 命中记录（alert_hit）在任何后续动作之前先持久化，裁决/通知失败都不丢消费记录；
- 评估结果按「规则 + 周期 + 口径版本 + 规则指纹」缓存（alert_evaluation），
  口径版本与规则指纹不变即复用，口径版本变化时只重评引用该版本的规则，其余结论继续复用；
- 通知清单（alert_notification）与预警状态读取同一裁决版本（verdict_version）；
- 并发修改规则或重复计算同一周期：按 (指标编号, 周期) 分段加锁，临界区内幂等归并为一份。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Any

from app.store import store

# ---- 模块表名 ----------------------------------------------------------------
RULE_TABLE = "alertconfig"
PERIOD_TABLE = "alert_period"
HIT_TABLE = "alert_hit"
STATE_TABLE = "alert_state"
NOTIFICATION_TABLE = "alert_notification"
CONFLICT_TABLE = "alert_conflict"
EVALUATION_TABLE = "alert_evaluation"
METRIC_TABLE = "metric"

# ---- 领域常量 ----------------------------------------------------------------
LEVEL_RANK = {"P1": 1, "P2": 2, "P3": 3}
# 阈值边界严格度：严格不等号（>、<）优先于含等号（>=、<=）。
# 上界/下界方向不参与排序——一条上界规则与一条下界规则同级同时命中时，
# 严格度相同即视为同档，继续比较持续周期、生效时间，并按同级冲突留痕。
OPERATOR_STRICTNESS = {
    ">": 1,
    "<": 1,
    ">=": 2,
    "<=": 2,
}
OPERATORS = set(OPERATOR_STRICTNESS)

# 裁决结论分类（验收要能区分这四类，加上未命中）
OUTCOME_TRIGGER = "触发"       # 本规则胜出并形成预警
OUTCOME_ABSORBED = "吸收"      # 本规则命中但被更高优先级规则吸收
OUTCOME_REUSED = "复用"        # 口径版本未变，沿用上轮评估结论
OUTCOME_CONFLICT = "冲突"      # 同级规则同时命中，留痕裁决
OUTCOME_NORMAL = "未命中"      # 规则在该周期未命中

ACTIVE_STATUS = "已生效"
DEFAULT_LEVEL_RANK = 9


class ArbitrationError(RuntimeError):
    """裁决过程中的可读业务错误。"""


def _to_float(value: Any, field: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ArbitrationError(f"{field}「{value}」无法解析为数值") from exc


def _to_int(value: Any, field: str) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ArbitrationError(f"{field}「{value}」无法解析为整数") from exc


def compare(value: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return value > threshold
    if operator == "<":
        return value < threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<=":
        return value <= threshold
    raise ArbitrationError(f"比较运算符「{operator}」不在支持范围 {sorted(OPERATORS)}")


def rule_fingerprint(rule: dict[str, Any]) -> str:
    """规则参与裁决的口径指纹：阈值类字段任何一个变化都会使旧缓存失效。"""
    material = "|".join(
        str(rule.get(key, ""))
        for key in (
            "关联指标", "口径版本", "比较运算符", "阈值数值",
            "持续周期数", "预警级别", "生效时间", "修订版本",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def rule_set_signature(rules: list[dict[str, Any]]) -> str:
    """同一指标当前口径版本下全部生效规则的集合指纹。

    规则增删、生效/关闭、阈值修订都会改变该指纹；指纹不变时预警状态可整单复用，
    指纹变化时重裁，但其中未变化的规则仍可命中规则级评估缓存。
    """
    joined = ",".join(sorted(rule_fingerprint(rule) for rule in rules))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def priority_key(rule: dict[str, Any]) -> tuple:
    """确定性优先级字典序：级别 -> 阈值边界严格度 -> 持续周期 -> 生效时间 -> id。

    同一指标同一周期的多条命中规则据此排出唯一胜出者；除 id 外全部同档即
    「同级规则同时命中」，由生效时间（已持久化、更早者优先）裁决并记录冲突。
    """
    level = LEVEL_RANK.get(str(rule.get("预警级别", "")).strip().upper(), DEFAULT_LEVEL_RANK)
    strictness = OPERATOR_STRICTNESS.get(
        str(rule.get("比较运算符", "")).strip(), 9
    )
    duration = _to_int(rule.get("持续周期数", 1), "持续周期数")
    effective_at = str(rule.get("生效时间") or "")
    return (level, strictness, duration, effective_at, int(rule.get("id", 0)))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class AlertArbitrationEngine:
    """无状态外观 + 进程内分段锁；所有结论都落在 store 的表里。"""

    def __init__(self) -> None:
        # 每个 (指标, 周期) 一把锁：不同指标/周期可并行，同一周期的并发归并
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, metric_code: str, period: str) -> threading.RLock:
        key = (metric_code, period)
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------
    # 规则与口径版本
    # ------------------------------------------------------------------
    def _metric_version(self, metric_code: str) -> str | None:
        for row in store.rows(METRIC_TABLE):
            if row.get("指标编号") == metric_code:
                return str(row.get("口径版本") or "")
        return None

    def _active_rules(self, metric_code: str, caliber_version: str) -> list[dict[str, Any]]:
        rules = []
        for rule in store.rows(RULE_TABLE):
            if rule.get("关联指标") != metric_code:
                continue
            if rule.get("status") != ACTIVE_STATUS:
                continue
            # 规则必须绑定口径版本；只评估引用当前观测版本的规则
            if str(rule.get("口径版本") or "") != caliber_version:
                continue
            rules.append(rule)
        return rules

    def _find_evaluation(
        self, rule: dict[str, Any], period: str
    ) -> dict[str, Any] | None:
        """按 (规则, 周期, 口径版本, 规则指纹) 定位可复用的评估缓存。

        周期不同则结论不可复用：连续命中周期数、阈值是否满足都依赖具体周期。
        规则指纹（阈值类字段）或口径版本变化也会导致查不到而进入重评。
        """
        fingerprint = rule_fingerprint(rule)
        for evaluation in store.rows(EVALUATION_TABLE):
            if (
                evaluation.get("规则id") == rule.get("id")
                and evaluation.get("周期") == period
                and evaluation.get("指标编号") == rule.get("关联指标")
                and evaluation.get("口径版本") == rule.get("口径版本")
                and evaluation.get("规则指纹") == fingerprint
            ):
                return evaluation
        return None

    def _upsert_evaluation(
        self,
        rule: dict[str, Any],
        period: str,
        *,
        outcome: str,
        consecutive_periods: int,
        reused: bool,
    ) -> dict[str, Any]:
        existing = self._find_evaluation(rule, period)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing is not None:
            existing["最近结论"] = outcome
            existing["连续周期数"] = consecutive_periods
            existing["复用次数"] = int(existing.get("复用次数", 0)) + (1 if reused else 0)
            existing["最近裁决时间"] = now
            return existing
        evaluation = {
            "id": store.next_id(EVALUATION_TABLE),
            "规则id": rule.get("id"),
            "预警编号": rule.get("预警编号"),
            "指标编号": rule.get("关联指标"),
            "口径版本": rule.get("口径版本"),
            "周期": period,
            "规则指纹": rule_fingerprint(rule),
            "最近结论": outcome,
            "连续周期数": consecutive_periods,
            "复用次数": 1 if reused else 0,
            "最近裁决时间": now,
        }
        store.rows(EVALUATION_TABLE).append(evaluation)
        return evaluation

    # ------------------------------------------------------------------
    # 周期与命中
    # ------------------------------------------------------------------
    def list_periods(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PERIOD_TABLE)
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        return sorted(rows, key=lambda row: (str(row.get("指标编号")), int(row.get("周期序号", 0))))

    def ingest_period(self, metric_code: str, period: str, observed_value: Any) -> dict[str, Any]:
        """登记一条周期观测；同 (指标,周期) 重复登记按覆盖处理，保证幂等。"""
        with store.lock:
            caliber_version = self._metric_version(metric_code)
            if caliber_version is None:
                raise ArbitrationError(f"指标 {metric_code} 不存在，无法登记周期观测")
            value = _to_float(observed_value, "观测值")
            rows = store.rows(PERIOD_TABLE)
            for row in rows:
                if row.get("指标编号") == metric_code and row.get("周期") == period:
                    row["观测值"] = value
                    row["口径版本"] = caliber_version
                    return row
            sequence = (
                max(
                    (int(row.get("周期序号", 0)) for row in rows if row.get("指标编号") == metric_code),
                    default=0,
                )
                + 1
            )
            entry = {
                "id": store.next_id(PERIOD_TABLE),
                "指标编号": metric_code,
                "口径版本": caliber_version,
                "周期": period,
                "周期序号": sequence,
                "观测值": value,
            }
            rows.append(entry)
            return entry

    def _period_observations(self, metric_code: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(PERIOD_TABLE) if row.get("指标编号") == metric_code
        ]
        return sorted(rows, key=lambda row: int(row.get("周期序号", 0)))

    def _hit_map(self, metric_code: str, rules: list[dict[str, Any]]) -> dict[int, list[bool]]:
        """对每条规则按时间序计算各周期是否命中（含阈值合法性校验）。"""
        observations = self._period_observations(metric_code)
        result: dict[int, list[bool]] = {}
        for rule in rules:
            operator = str(rule.get("比较运算符", "")).strip()
            if operator not in OPERATORS:
                raise ArbitrationError(
                    f"规则 {rule.get('预警编号')} 的比较运算符「{operator}」不受支持"
                )
            threshold = _to_float(rule.get("阈值数值"), "阈值数值")
            result[int(rule["id"])] = [
                compare(float(row["观测值"]), operator, threshold) for row in observations
            ]
        return result

    @staticmethod
    def _trailing_hits(flags: list[bool], index: int) -> int:
        """截至 index 周期（含）的连续命中周期数。"""
        count = 0
        for flag in reversed(flags[: index + 1]):
            if not flag:
                break
            count += 1
        return count

    def _persist_hit(
        self,
        rule: dict[str, Any],
        period_row: dict[str, Any],
        *,
        hit: bool,
        consecutive_periods: int,
    ) -> dict[str, Any]:
        """命中消费记录先落库：同 (规则,周期) 幂等，已存在的命中标记不允许被覆盖丢失。"""
        for existing in store.rows(HIT_TABLE):
            if (
                existing.get("规则id") == rule.get("id")
                and existing.get("周期") == period_row.get("周期")
            ):
                # 已经记录过命中，则永久保留；只有未命中记录可被刷新
                if existing.get("是否命中"):
                    return existing
                existing["是否命中"] = hit
                existing["连续周期数"] = consecutive_periods
                existing["口径版本"] = rule.get("口径版本")
                return existing
        record = {
            "id": store.next_id(HIT_TABLE),
            "规则id": rule.get("id"),
            "预警编号": rule.get("预警编号"),
            "指标编号": rule.get("关联指标"),
            "口径版本": rule.get("口径版本"),
            "周期": period_row.get("周期"),
            "周期序号": period_row.get("周期序号"),
            "观测值": period_row.get("观测值"),
            "是否命中": hit,
            "连续周期数": consecutive_periods,
            "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(HIT_TABLE).append(record)
        return record

    # ------------------------------------------------------------------
    # 单周期裁决
    # ------------------------------------------------------------------
    def arbitrate_period(
        self,
        metric_code: str,
        period: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """对一个 (指标, 周期) 执行裁决，返回该周期唯一预警状态与裁决过程。"""
        lock = self._lock_for(metric_code, period)
        with lock:
            with store.lock:
                return self._arbitrate_locked(metric_code, period, force=force)

    def _arbitrate_locked(
        self, metric_code: str, period: str, *, force: bool
    ) -> dict[str, Any]:
        caliber_version = self._metric_version(metric_code)
        if caliber_version is None:
            raise ArbitrationError(f"指标 {metric_code} 不存在")
        period_row = next(
            (
                row
                for row in store.rows(PERIOD_TABLE)
                if row.get("指标编号") == metric_code and row.get("周期") == period
            ),
            None,
        )
        if period_row is None:
            raise ArbitrationError(f"指标 {metric_code} 在周期 {period} 没有观测数据")

        existing_state = self._find_state(metric_code, period)
        rules = self._active_rules(metric_code, caliber_version)
        current_signature = rule_set_signature(rules)
        if (
            existing_state is not None
            and not force
            and existing_state.get("规则集合指纹") == current_signature
        ):
            # 重复计算同一周期且规则集合未变：归并到已持久化的唯一状态，不产生新通知。
            # 返回副本并标注访问方式，避免把瞬态字段写回持久化状态。
            merged = dict(existing_state)
            merged["访问方式"] = OUTCOME_REUSED
            return merged

        hit_map = self._hit_map(metric_code, rules)
        period_index = next(
            idx
            for idx, row in enumerate(self._period_observations(metric_code))
            if row.get("周期") == period
        )

        # 第一步：逐条规则评估。命中判定始终基于当前观测重算，并先持久化命中记录
        # （命中即落库，后续任何失败都不丢消费记录）。
        # 口径版本与规则指纹未变时沿用上轮裁决结论（复用）；
        # 口径版本变化或规则被修订导致指纹变化时，缓存查不到，规则进入重新评估。
        evaluations: list[dict[str, Any]] = []
        reused_rules: set[int] = set()
        hitting: list[dict[str, Any]] = []
        for rule in rules:
            flags = hit_map[int(rule["id"])]
            hit = bool(flags[period_index])
            streak = self._trailing_hits(flags, period_index) if hit else 0
            duration = _to_int(rule.get("持续周期数", 1), "持续周期数")
            qualified = hit and streak >= max(duration, 1)
            self._persist_hit(
                rule, period_row, hit=qualified, consecutive_periods=streak
            )

            # force 表示规则集合发生过修订，需要全量重裁：规则级缓存一并失效重算。
            # 非 force 的重复计算在方法入口就已归并返回，走到这里的缓存命中即真正的复用。
            cached = None if force else self._find_evaluation(rule, period)
            if cached is not None:
                # 口径版本未变、规则指纹未变、同一周期：复用既有结论
                reused_rules.add(int(rule["id"]))
                outcome = str(cached.get("最近结论", OUTCOME_NORMAL))
            elif qualified:
                hitting.append(rule)
                outcome = OUTCOME_TRIGGER  # 暂定，吸收/冲突在选优后回写
            else:
                outcome = OUTCOME_NORMAL
            evaluation = self._upsert_evaluation(
                rule,
                period,
                outcome=outcome,
                consecutive_periods=streak,
                reused=cached is not None,
            )
            evaluations.append(evaluation)

        # 复用规则若上轮结论为触发/冲突，说明该规则在本周期同样命中（命中判定已重算），
        # 纳入候选以保证同一周期最终仍只归并出一条预警状态。
        for rule in rules:
            rid = int(rule["id"])
            if rid in reused_rules:
                cached = self._find_evaluation(rule, period)
                if cached and cached.get("最近结论") in (OUTCOME_TRIGGER, OUTCOME_CONFLICT):
                    if rule not in hitting:
                        hitting.append(rule)

        # 第二步：在命中规则里按确定性优先级选唯一胜出者
        winner: dict[str, Any] | None = None
        conflict_records: list[dict[str, Any]] = []
        absorbed: list[dict[str, Any]] = []
        if hitting:
            ordered = sorted(hitting, key=priority_key)
            winner = ordered[0]
            top_key = priority_key(winner)
            # 同级（预警级别相同）规则同时命中：除胜出者外全部留冲突记录，
            # 并以已持久化且生效时间更早（再以 id 兜底）的规则为准。
            same_level = [rule for rule in ordered if priority_key(rule)[0] == top_key[0]]
            for rule in ordered[1:]:
                reason = self._absorb_reason(rule, winner)
                absorbed.append(
                    {
                        "规则id": rule.get("id"),
                        "预警编号": rule.get("预警编号"),
                        "吸收原因": reason,
                        "预警级别": rule.get("预警级别"),
                        "阈值数值": rule.get("阈值数值"),
                        "比较运算符": rule.get("比较运算符"),
                        "复用": int(rule["id"]) in reused_rules,
                    }
                )
                if int(rule["id"]) not in reused_rules:
                    self._mark_evaluation(rule, period, OUTCOME_ABSORBED)
            if len(same_level) > 1:
                for loser in same_level:
                    if loser.get("id") == winner.get("id"):
                        continue
                    conflict = self._record_conflict(
                        winner, loser, metric_code, period, caliber_version
                    )
                    conflict_records.append(conflict)
                if int(winner["id"]) not in reused_rules:
                    self._mark_evaluation(winner, period, OUTCOME_CONFLICT)
            elif int(winner["id"]) not in reused_rules:
                self._mark_evaluation(winner, period, OUTCOME_TRIGGER)

        # 第三步：归并出该周期唯一预警状态（同 key 覆盖，历史状态被新裁决版本取代）
        streak_winner = 0
        if winner is not None:
            streak_winner = self._trailing_hits(hit_map[int(winner["id"])], period_index)
        state = self._upsert_state(
            metric_code=metric_code,
            period=period,
            period_row=period_row,
            caliber_version=caliber_version,
            rule_signature=current_signature,
            winner=winner,
            absorbed=absorbed,
            conflict_records=conflict_records,
            evaluations=evaluations,
            reused_rules=reused_rules,
            consecutive_periods=streak_winner,
        )

        # 第四步：通知清单与状态共用同一裁决版本；同一状态版本只发一次。
        # 重裁后不再预警时，撤回上一裁决版本遗留的通知，避免重复/过期消息。
        if winner is not None:
            self._upsert_notification(state)
        else:
            self._retire_notification(metric_code, period)
        return state

    def _absorb_reason(self, loser: dict[str, Any], winner: dict[str, Any]) -> str:
        loser_key = priority_key(loser)
        winner_key = priority_key(winner)
        if loser_key[0] != winner_key[0]:
            return (
                f"预警级别 {loser.get('预警级别')} 低于胜出规则 "
                f"{winner.get('预警编号')} 的 {winner.get('预警级别')}"
            )
        if loser_key[1] != winner_key[1]:
            return (
                f"阈值边界严格度不优于胜出规则 {winner.get('预警编号')} "
                f"（{winner.get('比较运算符')}{winner.get('阈值数值')}）"
            )
        if loser_key[2] != winner_key[2]:
            return (
                f"持续周期 {loser.get('持续周期数')} 长于胜出规则 "
                f"{winner.get('预警编号')} 的 {winner.get('持续周期数')}"
            )
        if loser_key[3] != winner_key[3]:
            return (
                f"同级别同时命中，生效时间 {loser.get('生效时间')} 晚于胜出规则 "
                f"{winner.get('预警编号')} 的 {winner.get('生效时间')}，同周期只保留一条预警"
            )
        return f"同级规则同时命中，按规则 id 兜底由 {winner.get('预警编号')} 胜出"

    def _mark_evaluation(self, rule: dict[str, Any], period: str, outcome: str) -> None:
        cached = self._find_evaluation(rule, period)
        if cached is not None:
            cached["最近结论"] = outcome
            cached["最近裁决时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _record_conflict(
        self,
        winner: dict[str, Any],
        loser: dict[str, Any],
        metric_code: str,
        period: str,
        caliber_version: str,
    ) -> dict[str, Any]:
        winner_time = _parse_time(winner.get("生效时间"))
        loser_time = _parse_time(loser.get("生效时间"))
        if winner_time and loser_time and winner_time <= loser_time:
            basis = "生效时间更早"
        else:
            basis = "同生效时间下按规则 id 兜底"
        record = {
            "id": store.next_id(CONFLICT_TABLE),
            "指标编号": metric_code,
            "口径版本": caliber_version,
            "周期": period,
            "胜出规则id": winner.get("id"),
            "胜出预警编号": winner.get("预警编号"),
            "落选规则id": loser.get("id"),
            "落选预警编号": loser.get("预警编号"),
            "共同级别": winner.get("预警级别"),
            "裁决依据": basis,
            "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(CONFLICT_TABLE).append(record)
        return record

    def _find_state(self, metric_code: str, period: str) -> dict[str, Any] | None:
        for state in store.rows(STATE_TABLE):
            if state.get("指标编号") == metric_code and state.get("周期") == period:
                return state
        return None

    def _verdict_version(self, metric_code: str, period: str, attempt: int) -> str:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"VD-{metric_code}-{period}-{stamp}-{attempt}"

    def _upsert_state(
        self,
        *,
        metric_code: str,
        period: str,
        period_row: dict[str, Any],
        caliber_version: str,
        rule_signature: str,
        winner: dict[str, Any] | None,
        absorbed: list[dict[str, Any]],
        conflict_records: list[dict[str, Any]],
        evaluations: list[dict[str, Any]],
        reused_rules: set[int],
        consecutive_periods: int,
    ) -> dict[str, Any]:
        existing = self._find_state(metric_code, period)
        attempt = int(existing.get("裁决轮次", 0)) + 1 if existing else 1
        verdict_version = self._verdict_version(metric_code, period, attempt)
        # 结论汇总：验收可据此直接区分 触发 / 吸收 / 复用 / 冲突
        outcome_summary = {
            OUTCOME_TRIGGER: 0,
            OUTCOME_ABSORBED: 0,
            OUTCOME_REUSED: 0,
            OUTCOME_CONFLICT: 0,
            OUTCOME_NORMAL: 0,
        }
        detail_rows = []
        for item in evaluations:
            rule_id = int(item.get("规则id", 0))
            if rule_id in reused_rules:
                label = OUTCOME_REUSED
            else:
                label = str(item.get("最近结论", OUTCOME_NORMAL))
            outcome_summary[label] = outcome_summary.get(label, 0) + 1
            detail_rows.append(
                {
                    "规则id": item.get("规则id"),
                    "预警编号": item.get("预警编号"),
                    "结论": label,
                    "复用次数": item.get("复用次数", 0),
                }
            )
        payload = {
            "指标编号": metric_code,
            "周期": period,
            "周期序号": period_row.get("周期序号"),
            "口径版本": caliber_version,
            "规则集合指纹": rule_signature,
            "裁决版本": verdict_version,
            "裁决轮次": attempt,
            "是否预警": winner is not None,
            "胜出规则id": winner.get("id") if winner else None,
            "胜出预警编号": winner.get("预警编号") if winner else None,
            "预警级别": winner.get("预警级别") if winner else None,
            "观测值": period_row.get("观测值"),
            "连续周期数": consecutive_periods,
            "结论汇总": outcome_summary,
            "被吸收规则": absorbed,
            "冲突记录": conflict_records,
            "评估明细": detail_rows,
            "裁决时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if existing is not None:
            existing.update(payload)
            return existing
        payload["id"] = store.next_id(STATE_TABLE)
        store.rows(STATE_TABLE).append(payload)
        return payload

    def _upsert_notification(self, state: dict[str, Any]) -> dict[str, Any]:
        """通知与状态绑定同一裁决版本；状态重评产生新版本时旧通知同步替换。"""
        rows = store.rows(NOTIFICATION_TABLE)
        existing = next(
            (
                row
                for row in rows
                if row.get("指标编号") == state["指标编号"]
                and row.get("周期") == state["周期"]
            ),
            None,
        )
        winner_id = state.get("胜出规则id")
        winner = store.find(RULE_TABLE, int(winner_id)) if winner_id is not None else None
        payload = {
            "指标编号": state["指标编号"],
            "周期": state["周期"],
            "口径版本": state["口径版本"],
            "裁决版本": state["裁决版本"],
            "预警编号": state.get("胜出预警编号"),
            "通知接收人": winner.get("通知接收人") if winner else None,
            "预警级别": state.get("预警级别"),
            "连续周期数": state.get("连续周期数"),
            "吸收说明": [
                f"{item['预警编号']}：{item['吸收原因']}" for item in state.get("被吸收规则", [])
            ],
            "发送状态": "待发送",
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if existing is not None:
            existing.update(payload)
            return existing
        payload["id"] = store.next_id(NOTIFICATION_TABLE)
        rows.append(payload)
        return payload

    def _retire_notification(self, metric_code: str, period: str) -> None:
        """撤回不再预警周期的遗留通知，保证通知与状态严格一一对应。"""
        rows = store.rows(NOTIFICATION_TABLE)
        for row in list(rows):
            if row.get("指标编号") == metric_code and row.get("周期") == period:
                rows.remove(row)

    # ------------------------------------------------------------------
    # 批量入口
    # ------------------------------------------------------------------
    def run_all(self, *, force: bool = False) -> dict[str, Any]:
        """对所有已登记周期执行裁决；按 (指标,周期) 分段加锁，部分失败不丢已命中记录。"""
        targets = [
            (str(row["指标编号"]), str(row["周期"]))
            for row in self.list_periods()
        ]
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for metric_code, period in targets:
            try:
                results.append(self.arbitrate_period(metric_code, period, force=force))
            except ArbitrationError as exc:
                # 命中记录在抛错前已落库，这里只汇总失败，不回滚
                failures.append({"指标编号": metric_code, "周期": period, "原因": str(exc)})
        return {
            "裁决周期数": len(results),
            "失败周期数": len(failures),
            "失败明细": failures,
            "状态": results,
        }

    # ------------------------------------------------------------------
    # 只读查询（通知清单与状态同裁决版本的校验入口）
    # ------------------------------------------------------------------
    def list_states(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(STATE_TABLE)
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        return sorted(rows, key=lambda row: (str(row.get("指标编号")), int(row.get("周期序号", 0))))

    def list_notifications(self) -> list[dict[str, Any]]:
        return sorted(
            store.rows(NOTIFICATION_TABLE),
            key=lambda row: (str(row.get("指标编号")), str(row.get("周期"))),
        )

    def list_conflicts(self) -> list[dict[str, Any]]:
        return list(store.rows(CONFLICT_TABLE))

    def list_hits(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(HIT_TABLE)
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        return rows

    def list_evaluations(self) -> list[dict[str, Any]]:
        return list(store.rows(EVALUATION_TABLE))

    def consistency_report(self) -> dict[str, Any]:
        """一致性自检：状态/通知裁决版本对齐、每指标每周期状态唯一、口径版本连续。"""
        states = store.rows(STATE_TABLE)
        notifications = store.rows(NOTIFICATION_TABLE)
        keys = [(row.get("指标编号"), row.get("周期")) for row in states]
        duplicate_keys = sorted({key for key in keys if keys.count(key) > 1})
        notice_keys = [
            (row.get("指标编号"), row.get("周期")) for row in notifications
        ]
        duplicate_notice_keys = sorted(
            {key for key in notice_keys if notice_keys.count(key) > 1}
        )
        mismatched: list[str] = []
        for notification in notifications:
            state = self._find_state(
                str(notification["指标编号"]), str(notification["周期"])
            )
            if state is None:
                mismatched.append(
                    f"通知 {notification.get('预警编号')} 找不到对应预警状态"
                )
            elif state.get("裁决版本") != notification.get("裁决版本"):
                mismatched.append(
                    f"{notification['指标编号']} {notification['周期']} 通知裁决版本"
                    f" {notification.get('裁决版本')} 与状态 {state.get('裁决版本')} 不一致"
                )
            elif not state.get("是否预警"):
                mismatched.append(
                    f"{notification['指标编号']} {notification['周期']} 状态未预警却存在通知"
                )
        for state in states:
            if not state.get("是否预警"):
                continue
            notice = next(
                (
                    row
                    for row in notifications
                    if row.get("指标编号") == state["指标编号"]
                    and row.get("周期") == state["周期"]
                ),
                None,
            )
            if notice is None:
                mismatched.append(
                    f"{state['指标编号']} {state['周期']} 已预警但缺少通知清单"
                )
        version_gaps: list[str] = []
        for state in states:
            metric_code = str(state["指标编号"])
            current = self._metric_version(metric_code)
            if current is not None and not state.get("口径版本"):
                version_gaps.append(f"{metric_code} {state.get('周期')} 状态缺少口径版本")
        return {
            "预警状态数": len(states),
            "通知数": len(notifications),
            "重复状态键": duplicate_keys,
            "重复通知键": duplicate_notice_keys,
            "裁决版本不一致": mismatched,
            "口径版本缺口": version_gaps,
            "一致": (
                not duplicate_keys
                and not duplicate_notice_keys
                and not mismatched
                and not version_gaps
            ),
        }


engine = AlertArbitrationEngine()
