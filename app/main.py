"""FastAPI 应用：数控刀路禁入区审查服务。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .collision import (Zone, build_execution_trace, check_path,
                        initial_collision, minimum_clearance,
                        minimum_clearance_program)
from .database import Base, engine, get_db
from .gcode import GCodeError, parse_gcode
from .geometry import GeometryError, normalize_polygon, validate_z_range
from . import models
from .models import Batch, Report, UploadChunk, UploadSession, ZoneVersion
from .schemas import (MAX_CHUNK_BYTES, MAX_CHUNK_COUNT, MAX_TOTAL_BYTES,
                      BatchItemResult, BatchReviewRequest, BatchReviewResponse,
                      ChunkReceipt, MinClearanceOut, ReviewRequest,
                      ReviewResponse, TraceEventOut, UploadCompleteRequest,
                      UploadSessionCreate, UploadSessionOut, ZoneCreate,
                      ZoneDetailOut, ZoneOut, ZoneVersionCreate, ZoneVersionOut)

app = FastAPI(
    title="数控刀路禁入区审查服务",
    version="1.5.0",
    description="登记毫米制二维多边形禁区（含闭合 Z 高度范围、不可变版本），"
                "对 G-code 刀路做连续碰撞审查并出具不可变报告；"
                "可选计算全程最小水平净间隙（include_min_clearance），"
                "可选多刀半径映射（tool_radii：刀号到毫米半径），"
                "多刀模式随报告固化可回看的执行事件轨迹（execution_trace）；"
                "支持一次提交 1～50 项有序任务的批量审查（/reviews/batch，"
                "batch_id 幂等，整批同一事务）；"
                "支持长 G-code 可恢复分块上传（/uploads：冻结审查参数建会话，"
                "0 起序号乱序提交 UTF-8 文本块，声明块数与整文 SHA-256 完成，"
                "与单项审查同口径生成不可变报告，完成声明幂等）。",
)


@app.on_event("startup")
def on_startup() -> None:
    # 容器内由应用直接完成数据库初始化（表结构创建；v1.4 新增 batches 表、
    # v1.5 新增 upload_sessions / upload_chunks 表，对旧库同样由
    # create_all 幂等建出）
    Base.metadata.create_all(bind=engine)
    # 旧库 reports 表缺少后续版本新增列：幂等补齐，保证旧请求/旧报告仍可读写
    _ensure_report_columns(engine)


def _ensure_report_columns(db_engine) -> None:
    """对旧数据库幂等添加 reports 表后续版本新增列。

    PostgreSQL 支持 BOOLEAN/JSON 原生类型；SQLite 对列类型仅按名称亲和处理，
    同名 DDL 同样可用。
    """
    inspector = inspect(db_engine)
    if "reports" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("reports")}
    is_pg = db_engine.dialect.name == "postgresql"
    with db_engine.begin() as conn:
        if "min_clearance_enabled" not in existing:
            default = "FALSE" if is_pg else "0"
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN "
                f"min_clearance_enabled BOOLEAN NOT NULL DEFAULT {default}"))
        if "min_clearance" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN min_clearance JSON"))
        # v1.2：多刀模式首碰撞命中刀号/半径固化列（旧报告为 NULL）
        if "collision_tool_no" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN collision_tool_no INTEGER"))
        if "collision_tool_radius" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN collision_tool_radius FLOAT"))
        # v1.3：多刀模式执行事件轨迹固化列（单刀请求与旧报告为 NULL）
        if "execution_trace" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN execution_trace JSON"))


@app.exception_handler(GeometryError)
async def geometry_error_handler(request: Request, exc: GeometryError):  # noqa: ARG001
    return JSONResponse(status_code=422,
                        content={"error": "invalid_geometry", "detail": str(exc)})


# ---------- 禁区登记 ----------

@app.post("/zones", response_model=ZoneOut, status_code=201,
          summary="登记新禁区（生成版本 1）")
def create_zone(payload: ZoneCreate, db: Session = Depends(get_db)) -> ZoneOut:
    polygon = normalize_polygon(payload.polygon)
    validate_z_range(payload.z_min, payload.z_max)

    if payload.zone_no is not None:
        exists = db.scalar(select(models.Zone).where(
            models.Zone.zone_no == payload.zone_no))
        if exists:
            raise HTTPException(status_code=409,
                                detail=f"禁区编号 {payload.zone_no} 已存在")
        zone_no = payload.zone_no
    else:
        max_no = db.scalar(select(func.max(models.Zone.zone_no))) or 0
        zone_no = max_no + 1

    zone = models.Zone(zone_no=zone_no, description=payload.description)
    db.add(zone)
    db.flush()
    db.add(ZoneVersion(zone_id=zone.id, version=1, polygon=[list(p) for p in polygon],
                       z_min=payload.z_min, z_max=payload.z_max))
    db.commit()
    db.refresh(zone)
    return ZoneOut(zone_id=zone.id, zone_no=zone.zone_no,
                   description=zone.description, current_version=1,
                   created_at=zone.created_at)


@app.get("/zones", response_model=list[ZoneOut], summary="列出全部禁区")
def list_zones(db: Session = Depends(get_db)) -> list[ZoneOut]:
    zones = db.scalars(select(models.Zone).order_by(models.Zone.zone_no)).all()
    out: list[ZoneOut] = []
    for z in zones:
        cur = db.scalar(select(func.max(ZoneVersion.version)).where(
            ZoneVersion.zone_id == z.id))
        out.append(ZoneOut(zone_id=z.id, zone_no=z.zone_no,
                           description=z.description, current_version=cur or 0,
                           created_at=z.created_at))
    return out


@app.get("/zones/{zone_no}", response_model=ZoneDetailOut,
         summary="查询禁区及其全部不可变版本")
def get_zone(zone_no: int, db: Session = Depends(get_db)) -> ZoneDetailOut:
    zone = db.scalar(select(models.Zone).where(models.Zone.zone_no == zone_no))
    if zone is None:
        raise HTTPException(status_code=404, detail=f"禁区 {zone_no} 不存在")
    versions = db.scalars(
        select(ZoneVersion).where(ZoneVersion.zone_id == zone.id)
        .order_by(ZoneVersion.version)).all()
    return ZoneDetailOut(
        zone_id=zone.id, zone_no=zone.zone_no, description=zone.description,
        current_version=versions[-1].version if versions else 0,
        created_at=zone.created_at,
        versions=[ZoneVersionOut(
            version_id=v.id, version=v.version,
            polygon=[tuple(p) for p in v.polygon],
            z_min=v.z_min, z_max=v.z_max, created_at=v.created_at)
            for v in versions])


@app.post("/zones/{zone_no}/versions", response_model=ZoneVersionOut,
          status_code=201, summary="为禁区登记不可变新版本（修改不改旧版本）")
def add_zone_version(zone_no: int, payload: ZoneVersionCreate,
                     db: Session = Depends(get_db)) -> ZoneVersionOut:
    zone = db.scalar(select(models.Zone).where(models.Zone.zone_no == zone_no))
    if zone is None:
        raise HTTPException(status_code=404, detail=f"禁区 {zone_no} 不存在")

    polygon = normalize_polygon(payload.polygon)
    validate_z_range(payload.z_min, payload.z_max)

    # 行锁保证并发下版本号单调
    db.scalar(
        select(models.Zone).where(models.Zone.id == zone.id).with_for_update())
    latest = db.scalar(select(func.max(ZoneVersion.version)).where(
        ZoneVersion.zone_id == zone.id)) or 0
    ver = ZoneVersion(zone_id=zone.id, version=latest + 1,
                      polygon=[list(p) for p in polygon],
                      z_min=payload.z_min, z_max=payload.z_max)
    db.add(ver)
    db.commit()
    db.refresh(ver)
    return ZoneVersionOut(version_id=ver.id, version=ver.version,
                          polygon=[tuple(p) for p in ver.polygon],
                          z_min=ver.z_min, z_max=ver.z_max,
                          created_at=ver.created_at)


# ---------- 审查 ----------

def _load_zones(db: Session, refs) -> tuple[list[Zone], dict[str, int], dict[int, int]]:
    zones: list[Zone] = []
    used: dict[str, int] = {}
    version_ids: dict[int, int] = {}
    seen: set[int] = set()
    for ref in refs:
        if ref.zone_no in seen:
            raise HTTPException(status_code=422,
                                detail=f"禁区 {ref.zone_no} 在请求中重复")
        seen.add(ref.zone_no)
        zone = db.scalar(select(models.Zone).where(
            models.Zone.zone_no == ref.zone_no))
        if zone is None:
            raise HTTPException(status_code=404,
                                detail=f"禁区 {ref.zone_no} 不存在")
        zv = db.scalar(select(ZoneVersion).where(
            ZoneVersion.zone_id == zone.id,
            ZoneVersion.version == ref.version))
        if zv is None:
            raise HTTPException(
                status_code=404,
                detail=f"禁区 {ref.zone_no} 不存在版本 {ref.version}")
        polygon = normalize_polygon(zv.polygon)  # 防御性再校验
        zones.append(Zone(ref.zone_no, polygon, zv.z_min, zv.z_max))
        used[str(ref.zone_no)] = ref.version
        version_ids[ref.zone_no] = zv.id
    zones.sort(key=lambda z: z.zone_no)
    return zones, used, version_ids


def _build_review(db: Session, payload: ReviewRequest) -> ReviewResponse:
    """校验并计算单项审查，构造 Report（加入会话并 flush，不 commit）。

    调用方决定提交时机：单项端点立即提交；批量端点在同一事务中
    先跑完整批再统一提交，任一项抛错即整体回滚、不留任何报告。
    非法输入抛 HTTPException / GeometryError。
    """
    if payload.tool_radius < 0:
        raise HTTPException(status_code=422, detail="刀具半径不得为负")

    # 先加载并固化所用禁区版本；任何非法输入都在写报告之前拒绝
    zones, used_versions, _ = _load_zones(db, payload.zones)

    tool_map = payload.tool_radii
    multi_tool = tool_map is not None

    # 解析 G-code；语法/语义错误（含多刀模式下非法/未映射 T）明确拒绝，不留报告
    try:
        if multi_tool:
            parsed = parse_gcode(
                payload.gcode, tuple(payload.start),  # type: ignore[arg-type]
                tool_radius_map=tool_map,
                initial_tool_radius=float(payload.tool_radius))
        else:
            parsed = parse_gcode(
                payload.gcode, tuple(payload.start))  # type: ignore[arg-type]
    except GCodeError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_gcode",
            "line_no": exc.line_no,
            "message": str(exc),
        })

    summary = {
        "gcode_lines": parsed.line_count,
        "nonblank_lines": parsed.nonblank_count,
        "motion_count": len(parsed.motions),
        "start_position_mm": list(payload.start),
        "tool_radius_mm": payload.tool_radius,
        "final_position_mm": list(parsed.final_position),
        "gcode_sha256": _sha(payload.gcode),
        "include_min_clearance": bool(payload.include_min_clearance),
    }
    if multi_tool:
        # 固化本次多刀配置：刀号（字符串键，JSON 友好）-> 毫米半径
        summary["tool_map_enabled"] = True
        summary["tool_radii_mm"] = {str(k): v for k, v in tool_map.items()}

    trace_payload: list[dict] | None = None
    if multi_tool:
        # 多刀模式：构建执行事件轨迹（初始刀位 -> 各 T 切换静止刀位 ->
        # 各段运动，同行 T 先于本行运动），首碰撞即轨迹中首个命中事件；
        # 轨迹随报告固化，不因禁区新版本改变
        trace = build_execution_trace(
            parsed.motions, parsed.tool_events, zones,
            tuple(payload.start),  # type: ignore[arg-type]
            float(payload.tool_radius))
        collision = next((ev.hit for ev in trace if ev.hit is not None), None)
        min_clr = minimum_clearance_program(
            parsed.motions, parsed.tool_events, zones,
            tuple(payload.start),  # type: ignore[arg-type]
            float(payload.tool_radius)) \
            if payload.include_min_clearance else None
        trace_payload = [{
            "seq": ev.seq,
            "kind": ev.kind,
            "line_no": ev.line_no,
            "tool_no": ev.tool_no,
            "tool_radius_mm": ev.tool_radius,
            "start_mm": [ev.start[0], ev.start[1], ev.start[2]],
            "end_mm": [ev.end[0], ev.end[1], ev.end[2]],
            "hit_zone_no": ev.hit.zone_no if ev.hit is not None else None,
        } for ev in trace]
    else:
        # 单刀模式：旧口径（初始刀位仅在无运动碰撞时补判）保持兼容
        collision = check_path(parsed.motions, zones, payload.tool_radius)
        min_clr = minimum_clearance(parsed.motions, zones, payload.tool_radius,
                                    tuple(payload.start)) \
            if payload.include_min_clearance else None

    line_no: int | None = None
    zone_no: int | None = None
    collision_tool_no: int | None = None
    collision_tool_radius: float | None = None

    if collision is not None:
        line_no = collision.line_no
        zone_no = collision.zone_no
        if multi_tool:
            collision_tool_no = collision.tool_no
            collision_tool_radius = collision.tool_radius
    elif not multi_tool:
        # 无运动段碰撞时检查初始刀位点；有运动段时归属首个运动行号
        static_zone = initial_collision(tuple(payload.start),  # type: ignore[arg-type]
                                        zones, payload.tool_radius)
        if static_zone is not None:
            zone_no = static_zone
            line_no = parsed.motions[0].line_no if parsed.motions else None

    has_coll = zone_no is not None

    if has_coll:
        conclusion = "collision"
        message = (f"发现碰撞：G-code 第 {line_no} 行与禁区编号 {zone_no} 冲突") \
            if line_no is not None else \
            f"初始刀位点位于禁区编号 {zone_no} 范围内"
    else:
        conclusion = "no_collision"
        message = "刀路与全部指定禁区版本无碰撞"

    clearance_payload: dict | None = None
    if payload.include_min_clearance and min_clr is not None:
        clearance_payload = {
            "min_clearance_mm": min_clr.net,
            "line_no": min_clr.line_no,
            "point_mm": list(min_clr.point),
            "zone_no": min_clr.zone_no,
            "progress": min_clr.progress,
        }
        if multi_tool:
            clearance_payload["tool_no"] = min_clr.tool_no
            clearance_payload["tool_radius_mm"] = min_clr.tool_radius

    report = Report(
        zone_versions=used_versions,
        input_summary=summary,
        has_collision=has_coll,
        collision_line_no=line_no,
        collision_zone_no=zone_no,
        conclusion=conclusion,
        collision_tool_no=collision_tool_no,
        collision_tool_radius=collision_tool_radius,
        min_clearance_enabled=bool(payload.include_min_clearance),
        min_clearance=clearance_payload,
        execution_trace=trace_payload,
    )
    db.add(report)
    db.flush()  # 取得 report.id；提交与否由调用方决定

    return ReviewResponse(
        report_id=report.id, used_zone_versions=used_versions,
        input_summary=summary, has_collision=has_coll,
        collision_line_no=line_no, collision_zone_no=zone_no,
        conclusion=conclusion, message=message,
        min_clearance=MinClearanceOut(**clearance_payload)
        if clearance_payload is not None else None,
        min_clearance_enabled=bool(payload.include_min_clearance),
        collision_tool_no=collision_tool_no,
        collision_tool_radius_mm=collision_tool_radius,
        execution_trace=[TraceEventOut(**e) for e in trace_payload]
        if trace_payload is not None else None)


@app.post("/reviews", response_model=ReviewResponse,
          summary="提交刀路审查并生成不可变报告")
def review(payload: ReviewRequest,
           db: Session = Depends(get_db)) -> ReviewResponse:
    resp = _build_review(db, payload)
    db.commit()
    return resp


# ---------- 批量审查 ----------

def _batch_content_hash(payload: BatchReviewRequest) -> str:
    """批内容（逐项规范化请求）的 SHA-256，用于区分重放与冲突。"""
    canonical = json.dumps(payload.model_dump(mode="json")["items"],
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_response_body(batch_id: str, results: list[dict]) -> dict:
    return {"batch_id": batch_id, "item_count": len(results),
            "results": results}


@app.post("/reviews/batch", response_model=BatchReviewResponse, status_code=201,
          summary="批量提交有序刀路审查（1～50 项，同一事务，batch_id 幂等）")
def review_batch(payload: BatchReviewRequest,
                 db: Session = Depends(get_db)):
    content_hash = _batch_content_hash(payload)

    # 幂等：相同 batch_id 已入库 => 内容一致重放首次逐项结果，不一致 409
    existing = db.scalar(select(Batch).where(Batch.batch_id == payload.batch_id))
    if existing is not None:
        if existing.content_hash != content_hash:
            raise HTTPException(status_code=409, detail={
                "error": "batch_conflict",
                "batch_id": payload.batch_id,
                "message": f"批次 {payload.batch_id!r} 已存在且内容不同，"
                           "未生成新报告",
            })
        return JSONResponse(status_code=200, content=_batch_response_body(
            existing.batch_id, existing.results))

    # 同一事务中逐项校验并计算：任一项非法即记录逐项错误，整批回滚
    results: list[BatchItemResult] = []
    failed: list[dict] = []
    for idx, item in enumerate(payload.items):
        try:
            res = _build_review(db, item)
        except HTTPException as exc:
            failed.append({"index": idx, "task_id": item.task_id,
                           "error": exc.detail})
        except GeometryError as exc:
            failed.append({"index": idx, "task_id": item.task_id,
                           "error": {"error": "invalid_geometry",
                                     "detail": str(exc)}})
        else:
            results.append(BatchItemResult(index=idx, task_id=item.task_id,
                                           result=res))

    if failed:
        # 整批不生成报告：回滚已 flush 的本批报告
        db.rollback()
        return JSONResponse(status_code=422, content={
            "error": "invalid_batch",
            "batch_id": payload.batch_id,
            "message": f"批量审查未通过：{len(failed)} 项非法，"
                       "整批未生成任何报告",
            "failed_items": failed,
        })

    # 全部通过：按输入顺序固化逐项结果快照，与整批报告同一事务提交
    stored = [{"index": r.index, "task_id": r.task_id,
               "result": r.result.model_dump(mode="json")} for r in results]
    db.add(Batch(batch_id=payload.batch_id, content_hash=content_hash,
                 item_count=len(results), results=stored))
    try:
        db.commit()
    except IntegrityError:
        # 并发同 batch_id：唯一约束兜底，重放已入库的首次结果
        db.rollback()
        winner = db.scalar(select(Batch).where(
            Batch.batch_id == payload.batch_id))
        if winner is not None and winner.content_hash == content_hash:
            return JSONResponse(status_code=200, content=_batch_response_body(
                winner.batch_id, winner.results))
        raise HTTPException(status_code=409, detail={
            "error": "batch_conflict",
            "batch_id": payload.batch_id,
            "message": f"批次 {payload.batch_id!r} 已存在且内容不同，"
                       "未生成新报告",
        })

    return BatchReviewResponse(batch_id=payload.batch_id,
                               item_count=len(results), results=results)


# ---------- 长 G-code 可恢复分块上传 ----------
#
# 车间网络不稳时的可恢复提交：
# 1) POST /uploads 创建会话：冻结禁区编号 + 明确版本、初始刀尖坐标、刀半径
#    与可选多刀/最小净间隙设置，返回会话标识 session_id；
# 2) PUT /uploads/{session_id}/chunks/{seq} 以 0 起序号提交 UTF-8 文本块
#    （可乱序；单块 <= 64 KiB，会话总量 <= 8 MiB；同号同内容重传返回原回执，
#    同号异内容 409 冲突且不覆盖）；GET /uploads/{session_id} 查询已收序号；
# 3) POST /uploads/{session_id}/complete 声明正整数块数与拼接文本 SHA-256：
#    0..块数-1 齐全、无越界块、摘要一致且整份 G-code 通过既有审查校验时，
#    按与 POST /reviews 完全相同的口径生成一份不可变报告（行号按拼接原文），
#    否则不留报告；相同完成声明并发/重放返回同一报告，成功完成后不同声明
#    冲突且拒绝新块；全部状态落库，跨服务重启保留。

def _get_upload_session(db: Session, session_id: str) -> UploadSession:
    s = db.scalar(select(UploadSession).where(
        UploadSession.session_id == session_id))
    if s is None:
        raise HTTPException(status_code=404,
                            detail=f"上传会话 {session_id!r} 不存在")
    return s


def _upload_session_out(s: UploadSession,
                        received_seqs: list[int]) -> UploadSessionOut:
    return UploadSessionOut(
        session_id=s.session_id, status=s.status, params=s.params,
        received_seqs=received_seqs, received_count=s.received_count,
        total_bytes=s.total_bytes,
        max_chunk_bytes=MAX_CHUNK_BYTES, max_total_bytes=MAX_TOTAL_BYTES,
        declared_chunk_count=s.declared_chunk_count,
        declared_sha256=s.declared_sha256, report_id=s.report_id,
        created_at=s.created_at, completed_at=s.completed_at)


def _chunk_receipt(session_id: str, seq: int, byte_len: int,
                   sha256: str) -> dict:
    return ChunkReceipt(session_id=session_id, seq=seq, byte_len=byte_len,
                        sha256=sha256).model_dump(mode="json")


@app.post("/uploads", response_model=UploadSessionOut, status_code=201,
          summary="创建分块上传会话（冻结审查参数，返回会话标识）")
def create_upload_session(payload: UploadSessionCreate,
                          db: Session = Depends(get_db)) -> UploadSessionOut:
    # 创建时即校验禁区编号与明确版本存在（404/422 口径同 POST /reviews）；
    # 版本几何不可变，完成时按同一编号+版本重载，结果一致
    _load_zones(db, payload.zones)
    session = UploadSession(session_id=uuid4().hex, status="open",
                            params=payload.model_dump(mode="json"))
    db.add(session)
    db.commit()
    db.refresh(session)
    return _upload_session_out(session, [])


@app.get("/uploads/{session_id}", response_model=UploadSessionOut,
         summary="查询上传会话状态与已收块序号")
def get_upload_session(session_id: str,
                       db: Session = Depends(get_db)) -> UploadSessionOut:
    s = _get_upload_session(db, session_id)
    seqs = db.scalars(select(UploadChunk.seq)
                      .where(UploadChunk.upload_id == s.id)
                      .order_by(UploadChunk.seq)).all()
    return _upload_session_out(s, list(seqs))


@app.put("/uploads/{session_id}/chunks/{seq}", response_model=ChunkReceipt,
         status_code=201,
         summary="提交一个 UTF-8 文本块（0 起序号，可乱序，同号重传幂等）",
         openapi_extra={"requestBody": {
             "required": True,
             "content": {
                 "text/plain": {"schema": {"type": "string",
                                           "example": "G1 X15 Y5 Z0\n"}},
                 "application/octet-stream": {
                     "schema": {"type": "string", "format": "binary"}},
             },
             "description": "原始 UTF-8 文本块（1 字节～64 KiB；"
                            "Content-Type 不限，按原始字节读取）",
         }})
async def put_upload_chunk(request: Request, session_id: str,
                           seq: int = Path(ge=0, le=MAX_CHUNK_COUNT - 1),
                           db: Session = Depends(get_db)):
    # 单块上限 64 KiB：先按 Content-Length 快速拒绝，再按实际长度校验
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_len = int(content_length)
        except ValueError:
            declared_len = -1
        if declared_len > MAX_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail={
                "error": "chunk_too_large", "max_chunk_bytes": MAX_CHUNK_BYTES,
                "message": "单块大小超过 64 KiB 上限"})
    raw = await request.body()
    if len(raw) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "chunk_too_large", "max_chunk_bytes": MAX_CHUNK_BYTES,
            "message": "单块大小超过 64 KiB 上限"})
    if not raw:
        raise HTTPException(status_code=422, detail={
            "error": "empty_chunk", "message": "块内容不能为空"})
    try:
        chunk_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_encoding",
            "message": "块内容必须为合法 UTF-8 文本"})
    digest = hashlib.sha256(raw).hexdigest()

    # 写优先：条件 UPDATE 原子完成「会话存在且仍 open 且总量不超限」的
    # 计数推进（PostgreSQL 行锁 / SQLite 库锁下均与完成请求、其他块上传
    # 互斥，且避免先读后写造成的锁升级等待）；重复/冲突判定随后在同一
    # 事务内回滚计数，不落任何块。
    res = db.execute(
        update(UploadSession)
        .where(UploadSession.session_id == session_id,
               UploadSession.status == "open",
               UploadSession.total_bytes + len(raw) <= MAX_TOTAL_BYTES)
        .values(received_count=UploadSession.received_count + 1,
                total_bytes=UploadSession.total_bytes + len(raw))
        .execution_options(synchronize_session="fetch"))
    if res.rowcount != 1:
        # 未推进：只读判定 404 / 同号幂等回执 / 同号冲突 / 已完成 / 超限
        db.rollback()
        s = db.scalar(select(UploadSession).where(
            UploadSession.session_id == session_id))
        if s is None:
            raise HTTPException(status_code=404,
                                detail=f"上传会话 {session_id!r} 不存在")
        existing = db.scalar(select(UploadChunk).where(
            UploadChunk.upload_id == s.id, UploadChunk.seq == seq))
        if existing is not None:
            if existing.sha256 == digest and existing.data == chunk_text:
                return JSONResponse(status_code=200, content=_chunk_receipt(
                    session_id, seq, existing.byte_len, existing.sha256))
            raise HTTPException(status_code=409, detail={
                "error": "chunk_conflict", "session_id": session_id,
                "seq": seq,
                "message": f"序号 {seq} 已存在不同内容的块，未覆盖"})
        if s.status != "open":
            raise HTTPException(status_code=409, detail={
                "error": "session_completed", "session_id": session_id,
                "message": "会话已成功完成，拒绝新块"})
        raise HTTPException(status_code=413, detail={
            "error": "total_size_exceeded", "max_total_bytes": MAX_TOTAL_BYTES,
            "message": "会话总量超过 8 MiB 上限"})

    s = db.scalar(select(UploadSession).where(
        UploadSession.session_id == session_id))
    # 同号块已存在：同内容重传返回原回执（即使会话已完成），异内容冲突
    # 不覆盖；撤销刚才的计数推进，不落任何块
    existing = db.scalar(select(UploadChunk).where(
        UploadChunk.upload_id == s.id, UploadChunk.seq == seq))
    if existing is not None:
        same = existing.sha256 == digest and existing.data == chunk_text
        db.rollback()
        if same:
            return JSONResponse(status_code=200, content=_chunk_receipt(
                session_id, seq, existing.byte_len, existing.sha256))
        raise HTTPException(status_code=409, detail={
            "error": "chunk_conflict", "session_id": session_id, "seq": seq,
            "message": f"序号 {seq} 已存在不同内容的块，未覆盖"})

    db.add(UploadChunk(upload_id=s.id, seq=seq, byte_len=len(raw),
                       sha256=digest, data=chunk_text))
    try:
        db.commit()
    except IntegrityError:
        # 并发同号块：唯一约束兜底，重读判定幂等/冲突
        db.rollback()
        existing = db.scalar(select(UploadChunk).where(
            UploadChunk.upload_id == s.id, UploadChunk.seq == seq))
        if existing is not None and existing.sha256 == digest \
                and existing.data == chunk_text:
            return JSONResponse(status_code=200, content=_chunk_receipt(
                session_id, seq, existing.byte_len, existing.sha256))
        raise HTTPException(status_code=409, detail={
            "error": "chunk_conflict", "session_id": session_id, "seq": seq,
            "message": f"序号 {seq} 已存在不同内容的块，未覆盖"})
    return _chunk_receipt(session_id, seq, len(raw), digest)


def _completion_replay_or_conflict(session: UploadSession, chunk_count: int,
                                   digest: str) -> JSONResponse:
    """已完成会话：相同声明重放首次结果（同一报告），不同声明 409。"""
    if session.declared_chunk_count == chunk_count \
            and session.declared_sha256 == digest:
        return JSONResponse(status_code=200, content=session.result)
    raise HTTPException(status_code=409, detail={
        "error": "completion_conflict", "session_id": session.session_id,
        "report_id": session.report_id,
        "message": "会话已成功完成且完成声明不同，未生成新报告"})


@app.post("/uploads/{session_id}/complete",
          summary="声明块数与整文 SHA-256 完成上传，生成不可变审查报告")
def complete_upload(session_id: str, payload: UploadCompleteRequest,
                    db: Session = Depends(get_db)):
    digest = payload.sha256.lower()
    count = payload.chunk_count

    # 写优先抢占完成权：open -> completing（同事务内，失败/崩溃自动回滚
    # 为 open）。并发下仅一个请求抢占成功；其余等待后重读，按已完成
    # 重放/冲突处理（PostgreSQL 行锁 / SQLite 库锁行为一致）。
    claimed = False
    s: UploadSession | None = None
    for _ in range(3):
        res = db.execute(
            update(UploadSession)
            .where(UploadSession.session_id == session_id,
                   UploadSession.status == "open")
            .values(status="completing")
            .execution_options(synchronize_session="fetch"))
        if res.rowcount == 1:
            claimed = True
            break
        db.rollback()
        s = db.scalar(select(UploadSession).where(
            UploadSession.session_id == session_id))
        if s is None:
            raise HTTPException(status_code=404,
                                detail=f"上传会话 {session_id!r} 不存在")
        if s.status == "completed":
            return _completion_replay_or_conflict(s, count, digest)
        # 仍为 open：并发完成者已回滚（其声明失败），重试抢占
    if not claimed:
        raise HTTPException(status_code=409, detail={
            "error": "completion_in_progress", "session_id": session_id,
            "message": "另一个完成请求正在处理，请稍后按相同声明重试"})
    s = db.scalar(select(UploadSession).where(
        UploadSession.session_id == session_id))

    # 缺块/越界块/摘要不符/解析失败：抛错回滚，会话保持 open，不留报告
    chunks = db.scalars(select(UploadChunk)
                        .where(UploadChunk.upload_id == s.id)
                        .order_by(UploadChunk.seq)).all()
    have = {c.seq: c for c in chunks}
    present = sum(1 for seq in have if seq < count)
    missing_count = count - present
    if missing_count:
        missing_seqs: list[int] = []
        for i in range(count):
            if i not in have:
                missing_seqs.append(i)
                if len(missing_seqs) >= 100:
                    break
        raise HTTPException(status_code=409, detail={
            "error": "incomplete_upload", "session_id": session_id,
            "missing_count": missing_count, "missing_seqs": missing_seqs,
            "message": f"缺少 {missing_count} 个块（0..{count - 1} 未齐全），"
                       "未生成报告"})
    extra = sorted(seq for seq in have if seq >= count)
    if extra:
        raise HTTPException(status_code=409, detail={
            "error": "unexpected_chunks", "session_id": session_id,
            "extra_count": len(extra), "extra_seqs": extra[:100],
            "message": f"存在 {len(extra)} 个超出声明块数 {count} 的块，"
                       "未生成报告"})

    gcode = "".join(have[i].data for i in range(count))
    actual = _sha(gcode)
    if actual != digest:
        raise HTTPException(status_code=409, detail={
            "error": "digest_mismatch", "session_id": session_id,
            "declared_sha256": digest, "actual_sha256": actual,
            "message": "拼接后文本的 SHA-256 与声明不符，未生成报告"})

    # 与 POST /reviews 完全相同的审查口径：非法输入（含 G-code 解析失败）
    # 抛 422/404，回滚后不留报告、会话保持 open
    req = ReviewRequest(**s.params, gcode=gcode)
    resp = _build_review(db, req)

    result = resp.model_dump(mode="json")
    s.status = "completed"
    s.declared_chunk_count = count
    s.declared_sha256 = digest
    s.report_id = resp.report_id
    s.result = result
    s.completed_at = datetime.now(timezone.utc)
    db.commit()
    return JSONResponse(status_code=201, content=result)


@app.get("/reports/{report_id}", summary="查询不可变审查报告")
def get_report(report_id: int, db: Session = Depends(get_db)) -> dict:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {
        "report_id": report.id,
        "created_at": report.created_at,
        "used_zone_versions": report.zone_versions,
        "input_summary": report.input_summary,
        "has_collision": report.has_collision,
        "collision_line_no": report.collision_line_no,
        "collision_zone_no": report.collision_zone_no,
        "conclusion": report.conclusion,
        "message": (
            f"发现碰撞：G-code 第 {report.collision_line_no} 行与禁区编号 "
            f"{report.collision_zone_no} 冲突"
            if report.has_collision and report.collision_line_no is not None
            else ("初始刀位点位于禁区范围内" if report.has_collision
                  else "刀路与全部指定禁区版本无碰撞")),
        # 多刀模式固化：命中刀号（初始刀位为 null）与毫米半径；
        # 旧报告/单刀旧请求这两列为缺省 null
        "collision_tool_no": report.collision_tool_no,
        "collision_tool_radius_mm": report.collision_tool_radius,
        # 固化的可选最小净间隙：未启用的旧报告不含/为空，查询保持一致
        "min_clearance": report.min_clearance,
        "min_clearance_enabled": bool(report.min_clearance_enabled),
        # 多刀模式固化的执行事件轨迹；单刀请求与旧报告为 null
        "execution_trace": report.execution_trace,
    }


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
