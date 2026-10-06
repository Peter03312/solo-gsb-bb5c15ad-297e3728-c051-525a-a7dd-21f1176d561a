"""API 请求/响应模式。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


def _finite(v: float, name: str) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{name} 必须为数值")
    if not math.isfinite(float(v)):
        raise ValueError(f"{name} 必须为有限数值")
    return float(v)


class Point(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _chk(cls, v: float) -> float:
        return _finite(v, "坐标")


class ZoneCreate(BaseModel):
    """登记新禁区。zone_no 省略时自动分配。"""
    zone_no: int | None = Field(default=None, ge=1)
    polygon: list[tuple[float, float]] = Field(min_length=3)
    z_min: float
    z_max: float
    description: str | None = Field(default=None, max_length=500)

    @field_validator("z_min", "z_max")
    @classmethod
    def _z(cls, v: float) -> float:
        return _finite(v, "Z 高度")


class ZoneVersionCreate(BaseModel):
    """登记某禁区的新版本（几何不可变修改 => 新版本）。"""
    polygon: list[tuple[float, float]] = Field(min_length=3)
    z_min: float
    z_max: float
    description: str | None = Field(default=None, max_length=500)

    @field_validator("z_min", "z_max")
    @classmethod
    def _z(cls, v: float) -> float:
        return _finite(v, "Z 高度")


class ZoneVersionOut(BaseModel):
    version_id: int
    version: int
    polygon: list[tuple[float, float]]
    z_min: float
    z_max: float
    created_at: datetime


class ZoneOut(BaseModel):
    zone_id: int
    zone_no: int
    description: str | None
    current_version: int
    created_at: datetime


class ZoneDetailOut(ZoneOut):
    versions: list[ZoneVersionOut]


class ZoneRef(BaseModel):
    """审查引用：禁区编号 + 明确版本号。"""
    zone_no: int = Field(ge=1)
    version: int = Field(ge=1)


class ReviewRequest(BaseModel):
    zones: list[ZoneRef] = Field(min_length=1)
    start: tuple[float, float, float]
    tool_radius: float = Field(ge=0.0)
    gcode: str = Field(min_length=1)
    # 可选：启用后计算全程最小水平净间隙并随报告固化
    include_min_clearance: bool = False
    # 可选：多刀模式。非负整数刀号 -> 非负有限毫米半径。
    # 传入（含空映射）即启用：初始刀位仍用 tool_radius，G-code 中 Tn 切换；
    # 不传时 T 被忽略，保持单刀旧行为。
    tool_radii: dict[int, float] | None = None

    @field_validator("include_min_clearance", mode="before")
    @classmethod
    def _strict_bool(cls, v):
        # 仅接受真正的布尔值；拒绝 "true"/1 等隐式转换，属非法输入（422）
        if not isinstance(v, bool):
            raise ValueError("include_min_clearance 必须为布尔值 true/false")
        return v

    @field_validator("tool_radii", mode="before")
    @classmethod
    def _validate_tool_radii(cls, v):
        # 未传 => 单刀旧模式
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("tool_radii 必须为刀号到半径（mm）的映射对象")
        normalized: dict[int, float] = {}
        for raw_k, raw_val in v.items():
            # JSON 键天然为字符串：接受十进制非负整数写法（如 "0"/"2"）；
            # 拒绝小数/负数/布尔/其他类型
            if isinstance(raw_k, bool):
                raise ValueError("刀号必须为非负整数")
            k: int
            if isinstance(raw_k, int):
                if raw_k < 0:
                    raise ValueError("刀号必须为非负整数")
                k = raw_k
            elif isinstance(raw_k, str) and raw_k.isdigit():
                k = int(raw_k)
            else:
                raise ValueError(f"刀号 {raw_k!r} 非法：必须为非负整数")
            if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
                raise ValueError(f"刀号 {k} 的半径必须为非负有限毫米数值")
            rv = float(raw_val)
            if not math.isfinite(rv) or rv < 0.0:
                raise ValueError(f"刀号 {k} 的半径必须为非负有限毫米数值")
            if k in normalized:
                raise ValueError(f"刀号 {k} 在映射中重复")
            normalized[k] = rv
        return normalized

    @field_validator("tool_radius")
    @classmethod
    def _radius(cls, v: float) -> float:
        return _finite(v, "刀具半径")

    @field_validator("start")
    @classmethod
    def _start(cls, v: tuple[float, float]) -> tuple[float, float, float]:
        if len(v) != 3:
            raise ValueError("初始刀尖坐标必须为 [x, y, z]")
        for c in v:
            _finite(c, "初始刀尖坐标")
        return tuple(float(c) for c in v)  # type: ignore[return-value]


class MinClearanceOut(BaseModel):
    """全程最小水平净间隙结果（毫米；净间隙可为负）。"""
    min_clearance_mm: float
    line_no: int | None
    point_mm: tuple[float, float, float]
    zone_no: int
    progress: float = Field(description="取得最小值处的段内进度（直线 t / 圆弧 s）")
    # 多刀模式固化：取得最小值时生效刀号（初始刀位为 null）与半径（mm）。
    # 两者均为 null 表示单刀模式（旧请求），序列化时省略，保持旧报告/旧客户端兼容。
    tool_no: int | None = None
    tool_radius_mm: float | None = None

    @model_serializer(mode="plain")
    def _serialize(self):
        data = {
            "min_clearance_mm": self.min_clearance_mm,
            "line_no": self.line_no,
            "point_mm": list(self.point_mm),
            "zone_no": self.zone_no,
            "progress": self.progress,
        }
        if self.tool_no is not None or self.tool_radius_mm is not None:
            data["tool_no"] = self.tool_no
            data["tool_radius_mm"] = self.tool_radius_mm
        return data


class TraceEventOut(BaseModel):
    """执行事件轨迹中的一项（多刀模式，按执行顺序）。

    kind：initial（初始刀位）/ tool_change（T 切换时的静止刀位，
    含与运动同行、先于本行运动）/ motion（运动段）。
    """
    seq: int = Field(description="执行顺序（初始刀位为 0，从 1 起递增）")
    kind: Literal["initial", "tool_change", "motion"]
    line_no: int | None = Field(description="G-code 原行号；初始刀位为 null")
    tool_no: int | None = Field(description="生效刀号；初始刀位为 null")
    tool_radius_mm: float = Field(description="生效刀具半径（mm）")
    start_mm: tuple[float, float, float] = Field(
        description="事件起点刀尖坐标 [x, y, z]（mm）")
    end_mm: tuple[float, float, float] = Field(
        description="事件终点刀尖坐标 [x, y, z]（mm）；静止事件与起点相同")
    hit_zone_no: int | None = Field(
        description="该事件命中的禁区编号（段内取最早命中），未命中为 null")


class ReviewResponse(BaseModel):
    report_id: int
    used_zone_versions: dict[str, int]
    input_summary: dict[str, Any]
    has_collision: bool
    collision_line_no: int | None
    collision_zone_no: int | None
    conclusion: Literal["collision", "no_collision"]
    message: str
    # 仅当请求 include_min_clearance=true 时非空；全程不在任何禁区高度内时为 null
    min_clearance: MinClearanceOut | None = None
    min_clearance_enabled: bool = False
    # 多刀模式固化：命中时生效刀号（初始刀位为 null）与半径（mm）；
    # 旧请求/旧报告这两个字段为 null
    collision_tool_no: int | None = None
    collision_tool_radius_mm: float | None = None
    # 多刀模式固化的执行事件轨迹（初始刀位 -> 各 T 切换静止刀位 -> 各段运动）；
    # 单刀请求与旧报告为 null
    execution_trace: list[TraceEventOut] | None = None


# ---------- 批量审查 ----------

# 批次/任务标识：字母数字开头，可含 _ . : -，最长 64
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"


class BatchReviewItem(ReviewRequest):
    """批量审查中的单项任务：完整单项审查输入 + 可选批内唯一任务标识。"""
    task_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=_ID_PATTERN,
        description="批内唯一任务标识；省略时按输入顺序号标识")


class BatchReviewRequest(BaseModel):
    """批量审查请求：1～50 项有序任务，同一事务校验并生成报告。

    batch_id 为幂等键：相同 batch_id + 相同内容重放返回首次逐项结果；
    相同 batch_id + 不同内容返回 409 冲突且不生成新报告。
    """
    batch_id: str = Field(min_length=1, max_length=64, pattern=_ID_PATTERN)
    items: list[BatchReviewItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_task_ids(self) -> "BatchReviewRequest":
        seen: set[str] = set()
        for it in self.items:
            if it.task_id is None:
                continue
            if it.task_id in seen:
                raise ValueError(f"任务标识 {it.task_id!r} 在批内重复")
            seen.add(it.task_id)
        return self


class BatchItemResult(BaseModel):
    """批量审查逐项结果：输入顺序号、任务标识与该项完整审查结果。"""
    index: int = Field(description="任务在请求中的输入顺序号（从 0 起）")
    task_id: str | None
    result: ReviewResponse


class BatchReviewResponse(BaseModel):
    batch_id: str
    item_count: int
    results: list[BatchItemResult]
