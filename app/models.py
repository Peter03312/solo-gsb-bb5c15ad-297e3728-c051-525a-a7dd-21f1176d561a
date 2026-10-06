"""ORM 模型。

- zones：禁区逻辑实体（编号唯一）；
- zone_versions：禁区的不可变版本，每次修改产生新版本，旧版本保留；
- reports：审查报告，创建后不可变，不随后续禁区版本变化；
- batches：批量审查批次（batch_id 幂等键 + 内容哈希 + 逐项结果快照）；
- upload_sessions：长 G-code 可恢复上传会话（固定审查参数、完成状态、
  完成声明与审查结果快照），upload_chunks：从 0 起序号的 UTF-8 文本块。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, CheckConstraint, Column, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text, Boolean)
from sqlalchemy.orm import relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Zone(Base):
    __tablename__ = "zones"

    id = Column(Integer, primary_key=True)
    zone_no = Column(Integer, nullable=False, unique=True)
    description = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    versions = relationship("ZoneVersion", back_populates="zone",
                            order_by="ZoneVersion.version")


class ZoneVersion(Base):
    __tablename__ = "zone_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_version_positive"),
        Index("ix_zone_version", "zone_id", "version", unique=True),
    )

    id = Column(Integer, primary_key=True)
    zone_id = Column(Integer, ForeignKey("zones.id"), nullable=False)
    version = Column(Integer, nullable=False)
    # 毫米制二维多边形顶点 [[x, y], ...]
    polygon = Column(JSON, nullable=False)
    z_min = Column(Float, nullable=False)
    z_max = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    zone = relationship("Zone", back_populates="versions")


class Report(Base):
    """审查报告：保存即不可变。"""

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # 审查所用禁区版本快照（zone_no -> version_id/version）
    zone_versions = Column(JSON, nullable=False)
    # 输入摘要
    input_summary = Column(JSON, nullable=False)

    has_collision = Column(Boolean, nullable=False)
    collision_line_no = Column(Integer, nullable=True)
    collision_zone_no = Column(Integer, nullable=True)
    conclusion = Column(Text, nullable=False)

    # 多刀模式/命中固化：首碰撞发生时生效刀号（初始刀位为空）与半径（mm）。
    # 旧请求/旧报告这两列为 NULL，读取保持兼容。
    collision_tool_no = Column(Integer, nullable=True)
    collision_tool_radius = Column(Float, nullable=True)

    # 可选的全程最小水平净间隙（仅 include_min_clearance=true 的报告固化）
    min_clearance_enabled = Column(Boolean, nullable=False, default=False,
                                   server_default="0")
    # {min_clearance_mm, line_no, point_mm, zone_no, progress,
    #  tool_no, tool_radius_mm} 或 None
    min_clearance = Column(JSON, nullable=True)

    # 多刀模式固化的执行事件轨迹（仅 tool_radii 启用的报告）：
    # [{seq, kind, line_no, tool_no, tool_radius_mm, start_mm, end_mm,
    #   hit_zone_no}, ...]；单刀请求与旧报告为 NULL
    execution_trace = Column(JSON, nullable=True)


class Batch(Base):
    """批量审查批次：与整批报告在同一事务提交。

    batch_id 为幂等键（唯一约束兜底并发）；content_hash 为批内容
    （逐项规范化请求）的 SHA-256，用于区分重放与冲突：
    相同 batch_id + 相同内容 => 重放首次逐项结果（results 快照原样返回）；
    相同 batch_id + 不同内容 => 由 API 层返回 409，不新增行、不生成报告。
    """

    __tablename__ = "batches"

    id = Column(Integer, primary_key=True)
    batch_id = Column(String(64), nullable=False, unique=True)
    # 批内容（逐项规范化请求）的 SHA-256
    content_hash = Column(String(64), nullable=False)
    item_count = Column(Integer, nullable=False)
    # 逐项结果快照 [{index, task_id, result: {report_id, ...}}]，按输入顺序
    results = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class UploadSession(Base):
    """长 G-code 可恢复上传会话。

    创建时即固定审查参数（禁区编号+明确版本、初始刀尖坐标、初始刀半径、
    可选多刀映射与最小净间隙开关），存为规范化 JSON。块乱序提交到
    upload_chunks；完成时校验块齐全、总量、拼接 SHA-256 与既有审查口径，
    通过则在同一事务落报告并把会话置为 completed，固化完成声明与审查结果
    快照（completed_result），完成重放原样返回；缺块/超限/摘要不符/解析失败
    均不置完成、不留报告。完成后拒绝新块与不同声明。
    """

    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'completed')",
                        name="ck_upload_session_status"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(String(36), nullable=False, unique=True)
    # open：可继续提交块；completed：已生成不可变报告，拒绝写入
    status = Column(String(16), nullable=False, default="open",
                    server_default="open")
    # 创建时固定的审查参数（规范化 JSON）：
    # {zones, start, tool_radius, include_min_clearance, tool_radii}
    params = Column(JSON, nullable=False)
    # 完成时声明的块数、拼接文本 SHA-256（open 时为 NULL）
    block_count = Column(Integer, nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    # 已收块总字节数（UTF-8）
    total_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    # 完成时的审查结果快照（与 POST /reviews 同构），重放原样返回
    completed_result = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class UploadChunk(Base):
    """上传会话中的一个 UTF-8 文本块（序号从 0 起，同会话内唯一）。

    content 存 UTF-8 解码后的文本；size_bytes 为该块 UTF-8 编码字节数
    （单块 ≤ 64 KiB，会话总量 ≤ 8 MiB，均在写入路径强制）；
    content_sha256 为该块 UTF-8 字节 SHA-256，用于同号重放返回原回执。
    """

    __tablename__ = "upload_chunks"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="ck_upload_chunk_seq_nonneg"),
        Index("ix_upload_chunk_session_seq", "session_id", "seq", unique=True),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("upload_sessions.id"),
                        nullable=False)
    seq = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
