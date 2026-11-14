"""预警冲突裁决引擎验收测试。

覆盖需求点：
1. 优先级：预警级别 -> 阈值边界 -> 持续周期 -> 生效时间 的确定性裁决；
2. 同一指标同一周期只有一条预警状态，被吸收规则保留身份与原因；
3. 触发 / 吸收 / 复用 / 冲突 四类结论可区分；
4. 口径版本变化只重评引用新版本的规则，其余结论复用；
5. 同级规则同时命中按已持久化、生效时间更早者胜出并记录冲突（ALER-0002/0003 样例）；
6. 命中记录先持久化，后续失败不丢；
7. 并发修改/重复计算归并为一份；
8. 通知清单与预警状态读取同一裁决版本，持久一致。
"""
from __future__ import annotations

import threading

import pytest

from app.services.alert_arbitration import (
    OUTCOME_ABSORBED,
    OUTCOME_CONFLICT,
    OUTCOME_NORMAL,
    OUTCOME_REUSED,
    OUTCOME_TRIGGER,
    ArbitrationError,
    engine,
    priority_key,
    rule_fingerprint,
)
from app.services.alertconfig import AlertconfigService
from app.services.metric import MetricService
from app.store import store


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield
    store.reset()


def _find(rows: list[dict], **criteria) -> dict:
    for row in rows:
        if all(row.get(key) == value for key, value in criteria.items()):
            return row
    raise AssertionError(f"找不到记录：{criteria}")


def _rule(rule_id: int) -> dict:
    rule = store.find("alertconfig", rule_id)
    assert rule is not None
    return rule


# ----------------------------------------------------------------------
# 1. 确定性优先级
# ----------------------------------------------------------------------
class TestPriority:
    def test_level_dominates(self):
        # ALER-0004 是 P1，ALER-0002 是 P2：P1 必须排在前面
        assert priority_key(_rule(4)) < priority_key(_rule(2))

    def test_strict_boundary_beats_loose(self):
        base = {
            "id": 1, "预警级别": "P2", "比较运算符": ">", "阈值数值": 10,
            "持续周期数": 1, "生效时间": "2026-09-01 00:00:00",
        }
        loose = dict(base, 比较运算符=">=")
        assert priority_key(base) < priority_key(loose)

    def test_shorter_duration_beats_longer(self):
        base = {
            "id": 1, "预警级别": "P2", "比较运算符": ">", "阈值数值": 10,
            "持续周期数": 1, "生效时间": "2026-09-01 00:00:00",
        }
        longer = dict(base, 持续周期数=3)
        assert priority_key(base) < priority_key(longer)

    def test_earlier_effective_time_beats_later(self):
        early = {
            "id": 1, "预警级别": "P2", "比较运算符": ">", "阈值数值": 10,
            "持续周期数": 1, "生效时间": "2026-09-01 00:00:00",
        }
        late = dict(early, 生效时间="2026-09-05 00:00:00")
        assert priority_key(early) < priority_key(late)

    def test_direction_does_not_break_same_level_tie(self):
        # 上界 >= 与下界 <= 同档严格度，方向不参与优先级（交给生效时间与冲突留痕）
        upper = {
            "id": 2, "预警级别": "P2", "比较运算符": ">=", "阈值数值": 95,
            "持续周期数": 2, "生效时间": "2026-09-02 09:00:00",
        }
        lower = {
            "id": 3, "预警级别": "P2", "比较运算符": "<=", "阈值数值": 96,
            "持续周期数": 2, "生效时间": "2026-09-03 09:00:00",
        }
        assert priority_key(upper) < priority_key(lower)
        # 除生效时间与 id 外完全同档
        assert priority_key(upper)[:3] == priority_key(lower)[:3]


# ----------------------------------------------------------------------
# 2/3/5. ALER-0002 / ALER-0003 冲突样例与四类结论
# ----------------------------------------------------------------------
class TestArbitrationOutcomes:
    def test_single_state_per_metric_period(self):
        engine.run_all()
        states = engine.list_states("METR-0003")
        keys = [(s["指标编号"], s["周期"]) for s in states]
        assert len(keys) == len(set(keys))
        assert len(states) == 3

    def test_conflict_sample_unique_notification_with_absorbed_identity(self):
        engine.run_all()
        state = _find(engine.list_states("METR-0003"), 周期="2026-W38")

        # 唯一胜出：同级 P2，按生效时间更早的 ALER-0002
        assert state["胜出预警编号"] == "ALER-0002"
        assert state["预警级别"] == "P2"

        # 被吸收规则身份与原因都保留
        absorbed = _find(state["被吸收规则"], 预警编号="ALER-0003")
        assert "生效时间" in absorbed["吸收原因"]
        assert absorbed["比较运算符"] == "<="

        # 冲突留痕
        conflict = _find(engine.list_conflicts(), 周期="2026-W38")
        assert conflict["胜出预警编号"] == "ALER-0002"
        assert conflict["落选预警编号"] == "ALER-0003"
        assert conflict["共同级别"] == "P2"
        assert "生效时间更早" in conflict["裁决依据"]

        # 四类结论在评估明细里可区分
        labels = {row["预警编号"]: row["结论"] for row in state["评估明细"]}
        assert labels["ALER-0002"] == OUTCOME_CONFLICT
        assert labels["ALER-0003"] == OUTCOME_ABSORBED
        assert labels["ALER-0004"] == OUTCOME_NORMAL
        assert set(state["结论汇总"]) >= {
            OUTCOME_TRIGGER, OUTCOME_ABSORBED, OUTCOME_REUSED, OUTCOME_CONFLICT, OUTCOME_NORMAL
        }

    def test_high_level_rule_absorbs_lower_level(self):
        engine.run_all()
        state = _find(engine.list_states("METR-0003"), 周期="2026-W36")
        assert state["胜出预警编号"] == "ALER-0004"
        assert state["预警级别"] == "P1"
        # W36 只有 ALER-0004 满足持续周期，ALER-0002 尚未连续两期 -> 不产生吸收
        assert state["被吸收规则"] == []

    def test_consecutive_periods_tracked(self):
        engine.run_all()
        w37 = _find(engine.list_states("METR-0003"), 周期="2026-W37")
        w38 = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        assert w37["胜出预警编号"] == "ALER-0002"
        assert w37["连续周期数"] == 2
        assert w38["连续周期数"] == 3

    def test_normal_period_no_winner(self):
        engine.run_all()
        state = _find(engine.list_states("METR-0002"), 周期="2026-W37")
        assert state["是否预警"] is False
        assert state["胜出预警编号"] is None


# ----------------------------------------------------------------------
# 4. 口径版本变化：只重评引用新版本的规则，其余复用
# ----------------------------------------------------------------------
class TestCaliberVersionChange:
    def test_rules_must_bind_caliber_version(self):
        for rule in store.rows("alertconfig"):
            assert rule.get("口径版本"), f"{rule.get('预警编号')} 未绑定口径版本"

    def test_version_bump_only_reevaluates_referencing_rules(self):
        engine.run_all()
        before = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        before_version = before["裁决版本"]

        # 指标 METR-0003 口径版本 v3 -> v4；规则仍绑定 v3
        metric_service = MetricService()
        metric, _ = metric_service.bump_caliber_version(3)
        assert metric["口径版本"] == "v4"

        report = AlertconfigService().reevaluate_metric("METR-0003")
        # 没有规则引用 v4：没有可重评周期，旧 v3 结论整体复用
        assert report["重评周期数"] == 0
        assert report["复用状态数"] >= 1

        after = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        assert after["裁决版本"] == before_version
        assert after["口径版本"] == "v3"

    def test_rebind_rule_invalidates_its_cache_and_rearbitrates(self):
        engine.run_all()
        state_before = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        old_verdict = state_before["裁决版本"]

        metric_service = MetricService()
        metric_service.bump_caliber_version(3)  # v3 -> v4
        alert_service = AlertconfigService()
        # 把 ALER-0002 显式迁移到 v4（修订版本递增、指纹变化）
        entry, message = alert_service.bind_metric_version(2, "v4")
        assert entry["口径版本"] == "v4"
        assert "缓存已失效" in message

        alert_service.reevaluate_metric("METR-0003", force=True)
        state_after = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        # 引用 v4 的规则被重评，产生新裁决版本
        assert state_after["裁决版本"] != old_verdict
        assert state_after["口径版本"] == "v4"

    def test_unchanged_rule_verdict_reused_when_rule_set_changes(self):
        engine.run_all()
        # 修订一条未生效规则（ALER-0006 是草稿），使 METR-0003 的规则集合指纹变化
        alert_service = AlertconfigService()
        entry, message = alert_service.update_entry(6, {"阈值数值": "99.5"})
        assert entry["修订版本"] == 2
        # 生效该规则，集合指纹随之变化
        alert_service.run_action(6, "提交规则评审")
        alert_service.run_action(6, "生效规则")

        state = engine.arbitrate_period("METR-0003", "2026-W38")
        labels = {row["预警编号"]: row["结论"] for row in state["评估明细"]}
        # ALER-0002/0003 指纹未变：规则级评估缓存被复用；ALER-0006 是新进入的规则
        assert labels["ALER-0002"] == OUTCOME_REUSED
        assert labels["ALER-0003"] == OUTCOME_REUSED
        evals = [e for e in engine.list_evaluations() if e["预警编号"] == "ALER-0002"]
        assert any(e["复用次数"] >= 1 for e in evals)


# ----------------------------------------------------------------------
# 6. 命中记录先持久化，失败不丢
# ----------------------------------------------------------------------
class TestHitDurability:
    def test_hit_records_persisted_before_verdict(self, monkeypatch):
        # 让通知写入阶段抛错，模拟裁决后半段失败
        def boom(_state):
            raise RuntimeError("通知通道故障")

        monkeypatch.setattr(engine, "_upsert_notification", boom)
        with pytest.raises(RuntimeError, match="通知通道故障"):
            engine.arbitrate_period("METR-0003", "2026-W38")

        hits = [h for h in engine.list_hits("METR-0003") if h["周期"] == "2026-W38"]
        # 命中消费记录在失败前已经落库，且包含全部规则的判定
        assert {h["预警编号"] for h in hits} >= {"ALER-0002", "ALER-0003"}
        assert _find(hits, 预警编号="ALER-0002")["是否命中"] is True

    def test_confirmed_hit_never_overwritten(self):
        engine.ingest_period("METR-0001", "2026-W37", 92.5)
        engine.arbitrate_period("METR-0001", "2026-W37")
        hit = _find(engine.list_hits("METR-0001"), 预警编号="ALER-0001", 周期="2026-W37")
        assert hit["是否命中"] is True
        # 观测值被改成不可能命中，已确认的命中记录仍保留
        engine.ingest_period("METR-0001", "2026-W37", 1.0)
        engine.arbitrate_period("METR-0001", "2026-W37", force=True)
        hit2 = _find(engine.list_hits("METR-0001"), 预警编号="ALER-0001", 周期="2026-W37")
        assert hit2["是否命中"] is True


# ----------------------------------------------------------------------
# 7. 并发与重复计算归并
# ----------------------------------------------------------------------
class TestConcurrency:
    def test_repeated_arbitration_merges_to_one_state(self):
        first = engine.arbitrate_period("METR-0003", "2026-W38")
        second = engine.arbitrate_period("METR-0003", "2026-W38")
        assert first["裁决版本"] == second["裁决版本"]
        assert second["访问方式"] == OUTCOME_REUSED
        states = [s for s in engine.list_states("METR-0003") if s["周期"] == "2026-W38"]
        assert len(states) == 1
        notifications = [
            n for n in engine.list_notifications()
            if n["指标编号"] == "METR-0003" and n["周期"] == "2026-W38"
        ]
        assert len(notifications) == 1

    def test_concurrent_arbitration_returns_same_version(self):
        results: list[dict] = []
        errors: list[Exception] = []

        def worker():
            try:
                results.append(engine.arbitrate_period("METR-0003", "2026-W38"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        versions = {state["裁决版本"] for state in results}
        assert len(versions) == 1
        w38_states = [
            s for s in engine.list_states("METR-0003") if s["周期"] == "2026-W38"
        ]
        assert len(w38_states) == 1

    def test_concurrent_rule_modification_serialized(self):
        service = AlertconfigService()
        errors: list[Exception] = []

        def modify(rule_id: int, threshold: str):
            try:
                service.update_entry(rule_id, {"阈值数值": threshold})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=modify, args=(6, f"99.{i}")) for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        rule = _rule(6)
        assert rule["阈值数值"] in {f"99.{i}" for i in range(6)}
        assert rule["修订版本"] >= 2


# ----------------------------------------------------------------------
# 8. 通知与状态同裁决版本、持久一致
# ----------------------------------------------------------------------
class TestNotificationAndConsistency:
    def test_notification_shares_verdict_version(self):
        engine.run_all()
        for notification in engine.list_notifications():
            state = _find(
                engine.list_states(notification["指标编号"]),
                周期=notification["周期"],
            )
            assert notification["裁决版本"] == state["裁决版本"]
            assert notification["口径版本"] == state["口径版本"]
            assert notification["预警编号"] == state["胜出预警编号"]

    def test_notification_carries_absorption_explanation(self):
        engine.run_all()
        notification = _find(
            engine.list_notifications(), 指标编号="METR-0003", 周期="2026-W38"
        )
        assert any("ALER-0003" in line for line in notification["吸收说明"])

    def test_consistency_report_clean(self):
        engine.run_all()
        report = engine.consistency_report()
        assert report["一致"] is True
        assert report["重复状态键"] == []
        assert report["裁决版本不一致"] == []

    def test_persistence_across_reads(self):
        engine.run_all()
        first = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        # 模拟刷新/重新进入：重新读取必须得到同一裁决版本与连续周期
        second = _find(engine.list_states("METR-0003"), 周期="2026-W38")
        assert first["裁决版本"] == second["裁决版本"]
        assert first["连续周期数"] == second["连续周期数"]
        assert first["口径版本"] == second["口径版本"]

    def test_notification_retired_when_no_longer_alerting(self):
        engine.run_all()
        # W37 初始为预警，存在通知
        assert _find(engine.list_notifications(), 指标编号="METR-0003", 周期="2026-W37")
        # 关闭胜出规则 ALER-0002，强制重裁后该周期不再预警
        service = AlertconfigService()
        service.run_action(2, "关闭规则")
        engine.arbitrate_period("METR-0003", "2026-W37", force=True)
        state = _find(engine.list_states("METR-0003"), 周期="2026-W37")
        assert state["是否预警"] is False
        leftover = [
            n for n in engine.list_notifications()
            if n["指标编号"] == "METR-0003" and n["周期"] == "2026-W37"
        ]
        assert leftover == []
        assert engine.consistency_report()["一致"] is True


# ----------------------------------------------------------------------
# 规则维护校验
# ----------------------------------------------------------------------
class TestRuleMaintenance:
    def test_create_rule_binds_current_caliber(self):
        service = AlertconfigService()
        entry, missing = service.create_entry(
            {
                "预警编号": "ALER-0010",
                "关联指标": "METR-0001",
                "比较运算符": ">",
                "阈值数值": "80",
                "持续周期数": "1",
                "预警级别": "P3",
                "通知接收人": "x@example.com",
                "生效时间": "2026-09-10 09:00:00",
            }
        )
        assert not missing
        assert entry["口径版本"] == "v1"
        assert entry["修订版本"] == 1

    def test_create_rule_rejects_unknown_metric(self):
        service = AlertconfigService()
        _, missing = service.create_entry(
            {"预警编号": "ALER-0011", "关联指标": "METR-9999", "比较运算符": ">"}
        )
        assert any("不存在" in item for item in missing)

    def test_create_rule_rejects_bad_operator(self):
        service = AlertconfigService()
        _, missing = service.create_entry(
            {"预警编号": "ALER-0012", "关联指标": "METR-0001", "比较运算符": "=="}
        )
        assert any("运算符" in item for item in missing)

    def test_active_rule_cannot_be_modified_directly(self):
        service = AlertconfigService()
        entry, message = service.update_entry(2, {"阈值数值": "1"})
        assert entry is None
        assert "已生效" in message

    def test_activate_rule_rewrites_current_caliber(self):
        service = AlertconfigService()
        MetricService().bump_caliber_version(1)  # v1 -> v2
        # 新建草稿规则后生效，应回写当前版本 v2
        entry, _ = service.create_entry(
            {
                "预警编号": "ALER-0013",
                "关联指标": "METR-0001",
                "比较运算符": ">",
                "阈值数值": "80",
                "持续周期数": "1",
                "预警级别": "P3",
                "生效时间": "2026-09-10 09:00:00",
            }
        )
        activated, _ = service.run_action(entry["id"], "提交规则评审")
        activated, _ = service.run_action(entry["id"], "生效规则")
        assert activated["口径版本"] == "v2"

    def test_bind_rejects_non_current_version(self):
        service = AlertconfigService()
        entry, message = service.bind_metric_version(2, "v999")
        assert entry is None
        assert "当前版本" in message

    def test_bad_observation_raises_readable_error(self):
        with pytest.raises(ArbitrationError, match="观测值"):
            engine.ingest_period("METR-0001", "2026-W40", "not-a-number")

    def test_fingerprint_changes_after_material_edit(self):
        before = rule_fingerprint(_rule(6))
        AlertconfigService().update_entry(6, {"阈值数值": "99.9"})
        after = rule_fingerprint(_rule(6))
        assert before != after
