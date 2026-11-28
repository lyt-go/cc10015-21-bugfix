"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class IdentityPayload(BaseModel):
    """旧日志补查设备身份时提交的出厂编号。"""
    serial: str


class TransferPayload(BaseModel):
    """设备保管交接凭据：新保管班组必填，新保管人与说明可选。"""
    new_team: str
    new_keeper: str | None = ""
    note: str | None = ""


class ReviewSettlePayload(BaseModel):
    """复核指派单核销/拒绝：action 仅支持「核销」「拒绝」。"""
    action: str
    reason: str | None = ""
