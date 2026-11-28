# -*- coding: utf-8 -*-
"""设备清单领域服务：管理科考设备全生命周期、保管交接与安全复核联动。

复核相关规则：
- 复核指派单按出厂编号绑定具体设备，设备详情可回查具体复核依据；
- 物资员领用前必须通过复核校验：未办结或被拒绝的指派单会拦住后续领用；
- 保管交接期间（两份保管凭据并存）原班组继续担责，交接完成后责任唯一转移到接手班组。
"""
from __future__ import annotations

from typing import Any

from app.services import safety_review
from app.services.base import BaseModuleService
from app.store import store


class EquipmentManifestService(BaseModuleService):
    MODULE = "equipment_manifest"
    ENTITY = "设备记录"
    OPERATION_TERM = "校验设备"
    LIST_FIELDS = [
        "设备名称", "设备类型", "出厂编号", "校准日期", "当前状态",
        "保管班组", "保管人", "所在位置", "备注说明",
    ]
    REQUIRED_FIELDS = ["设备名称", "设备类型", "出厂编号"]
    STATUS_ORDER = ["待校验", "已校验", "已启用", "已归还"]
    ACTION_RULES = {"校验设备": "已校验", "启用设备": "已启用", "归还设备": "已归还"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        # 启用设备前需确认已校验
        if action == "启用设备" and entry.get("status") != "已校验":
            return None, "只能启用已校验的设备"

        entry["status"] = target
        entry["pending"] = target in ("待校验", "已校验")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        """按设备名称查找（同名多台时返回第一台，跨模块停用场景使用）。"""
        return store.find_one_by(self.MODULE, "设备名称", name)

    def find_by_serial(self, serial: str) -> dict[str, Any] | None:
        """按出厂编号查找唯一设备；同名多台时编号是唯一可信身份。"""
        matches = safety_review.find_equipment_by_serial(serial)
        if len(matches) > 1:
            return None
        return matches[0] if matches else None

    def disable_equipment(self, equipment_name: str) -> tuple[dict[str, Any] | None, str]:
        """仪器校准不合格时自动停用设备。"""
        equipment = self.find_by_name(equipment_name)
        if equipment is None:
            return None, f"设备「{equipment_name}」不在清单中"
        if equipment.get("status") == "已归还":
            return equipment, "设备已归还，无需停用"
        equipment["status"] = "已归还"
        equipment["pending"] = False
        equipment["abnormal"] = True
        equipment["disable_reason"] = "仪器校准不合格"
        return equipment, f"设备「{equipment_name}」因校准不合格已自动停用"

    # ------------------------------------------------------------------
    # 保管交接：解释两份保管凭据并存时由谁担责
    # ------------------------------------------------------------------
    def start_transfer(
        self,
        equipment_id: int,
        *,
        new_team: str,
        new_keeper: str = "",
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """发起设备保管交接，生成一份「交接中」凭据；原凭据在完成前继续有效。"""
        equipment = store.find(self.MODULE, equipment_id)
        if equipment is None:
            return None, f"{self.ENTITY} {equipment_id} 不存在"
        new_team = str(new_team or "").strip()
        if not new_team:
            return None, "新保管班组不能为空"
        if safety_review.find_open_transfer(equipment_id) is not None:
            return None, "该设备已有一份交接中的保管凭据，请先完成或取消后再发起"

        old_team = str(equipment.get("保管班组", "") or equipment.get("保管人", ""))
        transfer = {
            "id": max((int(r.get("id", 0)) for r in store.rows(safety_review.TRANSFER_TABLE)), default=0) + 1,
            "status": safety_review.TRANSFER_OPEN_STATUS,
            "pending": True,
            "abnormal": False,
            "equipment_id": equipment_id,
            "出厂编号": equipment.get("出厂编号", ""),
            "设备名称": equipment.get("设备名称", ""),
            "原保管班组": old_team,
            "新保管班组": new_team,
            "新保管人": new_keeper,
            "交接说明": note,
            "接手复核单号": "",
        }
        store.rows(safety_review.TRANSFER_TABLE).append(transfer)
        equipment["transfer_id"] = transfer["id"]
        equipment["transfer_in_progress"] = True

        # 交接未完成，未办结复核仍由原班组承担，并在设备上写明解释
        open_assignment = safety_review.open_assignment_for_serial(str(equipment.get("出厂编号", "")))
        if open_assignment is not None:
            open_assignment["交接说明"] = (
                f"保管交接单{transfer['id']}发起中（{old_team} → {new_team}），"
                "交接完成前由原保管班组承担本次复核，设备暂不能领用"
            )
            equipment["review_responsible_team"] = open_assignment.get("责任班组", "")
        return transfer, (
            f"设备{equipment_id}保管交接已发起：{old_team} → {new_team}；"
            "交接完成前原保管班组继续承担复核责任，设备暂停领用"
        )

    def complete_transfer(self, equipment_id: int) -> tuple[dict[str, Any] | None, str]:
        """完成交接：实物移交，保管责任唯一转移到接手班组。"""
        equipment = store.find(self.MODULE, equipment_id)
        if equipment is None:
            return None, f"{self.ENTITY} {equipment_id} 不存在"
        transfer = safety_review.find_open_transfer(equipment_id)
        if transfer is None:
            return None, "该设备没有交接中的保管凭据"

        transfer["status"] = safety_review.TRANSFER_DONE_STATUS
        transfer["pending"] = False
        equipment["transfer_in_progress"] = False
        equipment["transfer_id"] = ""
        equipment["保管班组"] = transfer["新保管班组"]
        if transfer.get("新保管人"):
            equipment["保管人"] = transfer["新保管人"]

        # 未办结复核的责任班组原子切换；原持有人仍保留在指派单上可回查
        assignment = safety_review.reassign_after_transfer(equipment, transfer)
        if assignment is not None:
            equipment["review_responsible_team"] = assignment["责任班组"]
            msg = (
                f"交接完成，设备{equipment_id}保管责任移交至{transfer['新保管班组']}；"
                f"未办结复核单{assignment['id']}同步改派，原班组{transfer['原保管班组']}仍可回查"
            )
        else:
            msg = f"交接完成，设备{equipment_id}保管责任移交至{transfer['新保管班组']}"
        return transfer, msg

    def cancel_transfer(self, equipment_id: int) -> tuple[dict[str, Any] | None, str]:
        """取消交接：恢复为原保管班组单独持有，复核责任不变。"""
        equipment = store.find(self.MODULE, equipment_id)
        if equipment is None:
            return None, f"{self.ENTITY} {equipment_id} 不存在"
        transfer = safety_review.find_open_transfer(equipment_id)
        if transfer is None:
            return None, "该设备没有交接中的保管凭据"
        transfer["status"] = safety_review.TRANSFER_CANCELLED_STATUS
        transfer["pending"] = False
        equipment["transfer_in_progress"] = False
        equipment["transfer_id"] = ""
        return transfer, f"交接单{transfer['id']}已取消，设备仍由{transfer['原保管班组']}保管"

    # ------------------------------------------------------------------
    # 复核指派单核销 / 拒绝
    # ------------------------------------------------------------------
    def settle_review(
        self,
        assignment_id: int,
        *,
        action: str,
        reason: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        return safety_review.close_assignment(assignment_id, action=action, reason=reason)

    # ------------------------------------------------------------------
    # 物资员领用：用复核指派单核销或拒绝指定设备的后续领用
    # ------------------------------------------------------------------
    def request_usage(self, equipment_id: int) -> tuple[dict[str, Any] | None, str]:
        """物资员尝试领用设备，返回具体复核依据。"""
        equipment = store.find(self.MODULE, equipment_id)
        if equipment is None:
            return None, f"{self.ENTITY} {equipment_id} 不存在"
        allowed, message, basis = safety_review.usage_decision(equipment)
        if not allowed:
            return None, message
        equipment["last_usage_basis"] = (
            f"复核指派单{basis['id']}（{basis.get('status')}）" if basis else "无未办结复核"
        )
        return equipment, message

    # ------------------------------------------------------------------
    # 设备详情：关联复核指派单与保管交接，提供具体复核依据
    # ------------------------------------------------------------------
    def equipment_detail(self, equipment_id: int) -> dict[str, Any] | None:
        equipment = store.find(self.MODULE, equipment_id)
        if equipment is None:
            return None
        serial = str(equipment.get("出厂编号", ""))
        open_assignment = safety_review.open_assignment_for_serial(serial)
        latest = safety_review.latest_assignment_for_serial(serial)
        return {
            "equipment": dict(equipment),
            "当前责任班组": safety_review.current_custody_team(equipment),
            "有效复核指派单": dict(open_assignment) if open_assignment else None,
            "最近复核指派单": dict(latest) if latest else None,
            "全部复核依据": [dict(a) for a in safety_review.assignments_for_serial(serial)],
            "保管交接记录": [dict(t) for t in safety_review.list_transfers(equipment_id)],
        }
