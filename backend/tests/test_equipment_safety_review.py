# -*- coding: utf-8 -*-
"""设备安全复核专项测试。

以两台同名、不同出厂编号（306、307）的设备复现「审核扇出」根因，
并覆盖：按出厂编号派复核、同名设备隔离、保管交接责任解释、重复回执幂等、
唯一责任人、领用核销/拒绝、旧日志待补查、向航次日志回传接手班组等规则。
"""
from __future__ import annotations

import pytest

from app.store import store
from app.services import safety_review
from app.services.cruise_log import CruiseLogService
from app.services.equipment_manifest import EquipmentManifestService

SAME_NAME = "温盐深仪 CTD-90"


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


def _build_two_same_named_devices() -> tuple[dict, dict]:
    """构造两台同名设备：出厂编号 306 与 307，分属不同保管班组。"""
    equip_rows = store.rows("equipment_manifest")
    for row in equip_rows:
        row.pop("log_alert", None)
        row.pop("safety_review_required", None)
        row.pop("usage_blocked", None)
    e306 = equip_rows[0]
    e306.update({
        "设备名称": SAME_NAME, "出厂编号": "306", "status": "已启用",
        "保管班组": "甲板一班", "保管人": "张三", "pending": False,
    })
    e307 = equip_rows[1]
    e307.update({
        "设备名称": SAME_NAME, "出厂编号": "307", "status": "已启用",
        "保管班组": "甲板二班", "保管人": "李四", "pending": False,
    })
    return e306, e307


def _make_log(entry_id: int, serial: str, *, event: str = "探头读数跳变，疑似故障") -> dict:
    log = store.find("cruise_log", entry_id)
    log.update({
        "异常事件": event,
        "设备运行": "设备异常",
        "出厂编号": serial,
        "status": "待填写",
        "has_abnormal": False,
    })
    for key in ("log_alert", "identity_pending_lookup", "复核指派单号", "复核接手班组", "复核结论"):
        log.pop(key, None)
    return log


def _submit_and_approve(log_id: int) -> dict:
    svc = CruiseLogService()
    entry, msg = svc.run_action(log_id, "提交日志")
    assert entry is not None, f"提交日志失败: {msg}"
    entry, msg = svc.run_action(log_id, "审核通过")
    assert entry is not None, f"审核失败: {msg}"
    return entry


# ====================================================================
# 1. 复现审核扇出根因：306 异常只能向 306 当前保管班组派复核，307 保留原资格
# ====================================================================
def test_review_fan_out_only_targets_named_serial():
    e306, e307 = _build_two_same_named_devices()
    _make_log(1, "306")
    approved = _submit_and_approve(1)

    # 306 收到且仅收到一份复核指派
    assignments_306 = safety_review.assignments_for_serial("306")
    assert len(assignments_306) == 1
    assignment = assignments_306[0]
    assert assignment["出厂编号"] == "306"
    assert assignment["equipment_id"] == e306["id"]
    # 只能派给该设备的当前保管班组
    assert assignment["责任班组"] == "甲板一班"
    # 指派记录能追到原日志
    assert assignment["source_log_id"] == 1
    assert approved["复核指派单号"] == assignment["id"]

    # 另一台同名设备 307 保留原资格：无告警、无复核要求、不被封锁
    assert not e307.get("log_alert")
    assert not e307.get("safety_review_required")
    assert not e307.get("usage_blocked")
    assert safety_review.assignments_for_serial("307") == []

    # 全表不再被扇出
    alerted = [r for r in store.rows("equipment_manifest") if r.get("log_alert")]
    assert [r["出厂编号"] for r in alerted] == ["306"]
    blocked = [r for r in store.rows("equipment_manifest") if r.get("usage_blocked")]
    assert [r["出厂编号"] for r in blocked] == ["306"]


# ====================================================================
# 2. 审核回执重复提交只保留一份有效指派，原持有人仍可回查
# ====================================================================
def test_duplicate_review_receipt_keeps_single_assignment():
    e306, _ = _build_two_same_named_devices()
    _make_log(1, "306")
    approved = _submit_and_approve(1)
    first_id = approved["复核指派单号"]

    # 再次对同一日志触发审核升级（模拟审核回执重送）
    CruiseLogService()._escalate_abnormal_event(store.find("cruise_log", 1))
    rows = safety_review.assignments_for_serial("306")
    assert len(rows) == 1, "重复回执不得产生第二份指派"
    assert rows[0]["id"] == first_id
    # 原持有人/原班组仍保留在指派单上可回查
    assert rows[0]["原保管班组"] == "甲板一班"
    assert 1 in rows[0]["related_log_ids"]


# ====================================================================
# 3. 复核未办结时唯一责任人：同一设备并发多份日志也只有一份开放指派
# ====================================================================
def test_single_responsible_party_while_review_open():
    e306, _ = _build_two_same_named_devices()
    _make_log(1, "306", event="第一次异常")
    _make_log(2, "306", event="第二次异常")
    _submit_and_approve(1)
    _submit_and_approve(2)

    open_rows = [
        r for r in safety_review.assignments_for_serial("306")
        if r["status"] in safety_review.ASSIGNMENT_OPEN_STATUSES
    ]
    assert len(open_rows) == 1, "复核未办结必须只有唯一责任人"
    assert open_rows[0]["责任班组"] == "甲板一班"
    # 第二份日志关联到同一张单，且都能追溯
    assert set(open_rows[0]["related_log_ids"]) == {1, 2}
    assert e306["review_responsible_team"] == "甲板一班"


# ====================================================================
# 4. 交错提交审核与设备交接：交接中由原班组担责，完成后责任转移
# ====================================================================
def test_interleaved_review_and_transfer_responsibility():
    e306, e307 = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()

    _make_log(1, "306")
    _submit_and_approve(1)
    assignment = safety_review.assignments_for_serial("306")[0]
    assert assignment["责任班组"] == "甲板一班"

    # 日志刚送审后发起交接：两份保管凭据并存
    transfer, msg = equip_svc.start_transfer(e306["id"], new_team="甲板三班", new_keeper="王五")
    assert transfer is not None
    # 交接未完成，复核仍由原班组承担，指派单能解释现在谁担责、设备能否领用
    assignment = safety_review.assignments_for_serial("306")[0]
    assert assignment["责任班组"] == "甲板一班"
    assert "交接" in assignment["交接说明"] and "原保管班组" in assignment["交接说明"]

    # 交接期间领用被拦（设备还不可继续领用）
    entry, usage_msg = equip_svc.request_usage(e306["id"])
    assert entry is None and "尚未办结" in usage_msg

    # 完成交接：责任唯一转移到接手班组，原持有人留痕
    transfer, msg = equip_svc.complete_transfer(e306["id"])
    assert transfer is not None
    assignment = safety_review.assignments_for_serial("306")[0]
    assert assignment["责任班组"] == "甲板三班"
    assert assignment["原保管班组"] == "甲板一班"  # 原持有人仍可回查
    assert e306["保管班组"] == "甲板三班"
    # 只有一张单，责任人始终唯一
    assert len(safety_review.assignments_for_serial("306")) == 1
    # 307 全程不受影响
    assert e307["保管班组"] == "甲板二班"


# ====================================================================
# 5. 交接完成后再重送审核记录：复用同一单，责任班组为当前接手班组
# ====================================================================
def test_resend_review_after_transfer_reuses_open_assignment():
    e306, _ = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)

    equip_svc.start_transfer(e306["id"], new_team="甲板三班")
    equip_svc.complete_transfer(e306["id"])

    # 重送审核记录
    CruiseLogService()._escalate_abnormal_event(store.find("cruise_log", 1))
    rows = safety_review.assignments_for_serial("306")
    assert len(rows) == 1
    assert rows[0]["责任班组"] == "甲板三班"


# ====================================================================
# 6. 旧日志缺少能区分设备的编号：留待补查，不得用任意一台办复核
# ====================================================================
def test_missing_serial_held_for_lookup_no_arbitrary_review():
    e306, e307 = _build_two_same_named_devices()
    log = _make_log(1, "")
    svc = CruiseLogService()
    svc.run_action(1, "提交日志")
    svc.run_action(1, "审核通过")

    assert store.find("cruise_log", 1)["identity_pending_lookup"] is True
    assert "出厂编号" in store.find("cruise_log", 1)["lookup_reason"]
    # 两台设备都不应被派复核
    assert safety_review.assignments_for_serial("306") == []
    assert safety_review.assignments_for_serial("307") == []
    assert not e306.get("log_alert") and not e307.get("log_alert")

    # 同名多台时即便给了名称也不能用任意一台：构造编号重复场景
    e307["出厂编号"] = "306"  # 人为制造两台同编号（数据脏数据）
    log2 = _make_log(2, "306")
    svc.run_action(2, "提交日志")
    assert log2["identity_pending_lookup"] is True
    assert "多台" in log2["lookup_reason"]


# ====================================================================
# 7. 补查出厂编号后恢复正常派单
# ====================================================================
def test_supplement_identity_then_review_created():
    e306, _ = _build_two_same_named_devices()
    _make_log(1, "")
    svc = CruiseLogService()
    svc.run_action(1, "提交日志")
    svc.run_action(1, "审核通过")

    entry, msg = svc.supplement_identity(1, "306")
    assert entry is not None, msg
    # 日志已审核，补查后立即补派
    rows = safety_review.assignments_for_serial("306")
    assert len(rows) == 1
    assert store.find("cruise_log", 1)["复核指派单号"] == rows[0]["id"]

    # 补查一个查无此编号的值应被拒绝
    entry, msg = svc.supplement_identity(1, "999")
    assert entry is None and "查无" in msg


# ====================================================================
# 8. 领用：未办结拦截；核销放行；拒绝持续拦截，并给出具体复核依据
# ====================================================================
def test_usage_gated_by_assignment_settlement():
    e306, e307 = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)
    assignment_id = safety_review.assignments_for_serial("306")[0]["id"]

    # 未办结：306 领用被拦且依据具体到单号，307 可正常领用
    entry, msg = equip_svc.request_usage(e306["id"])
    assert entry is None and str(assignment_id) in msg
    entry307, msg307 = equip_svc.request_usage(e307["id"])
    assert entry307 is not None

    # 拒绝：维持封锁，后续领用继续被拦且带回拒绝原因
    _, reject_msg = equip_svc.settle_review(assignment_id, action="拒绝", reason="探头损坏待修")
    assert "已拒绝" in reject_msg
    entry, msg = equip_svc.request_usage(e306["id"])
    assert entry is None and "已被拒绝" in msg and "探头损坏" in msg

    # 重新送审产生新单并核销后放行
    _make_log(2, "306", event="更换探头后复测")
    _submit_and_approve(2)
    new_assignment = safety_review.open_assignment_for_serial("306")
    assert new_assignment is not None and new_assignment["id"] != assignment_id
    _, msg = equip_svc.settle_review(new_assignment["id"], action="核销", reason="复测合格")
    assert "已核销" in msg
    entry, msg = equip_svc.request_usage(e306["id"])
    assert entry is not None and "可正常领用" in msg
    assert e306["usage_blocked"] is False


# ====================================================================
# 9. 核销/拒绝向航次日志回传被哪一班接手
# ====================================================================
def test_settlement_feeds_handover_team_back_to_log():
    e306, _ = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)
    assignment = safety_review.assignments_for_serial("306")[0]

    equip_svc.start_transfer(e306["id"], new_team="甲板三班")
    equip_svc.complete_transfer(e306["id"])
    equip_svc.settle_review(assignment["id"], action="核销", reason="检查通过")

    log = store.find("cruise_log", 1)
    assert log["复核接手班组"] == "甲板三班"
    assert log["复核结论"] == "已核销"
    assert log["复核指派单号"] == assignment["id"]


# ====================================================================
# 10. 设备详情关联实际出厂编号的复核指派单，物资员可取得具体依据
# ====================================================================
def test_equipment_detail_links_review_basis_by_serial():
    e306, e307 = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)

    detail306 = equip_svc.equipment_detail(e306["id"])
    assert detail306 is not None
    assert detail306["有效复核指派单"]["出厂编号"] == "306"
    assert detail306["有效复核指派单"]["source_log_id"] == 1
    assert detail306["全部复核依据"][0]["复核依据"]

    detail307 = equip_svc.equipment_detail(e307["id"])
    assert detail307["有效复核指派单"] is None
    assert detail307["全部复核依据"] == []

    # 按出厂编号回查接口数据
    assert len(safety_review.assignments_for_serial("306")) == 1
    assert safety_review.assignments_for_serial("307") == []


# ====================================================================
# 11. 交接取消不改变复核责任；同一设备不能并发两份交接凭据
# ====================================================================
def test_cancel_transfer_keeps_original_responsibility():
    e306, _ = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)

    equip_svc.start_transfer(e306["id"], new_team="甲板三班")
    # 第二份交接凭据应被拒绝
    again, msg = equip_svc.start_transfer(e306["id"], new_team="甲板四班")
    assert again is None and "已有" in msg

    transfer, _ = equip_svc.cancel_transfer(e306["id"])
    assert transfer["status"] == "已取消"
    assert safety_review.assignments_for_serial("306")[0]["责任班组"] == "甲板一班"
    assert e306["保管班组"] == "甲板一班"


# ====================================================================
# 12. 已办结指派不能重复核销/拒绝
# ====================================================================
def test_closed_assignment_cannot_be_settled_twice():
    e306, _ = _build_two_same_named_devices()
    equip_svc = EquipmentManifestService()
    _make_log(1, "306")
    _submit_and_approve(1)
    assignment_id = safety_review.assignments_for_serial("306")[0]["id"]

    ok, _ = equip_svc.settle_review(assignment_id, action="核销")
    assert ok is not None
    again, msg = equip_svc.settle_review(assignment_id, action="核销")
    assert again is None and "已办结" in msg
