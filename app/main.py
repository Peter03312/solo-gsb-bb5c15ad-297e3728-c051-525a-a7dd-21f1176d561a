"""FastAPI 应用：数控刀路禁入区审查服务。"""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
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
from .schemas import (BatchItemResult, BatchReviewRequest, BatchReviewResponse,
                      MAX_CHUNK_BYTES, MAX_SESSION_BYTES, MinClearanceOut,
                      ReviewParamsBase, ReviewRequest, ReviewResponse,
                      TraceEventOut, UploadChunkReceipt, UploadCompleteRequest,
                      UploadCompleteResponse, UploadSessionCreate,
                      UploadSessionOut, UploadSessionStatus, ZoneCreate,
                      ZoneDetailOut, ZoneOut, ZoneVersionCreate,
                      ZoneVersionOut)

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
                "支持长 G-code 可恢复分片上传会话（/uploads：创建时固定审查参数，"
                "从 0 起序号提交 UTF-8 文本块，乱序可达、断点重传，"
                "完成时拼接校验并按既有单项审查口径生成不可变报告）。",
)


@app.on_event("startup")
def on_startup() -> None:
    # 容器内由应用直接完成数据库初始化（表结构创建；v1.4 新增 batches 表
    # 对旧库同样由 create_all 幂等建出）
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


def _build_review(db: Session, payload: ReviewParamsBase,
                  gcode: str | None = None) -> ReviewResponse:
    """校验并计算单项审查，构造 Report（加入会话并 flush，不 commit）。

    payload 为审查参数（单项请求或上传会话固定参数，同一校验口径）；
    gcode 对上传会话由调用方传入拼接文本，单项请求取 payload.gcode。

    调用方决定提交时机：单项端点立即提交；批量端点在同一事务中
    先跑完整批再统一提交，任一项抛错即整体回滚、不留任何报告。
    非法输入抛 HTTPException / GeometryError。
    """
    gcode = payload.gcode if gcode is None else gcode  # type: ignore[attr-defined]
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
                gcode, tuple(payload.start),  # type: ignore[arg-type]
                tool_radius_map=tool_map,
                initial_tool_radius=float(payload.tool_radius))
        else:
            parsed = parse_gcode(
                gcode, tuple(payload.start))  # type: ignore[arg-type]
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
        "gcode_sha256": _sha(gcode),
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


# ---------- 长 G-code 可恢复上传会话 ----------

async def _raw_body(request: Request) -> bytes:
    """读取原始请求体（块内容以 UTF-8 明文裸传，非 JSON）。"""
    return await request.body()


def _lock_session(db: Session, session_id: str) -> UploadSession | None:
    """按外部会话标识取会话并加行锁（完成/写块串行化，数据库无关）。"""
    sess = db.scalar(select(UploadSession).where(
        UploadSession.session_id == session_id))
    if sess is None:
        return None
    if db.bind.dialect.name == "postgresql":
        db.scalar(select(UploadSession).where(
            UploadSession.id == sess.id).with_for_update())
    return sess


def _session_params(payload: ReviewParamsBase) -> dict:
    """会话固定审查参数的规范化 JSON 快照（与单项审查同一校验模型）。"""
    data = payload.model_dump(mode="json")
    data.pop("gcode", None)  # 基类无此字段；防御性剔除
    return data


def _params_to_request(params: dict, gcode: str) -> ReviewRequest:
    """把会话固定参数快照还原为审查参数对象（与 POST /reviews 同模型）。"""
    return ReviewRequest(**dict(params, gcode=gcode))


def _session_param_view(params: dict) -> dict:
    """固定参数快照的外部视图（tool_radii 键还原为 int）。"""
    view = dict(params)
    if view.get("tool_radii") is not None:
        view["tool_radii"] = {int(k): v for k, v in view["tool_radii"].items()}
    return view


def _received(db: Session, sess: UploadSession) -> tuple[list[int], int]:
    rows = db.scalars(select(UploadChunk.seq).where(
        UploadChunk.session_id == sess.id).order_by(UploadChunk.seq)).all()
    return list(rows), int(sess.total_bytes or 0)


def _chunk_receipt(sess: UploadSession, chunk: UploadChunk,
                   seqs: list[int], total_bytes: int,
                   resubmitted: bool) -> UploadChunkReceipt:
    return UploadChunkReceipt(
        session_id=sess.session_id, seq=chunk.seq,
        size_bytes=chunk.size_bytes, chunk_sha256=chunk.content_sha256,
        received_seqs=seqs, received_count=len(seqs),
        total_bytes=total_bytes, resubmitted=resubmitted)


@app.post("/uploads", response_model=UploadSessionOut, status_code=201,
          summary="创建长 G-code 可恢复上传会话（固定审查参数，返回会话标识）")
def create_upload(payload: UploadSessionCreate,
                  db: Session = Depends(get_db)) -> UploadSessionOut:
    # 创建时即固定并校验禁区编号+明确版本：引用不存在直接 404/422，
    # 与单项审查同一口径（几何在完成时仍随 _build_review 防御性再校验）
    _load_zones(db, payload.zones)

    session_id = uuid.uuid4().hex
    sess = UploadSession(session_id=session_id, status="open",
                         params=_session_params(payload), total_bytes=0)
    db.add(sess)
    db.commit()
    db.refresh(sess)

    view = _session_param_view(sess.params)
    return UploadSessionOut(
        session_id=sess.session_id, status=sess.status,
        zones=view["zones"], start=tuple(view["start"]),
        tool_radius=view["tool_radius"],
        include_min_clearance=bool(view.get("include_min_clearance", False)),
        tool_radii=view.get("tool_radii"),
        max_chunk_bytes=MAX_CHUNK_BYTES, max_session_bytes=MAX_SESSION_BYTES,
        created_at=sess.created_at)


@app.put("/uploads/{session_id}/chunks/{seq}",
         response_model=UploadChunkReceipt,
         summary="提交一个 UTF-8 文本块（序号从 0 起，可乱序，可重传）")
def put_chunk(session_id: str, seq: int,
              body: bytes = Depends(_raw_body),
              db: Session = Depends(get_db)) -> UploadChunkReceipt:
    if seq < 0:
        raise HTTPException(status_code=404, detail="块序号必须从 0 开始")
    if len(body) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "chunk_too_large",
            "max_chunk_bytes": MAX_CHUNK_BYTES,
            "size_bytes": len(body),
            "message": f"单块不得超过 {MAX_CHUNK_BYTES} 字节（64 KiB）",
        })
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_utf8",
            "seq": seq,
            "message": f"块 {seq} 不是合法 UTF-8 文本：{exc.reason}",
        })

    sess = _lock_session(db, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"上传会话 {session_id} 不存在")
    if sess.status == "completed":
        raise HTTPException(status_code=409, detail={
            "error": "session_completed",
            "session_id": session_id,
            "message": "会话已完成并生成不可变报告，拒绝新块",
        })

    chunk_hash = hashlib.sha256(body).hexdigest()
    existing = db.scalar(select(UploadChunk).where(
        UploadChunk.session_id == sess.id, UploadChunk.seq == seq))
    if existing is not None:
        # 同号重传：内容一致返回原回执；同号异内容冲突，不覆盖原块
        if existing.content != text:
            raise HTTPException(status_code=409, detail={
                "error": "chunk_conflict",
                "session_id": session_id, "seq": seq,
                "message": f"块 {seq} 已存在且内容不同",
            })
        seqs, total_bytes = _received(db, sess)
        return _chunk_receipt(sess, existing, seqs, total_bytes,
                              resubmitted=True)

    # 会话总量上限（按 UTF-8 字节累计）
    new_total = int(sess.total_bytes or 0) + len(body)
    if new_total > MAX_SESSION_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "session_too_large",
            "max_session_bytes": MAX_SESSION_BYTES,
            "total_bytes": new_total,
            "message": f"会话总量不得超过 {MAX_SESSION_BYTES} 字节（8 MiB）",
        })

    chunk = UploadChunk(session_id=sess.id, seq=seq, content=text,
                        size_bytes=len(body), content_sha256=chunk_hash)
    db.add(chunk)
    sess.total_bytes = new_total
    try:
        db.commit()
    except IntegrityError:
        # 并发同序号：唯一约束兜底，回读按重放/冲突判定
        db.rollback()
        sess = _lock_session(db, session_id)
        if sess is None:
            raise HTTPException(status_code=404,
                                detail=f"上传会话 {session_id} 不存在")
        if sess.status == "completed":
            raise HTTPException(status_code=409, detail={
                "error": "session_completed", "session_id": session_id,
                "message": "会话已完成并生成不可变报告，拒绝新块"})
        winner = db.scalar(select(UploadChunk).where(
            UploadChunk.session_id == sess.id, UploadChunk.seq == seq))
        if winner is not None and winner.content != text:
            raise HTTPException(status_code=409, detail={
                "error": "chunk_conflict", "session_id": session_id, "seq": seq,
                "message": f"块 {seq} 已存在且内容不同"})
        seqs, total_bytes = _received(db, sess)
        return _chunk_receipt(sess, winner, seqs, total_bytes,
                              resubmitted=True)

    db.refresh(chunk)
    seqs, total_bytes = _received(db, sess)
    return _chunk_receipt(sess, chunk, seqs, total_bytes, resubmitted=False)


@app.get("/uploads/{session_id}", response_model=UploadSessionStatus,
         summary="查询上传会话状态（已收序号、字节量与完成状态）")
def get_upload(session_id: str,
               db: Session = Depends(get_db)) -> UploadSessionStatus:
    sess = db.scalar(select(UploadSession).where(
        UploadSession.session_id == session_id))
    if sess is None:
        raise HTTPException(status_code=404, detail=f"上传会话 {session_id} 不存在")
    view = _session_param_view(sess.params)
    seqs, total_bytes = _received(db, sess)
    report_id = None
    if sess.completed_result is not None:
        report_id = sess.completed_result.get("report_id")
    return UploadSessionStatus(
        session_id=sess.session_id, status=sess.status,
        zones=view["zones"], start=tuple(view["start"]),
        tool_radius=view["tool_radius"],
        include_min_clearance=bool(view.get("include_min_clearance", False)),
        tool_radii=view.get("tool_radii"),
        received_seqs=seqs, received_count=len(seqs), total_bytes=total_bytes,
        max_chunk_bytes=MAX_CHUNK_BYTES, max_session_bytes=MAX_SESSION_BYTES,
        block_count=sess.block_count, content_sha256=sess.content_sha256,
        report_id=report_id, created_at=sess.created_at,
        completed_at=sess.completed_at)


@app.post("/uploads/{session_id}/complete",
          response_model=UploadCompleteResponse,
          summary="完成上传会话：校验齐全性/摘要并生成不可变审查报告")
def complete_upload(session_id: str, payload: UploadCompleteRequest,
                    db: Session = Depends(get_db)):
    sess = _lock_session(db, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"上传会话 {session_id} 不存在")

    # 已完成：相同声明（块数+摘要）重放同一报告；不同声明冲突
    if sess.status == "completed":
        same = (sess.block_count == payload.block_count
                and (sess.content_sha256 or "").lower() == payload.sha256)
        if not same:
            raise HTTPException(status_code=409, detail={
                "error": "completion_conflict",
                "session_id": session_id,
                "message": "会话已完成且完成声明（块数/摘要）不同，拒绝重放",
            })
        return JSONResponse(status_code=200,
                            content=sess.completed_result)

    chunks = db.scalars(select(UploadChunk).where(
        UploadChunk.session_id == sess.id).order_by(UploadChunk.seq)).all()
    seqs = [c.seq for c in chunks]
    expected = list(range(payload.block_count))

    # 缺块（0..block_count-1 不齐全）=> 409，不留报告，会话保持 open
    if seqs != expected:
        present = set(seqs)
        missing = [i for i in expected if i not in present]
        extra = [i for i in seqs if i >= payload.block_count]
        raise HTTPException(status_code=409, detail={
            "error": "blocks_incomplete",
            "session_id": session_id,
            "block_count": payload.block_count,
            "missing_seqs": missing,
            "extra_seqs": extra,
            "message": (f"块 0..{payload.block_count - 1} 不齐全，"
                        f"缺少 {len(missing)} 块"),
        })

    # 总量超限（8 MiB）：写块路径已拦截，此处对历史/边界数据兜底
    total_bytes = sum(c.size_bytes for c in chunks)
    if total_bytes > MAX_SESSION_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "session_too_large",
            "max_session_bytes": MAX_SESSION_BYTES,
            "total_bytes": total_bytes,
            "message": f"会话总量不得超过 {MAX_SESSION_BYTES} 字节（8 MiB）",
        })

    gcode = "".join(c.content for c in chunks)
    digest = hashlib.sha256(gcode.encode("utf-8")).hexdigest()
    # 摘要不符 => 422，不留报告，会话保持 open（可修正声明后重试）
    if digest != payload.sha256:
        raise HTTPException(status_code=422, detail={
            "error": "sha256_mismatch",
            "session_id": session_id,
            "expected_sha256": payload.sha256,
            "actual_sha256": digest,
            "message": "拼接后文本 SHA-256 与声明不符",
        })

    # 既有单项审查口径（解析失败/几何/参数非法在此抛 422，不留报告）。
    # 此处仅 flush 报告（未提交）：随后由条件 UPDATE 原子占位完成权，
    # 占位失败则回滚掉这份报告，保证并发完成只产生一份报告。
    review_req = _params_to_request(sess.params, gcode)
    resp = _build_review(db, review_req, gcode=gcode)

    result = resp.model_dump(mode="json")
    result = {"session_id": sess.session_id, **result}

    # 原子置完成：仅在仍为 open 时写入完成声明与结果快照（数据库无关）。
    # PostgreSQL 下此前已持有该行行锁，并发完成者只会在锁后看到 completed。
    upd = db.execute(update(UploadSession).where(
        UploadSession.id == sess.id,
        UploadSession.status == "open").values(
            status="completed",
            block_count=payload.block_count,
            content_sha256=digest,
            completed_result=result,
            completed_at=models._utcnow()))
    if upd.rowcount == 0:
        # 并发下另一请求已完成：回滚本次 flush 的报告，按首次结果重放/冲突
        db.rollback()
        winner = _lock_session(db, session_id)
        if winner is not None and winner.status == "completed":
            same = (winner.block_count == payload.block_count
                    and (winner.content_sha256 or "").lower() == payload.sha256)
            if same:
                return JSONResponse(status_code=200,
                                    content=winner.completed_result)
        raise HTTPException(status_code=409, detail={
            "error": "completion_conflict", "session_id": session_id,
            "message": "会话已被并发完成且声明不同"})

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
