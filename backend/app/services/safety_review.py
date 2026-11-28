# -*- coding: utf-8 -*-
"""设备安全复核与保管交接领域辅助逻辑。

复核指派单（safety_review_assignment）与保管交接单（equipment_custody_transfer）
都存放在内存仓库的独立表中，供航次日志与设备清单两个领域服务共享，避免互相导入形成循环依赖。

核心约束：
- 复核指派必须按「出厂编号」锁定唯一一台设备，同名设备不得互相串单；
- 同一台设备复核未办结前只允许存在一份有效（开放状态）指派，即唯一责任人；
- 交接未完成前原保管班组继续承担复核责任，交接完成后责任才转移，原持有人始终留痕可查。
"""
from __future__ import annotations

from typing import Any

from app.store import store

ASSIGNMENT_TABLE = "safety_review_assignment"
TRANSFER_TABLE = "equipment_custody_transfer"

# 复核指派单状态
ASSIGNMENT_OPEN_STATUSES = ("待复核", "复核中")
ASSIGNMENT_CLOSED_STATUSES = ("已核销", "已拒绝")

# 保管交接单状态
TRANSFER_OPEN_STATUS = "交接中"
TRANSFER_DONE_STATUS = "已完成"
TRANSFER_CANCELLED_STATUS = "已取消"


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def find_equipment_by_serial(serial: str) -> list[dict[str, Any]]:
    """按出厂编号精确查找设备，返回全部匹配（正常应至多一台）。"""
    serial = str(serial or "").strip()
    if not serial:
        return []
    return [
        row for row in store.rows("equipment_manifest")
        if str(row.get("出厂编号", "")).strip() == serial
    ]


def current_custody_team(equipment: dict[str, Any]) -> str:
    """解析设备当前承担保管责任的班组。

    存在「交接中」的保管凭据时，实物尚未移交，仍由原保管班组承担责任；
    交接完成后取设备自身登记的保管班组，缺失时回退到保管人字段。
    """
    open_transfer = find_open_transfer(int(equipment.get("id", 0)))
    if open_transfer is not None:
        return str(open_transfer.get("原保管班组", ""))
    team = equipment.get("保管班组")
    if str(team or "").strip():
        return str(team)
    return str(equipment.get("保管人", "") or "")


def find_open_transfer(equipment_id: int) -> dict[str, Any] | None:
    """返回设备当前正在交接（未完成、未取消）的保管凭据。"""
    for row in store.rows(TRANSFER_TABLE):
        if int(row.get("equipment_id", 0)) == equipment_id and row.get("status") == TRANSFER_OPEN_STATUS:
            return row
    return None


def list_transfers(equipment_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(TRANSFER_TABLE)
        if int(row.get("equipment_id", 0)) == equipment_id
    ]


def open_assignment_for_serial(serial: str) -> dict[str, Any] | None:
    """返回该出厂编号当前唯一一份未办结的复核指派单。"""
    serial = str(serial or "").strip()
    for row in store.rows(ASSIGNMENT_TABLE):
        if str(row.get("出厂编号", "")).strip() == serial and row.get("status") in ASSIGNMENT_OPEN_STATUSES:
            return row
    return None


def find_assignment(assignment_id: int) -> dict[str, Any] | None:
    return store.find(ASSIGNMENT_TABLE, assignment_id)


def assignments_for_serial(serial: str) -> list[dict[str, Any]]:
    """物资员按出厂编号回查全部复核依据（含已办结）。"""
    serial = str(serial or "").strip()
    return [
        row for row in store.rows(ASSIGNMENT_TABLE)
        if str(row.get("出厂编号", "")).strip() == serial
    ]


def latest_assignment_for_serial(serial: str) -> dict[str, Any] | None:
    rows = assignments_for_serial(serial)
    return rows[-1] if rows else None


def get_or_create_assignment(
    *,
    equipment: dict[str, Any],
    source_log: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """按出厂编号取得或创建复核指派单。

    同一设备已有未办结指派时直接复用原单（重复审核回执不产生第二份指派），
    新日志编号追加到关联日志列表，原指派单仍可追溯到最初的日志与持有人。
    返回 (指派单, 是否新建)。
    """
    serial = str(equipment.get("出厂编号", "")).strip()
    existing = open_assignment_for_serial(serial)
    if existing is not None:
        related = existing.setdefault("related_log_ids", [])
        log_id = int(source_log.get("id", 0))
        if log_id and log_id not in related:
            related.append(log_id)
        return existing, False

    responsible_team = current_custody_team(equipment)
    open_transfer = find_open_transfer(int(equipment.get("id", 0)))
    transfer_note = ""
    if open_transfer is not None:
        transfer_note = (
            f"送审时设备正处于保管交接中（交接单{open_transfer.get('id')}："
            f"{open_transfer.get('原保管班组')} → {open_transfer.get('新保管班组')}），"
            "交接完成前由原保管班组承担本次复核责任"
        )

    assignment = {
        "id": _next_id(ASSIGNMENT_TABLE),
        "status": "待复核",
        "pending": True,
        "abnormal": True,
        "出厂编号": serial,
        "设备名称": equipment.get("设备名称", ""),
        "equipment_id": int(equipment.get("id", 0)),
        "source_log_id": int(source_log.get("id", 0)),
        "related_log_ids": [int(source_log.get("id", 0))],
        "责任班组": responsible_team,
        "原保管班组": responsible_team,
        "复核依据": f"航次日志{source_log.get('id')}记录异常：{source_log.get('异常事件', '')}",
        "交接说明": transfer_note,
        "拒绝原因": "",
        "接手班组": "",
        "处理说明": "",
    }
    store.rows(ASSIGNMENT_TABLE).append(assignment)

    equipment["safety_review_required"] = True
    equipment["usage_blocked"] = True
    equipment["review_assignment_id"] = assignment["id"]
    equipment["review_responsible_team"] = responsible_team
    equipment["review_status"] = "待复核"
    source_log["复核指派单号"] = assignment["id"]
    return assignment, True


def reassign_after_transfer(equipment: dict[str, Any], transfer: dict[str, Any]) -> dict[str, Any] | None:
    """保管交接完成后，把未办结复核的责任班组原子切换为接手班组。

    责任始终唯一：原班组责任解除，新班组接手；原持有人信息保留在指派单上可回查。
    """
    assignment = open_assignment_for_serial(str(equipment.get("出厂编号", "")))
    if assignment is None:
        return None
    new_team = str(transfer.get("新保管班组", ""))
    assignment["责任班组"] = new_team
    note = (
        f"保管交接单{transfer.get('id')}已完成，复核责任由{transfer.get('原保管班组')}"
        f"移交至{new_team}；原保管班组{transfer.get('原保管班组')}仍可回查本单"
    )
    assignment["交接说明"] = note
    equipment["review_responsible_team"] = new_team
    transfer["接手复核单号"] = assignment["id"]
    return assignment


def close_assignment(
    assignment_id: int,
    *,
    action: str,
    reason: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """核销或拒绝复核指派单，并向航次日志回传接手班组与结论。"""
    assignment = find_assignment(assignment_id)
    if assignment is None:
        return None, f"复核指派单 {assignment_id} 不存在"
    if assignment.get("status") not in ASSIGNMENT_OPEN_STATUSES:
        return None, f"复核指派单 {assignment_id} 已办结（{assignment.get('status')}），不能重复处理"

    if action == "核销":
        target_status = "已核销"
    elif action == "拒绝":
        target_status = "已拒绝"
    else:
        return None, "复核办结动作只支持「核销」或「拒绝」"

    assignment["status"] = target_status
    assignment["pending"] = False
    assignment["接手班组"] = assignment.get("责任班组", "")
    assignment["处理说明"] = reason or assignment.get("处理说明", "")
    if target_status == "已拒绝" and reason:
        assignment["拒绝原因"] = reason

    equipment = store.find("equipment_manifest", int(assignment.get("equipment_id", 0)))
    if equipment is not None:
        equipment["review_status"] = target_status
        equipment["review_assignment_id"] = assignment["id"]
        if target_status == "已核销":
            # 核销后解除领用封锁，设备恢复可用
            equipment["usage_blocked"] = False
            equipment["safety_review_required"] = False
        else:
            # 拒绝核销时维持封锁，后续领用继续被拦
            equipment["usage_blocked"] = True
            equipment["safety_review_required"] = True

    # 向航次日志回传：被哪一班接手、结论是什么
    source_log = store.find("cruise_log", int(assignment.get("source_log_id", 0)))
    if source_log is not None:
        source_log["复核接手班组"] = assignment["接手班组"]
        source_log["复核指派单号"] = assignment["id"]
        source_log["复核结论"] = target_status
    for log_id in assignment.get("related_log_ids", []):
        related_log = store.find("cruise_log", int(log_id))
        if related_log is not None:
            related_log["复核接手班组"] = assignment["接手班组"]
            related_log["复核结论"] = target_status
    return assignment, f"复核指派单 {assignment_id} 已办结，结论：{target_status}"


def usage_decision(equipment: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """物资员领用设备时的复核校验，返回(是否放行, 说明, 具体复核依据)。"""
    serial = str(equipment.get("出厂编号", "")).strip()
    open_one = open_assignment_for_serial(serial)
    if open_one is not None:
        basis = dict(open_one)
        return False, (
            f"设备出厂编号{serial}的复核指派单{open_one['id']}尚未办结"
            f"（责任班组：{open_one.get('责任班组')}），暂不能领用"
        ), basis

    latest = latest_assignment_for_serial(serial)
    if latest is not None and latest.get("status") == "已拒绝":
        basis = dict(latest)
        return False, (
            f"设备出厂编号{serial}的复核指派单{latest['id']}已被拒绝"
            f"（原因：{latest.get('拒绝原因') or '未填写'}），暂停领用"
        ), basis
    if latest is not None and latest.get("status") == "已核销":
        return True, f"复核指派单{latest['id']}已核销，设备可正常领用", dict(latest)
    return True, "该设备无未办结复核，可正常领用", None
