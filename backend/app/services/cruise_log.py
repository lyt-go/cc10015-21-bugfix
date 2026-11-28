# -*- coding: utf-8 -*-
"""航次日志领域服务：记录异常事件并按出厂编号触发设备安全复核联动。

修复要点（审核入口丢失设备身份的根因）：
- 异常联动必须依据日志里的「出厂编号」锁定唯一一台设备，禁止再对设备清单全表打标记；
- 日志缺少可区分设备的编号，或编号查不到/对应多台同名设备时，只挂「待补查」标记，
  绝不能任选一台设备办复核；
- 审核通过后才生成（或复用）按出厂编号核销的复核指派单，指派单可追溯到原日志。
"""
from __future__ import annotations

from typing import Any

from app.services import safety_review
from app.services.base import BaseModuleService
from app.store import store


class CruiseLogService(BaseModuleService):
    MODULE = "cruise_log"
    ENTITY = "航海日志"
    OPERATION_TERM = "提交日志"
    LIST_FIELDS = [
        "记录日期", "船舶位置", "作业内容", "设备运行", "异常事件",
        "出厂编号", "记录人", "值班班组", "备注说明",
    ]
    REQUIRED_FIELDS = ["记录日期", "船舶位置", "作业内容"]
    STATUS_ORDER = ["待填写", "已填写", "已审核", "已归档"]
    ACTION_RULES = {"提交日志": "已填写", "审核通过": "已审核", "归档日志": "已归档"}
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

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("待填写", "已填写")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        # 提交日志时如果包含异常事件，按出厂编号定位设备
        if action == "提交日志":
            abnormal_event = entry.get("异常事件", "")
            if abnormal_event and "无" not in abnormal_event and str(abnormal_event).strip():
                entry["has_abnormal"] = True
                self._log_abnormal_event(entry)

        # 审核通过时处理异常事件联动：生成/复用复核指派单
        if action == "审核通过" and entry.get("has_abnormal"):
            self._escalate_abnormal_event(entry)

        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 设备身份解析
    # ------------------------------------------------------------------
    def _resolve_equipment(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """依据日志携带的出厂编号解析唯一设备。

        返回 (设备, 待补查原因)；无法唯一定位时设备为 None，原因非空。
        """
        serial = str(entry.get("出厂编号", "") or "").strip()
        if not serial:
            return None, "旧日志缺少能区分设备的出厂编号，留待补查，暂不派复核"
        matches = safety_review.find_equipment_by_serial(serial)
        if not matches:
            return None, f"出厂编号{serial}在设备清单中查无对应设备，留待补查"
        if len(matches) > 1:
            return None, f"出厂编号{serial}对应多台设备，编号不能唯一区分，留待补查"
        return matches[0], ""

    def _mark_pending_lookup(self, entry: dict[str, Any], reason: str) -> None:
        entry["identity_pending_lookup"] = True
        entry["lookup_reason"] = reason
        entry["复核状态"] = "待补查"

    # ------------------------------------------------------------------
    # 提交阶段：只给被点名的那一台设备挂告警，绝不全表扇出
    # ------------------------------------------------------------------
    def _log_abnormal_event(self, entry: dict[str, Any]) -> None:
        equipment, reason = self._resolve_equipment(entry)
        if equipment is None:
            # 缺编号/查不到/同名多台：留待补查，不向任何设备派复核
            self._mark_pending_lookup(entry, reason)
            return

        equipment["log_alert"] = True
        equipment["log_alert_msg"] = f"航次日志{entry.get('id')}记录异常：{entry.get('异常事件', '')}"
        equipment["log_alert_log_id"] = entry.get("id")
        equipment["log_alert_serial"] = str(equipment.get("出厂编号", ""))
        entry["异常设备id"] = equipment.get("id")
        entry["复核状态"] = "待审核"

    # ------------------------------------------------------------------
    # 审核阶段：按出厂编号生成/复用复核指派单
    # ------------------------------------------------------------------
    def _escalate_abnormal_event(self, entry: dict[str, Any]) -> None:
        # 补查后再次解析（允许在审核前补填出厂编号）
        equipment, reason = self._resolve_equipment(entry)
        if equipment is None:
            self._mark_pending_lookup(entry, reason)
            entry["safety_escalation"] = "held"
            return

        assignment, created = safety_review.get_or_create_assignment(
            equipment=equipment, source_log=entry,
        )
        entry["复核指派单号"] = assignment["id"]
        entry["复核责任班组"] = assignment.get("责任班组", "")
        entry["identity_pending_lookup"] = False
        entry["safety_escalation"] = "created" if created else "reused"
        entry["复核状态"] = assignment.get("status", "待复核")

    # ------------------------------------------------------------------
    # 补查接口：补填出厂编号后重新解析设备身份
    # ------------------------------------------------------------------
    def supplement_identity(self, entry_id: int, serial: str) -> tuple[dict[str, Any] | None, str]:
        """旧日志缺编号时由调查队员补查出厂编号，重新定位设备。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        serial = str(serial or "").strip()
        if not serial:
            return None, "出厂编号不能为空"
        matches = safety_review.find_equipment_by_serial(serial)
        if not matches:
            return None, f"出厂编号{serial}在设备清单中查无对应设备"
        if len(matches) > 1:
            return None, f"出厂编号{serial}对应多台同名设备，不能用任意一台办复核，请核实唯一编号"

        entry["出厂编号"] = serial
        entry["identity_pending_lookup"] = False
        entry["lookup_reason"] = ""
        equipment = matches[0]
        equipment["log_alert"] = True
        equipment["log_alert_msg"] = f"航次日志{entry.get('id')}补查后记录异常：{entry.get('异常事件', '')}"
        equipment["log_alert_log_id"] = entry.get("id")
        entry["异常设备id"] = equipment.get("id")
        entry["复核状态"] = "待审核"

        # 日志若已审核过（旧回执重送），补查后立即补派复核
        if entry.get("status") == "已审核" and entry.get("has_abnormal"):
            assignment, _ = safety_review.get_or_create_assignment(
                equipment=equipment, source_log=entry,
            )
            entry["复核指派单号"] = assignment["id"]
            entry["复核责任班组"] = assignment.get("责任班组", "")
            entry["复核状态"] = assignment.get("status", "待复核")
        return entry, f"日志{entry_id}已补查出厂编号{serial}并定位到设备{equipment.get('id')}"

    def has_recent_abnormal(self, days: int = 1) -> bool:
        """检查近期是否有异常事件记录。"""
        return any(
            row.get("has_abnormal") and row.get("status") == "已审核"
            for row in store.rows(self.MODULE)
        )
