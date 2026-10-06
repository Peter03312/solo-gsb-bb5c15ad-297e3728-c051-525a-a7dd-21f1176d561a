"""刀路-禁入区碰撞引擎。

判定条件（连续运动、按程序顺序）：
刀位点 Z 位于禁区闭合高度范围 [z_min, z_max] 内，
且水平路径与填充多边形的距离不大于刀具半径时 => 碰撞；
边界接触同样计为碰撞。

线段运动不做端点采样：对每条多边形边建立
“运动点到边距离 ≤ 刀半径”的二次不等式，精确求解参数 t 区间，
再与 Z 高度区间求交，取全程最早 t。
圆弧运动：弧-边距离精确求值 + 子弧二分定位首个碰撞进度。

可选全程最小水平净间隙（minimum_clearance）：
净间隙 = 连续 XY 路径到填充多边形最小距离 − 刀具半径，
同样只在刀尖 Z 处于禁区闭合高度范围的部分上统计；
直线按分段二次函数精确求极值，圆弧按弧-边候选角解析求值。

多刀模式执行事件轨迹（build_execution_trace）：
按执行顺序记录初始刀位、每次 T 切换的静止刀位（含与运动同行，
先于本行运动）与每段运动；每项给出原行号、刀号、半径、起止坐标
与命中禁区编号（或空）。首碰撞即轨迹中首个命中事件；
可选最小净间隙沿同一事件顺序裁决。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import (
    EPS, CONTACT_EPS, XY,
    cross, dist, point_in_polygon, point_polygon_distance,
    earliest_arc_hit, subarc_hits, arc_polygon_closest,
)
from .gcode import Motion, ToolChange


@dataclass(frozen=True)
class Zone:
    zone_no: int
    polygon: list[XY]
    z_min: float
    z_max: float


@dataclass
class Collision:
    line_no: int | None
    zone_no: int
    progress: float          # 该段运动上的碰撞进度（t 或角度 s），仅用于排序
    order: int = 0           # 运动段序号
    # 多刀模式/新报告固化：命中时生效刀号与半径（初始刀位刀号为 None）
    tool_no: int | None = None
    tool_radius: float = 0.0


# ---------- 区间工具 ----------

def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并重叠/相接区间。"""
    if not intervals:
        return []
    intervals = sorted((max(0.0, a), min(1.0, b)) for a, b in intervals
                       if b >= a - EPS)
    out: list[list[float]] = []
    for a, b in intervals:
        if out and a <= out[-1][1] + EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _intersect(iv1: list[tuple[float, float]],
               iv2: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    i = j = 0
    while i < len(iv1) and j < len(iv2):
        a = max(iv1[i][0], iv2[j][0])
        b = min(iv1[i][1], iv2[j][1])
        if a <= b + EPS:
            out.append((a, b))
        if iv1[i][1] < iv2[j][1]:
            i += 1
        else:
            j += 1
    # 交集可能因输入未夹断而略超出 [0,1]，统一夹回并合并相接区间
    return _merge(out)


def _solve_quad_le(aa: float, bb: float, cc: float,
                   k: float) -> list[tuple[float, float]]:
    """求解 aa t^2 + bb t + cc <= k 的实数区间。"""
    A = aa
    B = bb
    C = cc - k
    if abs(A) <= 1e-18:
        if abs(B) <= 1e-18:
            return [(-math.inf, math.inf)] if C <= CONTACT_EPS else []
        t = -C / B
        if B > 0:
            return [(-math.inf, t)]
        return [(t, math.inf)]
    disc = B * B - 4.0 * A * C
    if disc < -EPS:
        if A < 0:
            return [(-math.inf, math.inf)]
        return []
    disc = max(0.0, disc)
    sq = math.sqrt(disc)
    t1 = (-B - sq) / (2.0 * A)
    t2 = (-B + sq) / (2.0 * A)
    lo, hi = sorted((t1, t2))
    if A > 0:
        return [(lo, hi)]
    return [(-math.inf, lo), (hi, math.inf)]


# ---------- 线段运动 ----------

def _z_window(m: Motion, zone: Zone) -> list[tuple[float, float]]:
    """运动参数 t∈[0,1] 中，刀尖 Z 位于禁区闭合高度内的区间。"""
    z0, z1 = m.start[2], m.end[2]
    dz = z1 - z0
    if abs(dz) <= EPS:
        return [(0.0, 1.0)] if zone.z_min - EPS <= z0 <= zone.z_max + EPS else []
    t_lo = (zone.z_min - z0) / dz
    t_hi = (zone.z_max - z0) / dz
    lo, hi = sorted((t_lo, t_hi))
    lo = max(0.0, lo)
    hi = min(1.0, hi)
    # 闭合区间：端点含边界（z 全程在范围内时直接覆盖整段）
    if lo <= hi + EPS:
        return [(max(0.0, lo), min(1.0, hi))]
    return []


def _line_capsule_interval(p0: XY, p1: XY, a: XY, b: XY,
                           tool_r: float) -> list[tuple[float, float]]:
    """运动点 P(t)=p0+t(p1-p0) 到固定边 a-b 的距离 ≤ tool_r 的 t 区间。

    P 到无限直线的垂足参数 tc 落在 [0,1] 时用二次不等式；
    否则距离由近端点决定（|P-e| ≤ r 的圆域也是二次不等式）。
    两种情况合并即可精确覆盖整个线段的膨胀区域（胶囊）。
    """
    d = (p1[0] - p0[0], p1[1] - p0[1])
    ab = (b[0] - a[0], b[1] - a[1])
    length2 = ab[0] * ab[0] + ab[1] * ab[1]
    if length2 <= EPS * EPS:
        # 退化边：按点处理
        dx = p0[0] - a[0]
        dy = p0[1] - a[1]
        return _merge(_solve_quad_le(dot2(d, d), 2.0 * (d[0] * dx + d[1] * dy),
                                     dx * dx + dy * dy, tool_r ** 2))
    # P(t) 在边无限线上的投影位置 s(t)
    ap = (p0[0] - a[0], p0[1] - a[1])
    # s(t) = (ab·(ap + t d)) / |ab|^2 = s0 + sd t
    s0 = (ab[0] * ap[0] + ab[1] * ap[1]) / length2
    sd = (ab[0] * d[0] + ab[1] * d[1]) / length2

    intervals: list[tuple[float, float]] = []
    r2 = (tool_r + CONTACT_EPS) ** 2

    if sd != 0.0:
        t_a = -s0 / sd
        t_b = (1.0 - s0) / sd
        t0, t1 = sorted((t_a, t_b))
        slab = [(max(0.0, t0), min(1.0, t1))] if t0 <= 1.0 + EPS and t1 >= -EPS else []
    else:
        slab = [(0.0, 1.0)] if 0.0 - EPS <= s0 <= 1.0 + EPS else []

    if slab:
        # 垂足段：垂直距离平方 = |ap + t d - s(t) ab|^2
        # w(t) = ap - s0 ab + t (d - sd ab)
        w0 = (ap[0] - s0 * ab[0], ap[1] - s0 * ab[1])
        wd = (d[0] - sd * ab[0], d[1] - sd * ab[1])
        ivs = _solve_quad_le(dot2(wd, wd),
                             2.0 * (w0[0] * wd[0] + w0[1] * wd[1]),
                             dot2(w0, w0), r2)
        for iv in ivs:
            cut = _intersect([iv], slab)
            intervals.extend(cut)

    # 两端点圆域（处理垂足在边外的情形）
    for e in (a, b):
        dx = p0[0] - e[0]
        dy = p0[1] - e[1]
        intervals.extend(
            _solve_quad_le(dot2(d, d), 2.0 * (d[0] * dx + d[1] * dy),
                           dx * dx + dy * dy, r2))
    return _merge(intervals)


def dot2(a: XY, b: XY) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _line_inside_intervals(p0: XY, p1: XY,
                           poly: list[XY]) -> list[tuple[float, float]]:
    """水平路径（参数 t）严格位于填充多边形内部/边界的区间。

    收集运动线段与各边的真相交参数作为事件点，事件点之间内外状态恒定；
    事件点本身（边界接触）也并入结果。
    """
    d = (p1[0] - p0[0], p1[1] - p0[1])
    if dot2(d, d) <= EPS * EPS:
        # 水平方向静止
        return [(0.0, 1.0)] if point_in_polygon(p0, poly) else []

    events: set[float] = {0.0, 1.0}
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ab = (b[0] - a[0], b[1] - a[1])
        denom = cross(d, ab)
        if abs(denom) <= EPS:
            continue
        t = cross((a[0] - p0[0], a[1] - p0[1]), ab) / denom
        u = cross((a[0] - p0[0], a[1] - p0[1]), d) / denom
        if -EPS <= t <= 1.0 + EPS and -EPS <= u <= 1.0 + EPS:
            events.add(max(0.0, min(1.0, t)))

    evs = sorted(events)
    result: list[tuple[float, float]] = []
    for k in range(len(evs) - 1):
        t_lo, t_hi = evs[k], evs[k + 1]
        if t_hi - t_lo <= EPS:
            # 事件点自身（边界）计入
            result.append((t_lo, t_lo))
            continue
        t_mid = (t_lo + t_hi) * 0.5
        pm = (p0[0] + d[0] * t_mid, p0[1] + d[1] * t_mid)
        if point_in_polygon(pm, poly):
            result.append((t_lo, t_hi))
    # 端点边界检查（point_in_polygon 已在采样点覆盖，此处显式加入事件点）
    for t in (0.0, 1.0):
        p = (p0[0] + d[0] * t, p0[1] + d[1] * t)
        if point_in_polygon(p, poly):
            result.append((t, t))
    return _merge(result)


def _segment_overlap_events(p0: XY, p1: XY,
                            poly: list[XY]) -> list[tuple[float, float]]:
    """运动线段与多边形边共线重合的参数区间（含重合端点）。

    这类“贴边”路径的中点不在任何穿边事件上，通用内外采样会漏掉，
    需显式按一维参数区间收集。
    """
    d = (p1[0] - p0[0], p1[1] - p0[1])
    d2 = dot2(d, d)
    if d2 <= EPS * EPS:
        return [(0.0, 0.0)] if point_in_polygon(p0, poly) else []
    out: list[tuple[float, float]] = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        # p0->a 与 d 共线？
        if abs(cross((a[0] - p0[0], a[1] - p0[1]), d)) > EPS:
            continue
        # 边方向与运动方向平行
        ab = (b[0] - a[0], b[1] - a[1])
        if abs(cross(ab, d)) > EPS:
            continue
        ts = []
        for e in (a, b):
            t = ((e[0] - p0[0]) * d[0] + (e[1] - p0[1]) * d[1]) / d2
            if -EPS <= t <= 1.0 + EPS:
                ts.append(max(0.0, min(1.0, t)))
        if ts:
            out.append((min(ts), max(ts)))
    return _merge(out)


def _vertex_passthrough_events(p0: XY, p1: XY,
                               poly: list[XY]) -> list[tuple[float, float]]:
    """运动路径恰好经过多边形顶点的参数点（顶点属于多边形，必为碰撞点）。

    覆盖“折线穿单个顶点”这类退化为单个参数点的接触，
    该点可能被一般区间运算当作零测事件漏掉。
    """
    d = (p1[0] - p0[0], p1[1] - p0[1])
    if dot2(d, d) <= EPS * EPS:
        return [(0.0, 0.0)] if point_in_polygon(p0, poly) else []
    events: list[tuple[float, float]] = []
    for v in poly:
        ap = (v[0] - p0[0], v[1] - p0[1])
        # ap = t d 共线
        if abs(cross(ap, d)) > EPS:
            continue
        t = (ap[0] * d[0] + ap[1] * d[1]) / dot2(d, d)
        if -EPS <= t <= 1.0 + EPS:
            t = max(0.0, min(1.0, t))
            events.append((t, t))
    return events


def _line_earliest(m: Motion, zone: Zone, tool_r: float) -> float | None:
    """线段运动对单个禁区的最早碰撞参数 t，无碰撞返回 None。"""
    zwin = _z_window(m, zone)
    if not zwin:
        return None
    p0 = (m.start[0], m.start[1])
    p1 = (m.end[0], m.end[1])

    poly = zone.polygon
    hit_t: list[tuple[float, float]] = list(_line_inside_intervals(p0, p1, poly))
    hit_t.extend(_vertex_passthrough_events(p0, p1, poly))

    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        hit_t.extend(_line_capsule_interval(p0, p1, a, b, tool_r))

    hit_t = _merge(hit_t)
    hit_t = _intersect(hit_t, zwin)
    if not hit_t:
        return None
    return min(a for a, _ in hit_t)


# ---------- 圆弧运动 ----------

def _arc_earliest(m: Motion, zone: Zone, tool_r: float) -> float | None:
    z0 = m.start[2]
    if not (zone.z_min - EPS <= z0 <= zone.z_max + EPS):
        return None
    if not subarc_hits(m.center, m.radius, m.theta0, m.sweep,
                       zone.polygon, tool_r):
        return None
    return earliest_arc_hit(m.center, m.radius, m.theta0, m.sweep,
                            zone.polygon, tool_r)


# ---------- 总入口 ----------

def _motion_hit(m: Motion, zone: Zone, tool_r: float) -> float | None:
    if m.kind == "line":
        return _line_earliest(m, zone, tool_r)
    return _arc_earliest(m, zone, tool_r)


def check_path(motions: list[Motion], zones: list[Zone],
               tool_radius: float) -> Collision | None:
    """按程序顺序检查全部连续运动，返回首个碰撞。

    首个碰撞：运动段序号最小；同一段内碰撞进度最小；
    进度相同取禁区编号最小。
    """
    best: Collision | None = None
    for order, m in enumerate(motions):
        for zone in zones:  # zones 已按 zone_no 排序
            prog = _motion_hit(m, zone, tool_radius)
            if prog is None:
                continue
            cand = Collision(m.line_no, zone.zone_no, prog, order)
            if best is None or _earlier(cand, best):
                best = cand
    return best


def _earlier(a: Collision, b: Collision) -> bool:
    if a.order != b.order:
        return a.order < b.order
    if abs(a.progress - b.progress) > EPS:
        return a.progress < b.progress
    return a.zone_no < b.zone_no


def initial_collision(pos: tuple[float, float, float],
                      zones: list[Zone], tool_radius: float) -> int | None:
    """初始刀位点本身是否已在某禁区（用于静止/首点判定），返回禁区号。"""
    x, y, z = pos
    for zone in zones:
        if zone.z_min - EPS <= z <= zone.z_max + EPS:
            if point_polygon_distance((x, y), zone.polygon) <= tool_radius + CONTACT_EPS:
                return zone.zone_no
    return None


def _static_collision(pos: tuple[float, float, float],
                      zones: list[Zone], tool_r: float,
                      line_no: int | None, seq: int,
                      tool_no: int | None) -> Collision | None:
    """静止刀位（初始刀位或独立 T 行切换后）的最早碰撞。

    静止点无“段内进度”（按 0 计）；同一点多个禁区命中时取最小编号。
    """
    x, y, z = pos
    best: Collision | None = None
    for zone in zones:  # 已按 zone_no 排序
        if zone.z_min - EPS <= z <= zone.z_max + EPS and \
                point_polygon_distance((x, y), zone.polygon) <= \
                tool_r + CONTACT_EPS:
            cand = Collision(line_no, zone.zone_no, 0.0, seq,
                             tool_no=tool_no, tool_radius=tool_r)
            if best is None or cand.zone_no < best.zone_no:
                best = cand
    return best


def check_program(motions: list[Motion],
                  tool_events: list[ToolChange],
                  zones: list[Zone],
                  start: tuple[float, float, float],
                  initial_radius: float) -> Collision | None:
    """多刀模式：按真实执行顺序检查初始刀位、静止换刀事件与各段运动。

    事件顺序：初始刀位（seq=0，刀号为空，半径为初始刀半径）→
    解析出的运动/静止换刀事件（seq 从 1 起，同行 T 的换刀事件先于本行
    运动）。首碰撞即执行事件轨迹中首个命中事件；等值先按执行顺序，
    再按段内进度、禁区编号裁决；每段运动使用该段生效刀具的半径。
    """
    for ev in build_execution_trace(motions, tool_events, zones, start,
                                    initial_radius):
        if ev.hit is not None:
            return ev.hit
    return None


def _program_earlier(a: Collision, b: Collision) -> bool:
    """等值裁决：执行顺序（seq）→ 段内进度 → 禁区编号。"""
    if a.order != b.order:
        return a.order < b.order
    if abs(a.progress - b.progress) > EPS:
        return a.progress < b.progress
    return a.zone_no < b.zone_no


# ---------- 多刀执行事件轨迹 ----------

@dataclass
class TraceEvent:
    """执行事件轨迹中的一项（多刀模式）。

    按执行顺序记录：初始刀位（kind="initial"，刀号为空）、每次 T 切换的
    静止刀位（kind="tool_change"，含与运动同行，先于本行运动）、
    每段运动（kind="motion"）。hit 为该事件内最早命中（无命中为 None）。
    """
    seq: int                 # 执行顺序（初始刀位为 0，其余与解析事件序号一致）
    kind: str                # "initial" / "tool_change" / "motion"
    line_no: int | None      # 原行号（初始刀位为 None）
    tool_no: int | None      # 生效刀号（初始刀位为 None）
    tool_radius: float       # 生效刀具半径（mm）
    start: tuple[float, float, float]  # 起坐标（静止事件与 end 相同）
    end: tuple[float, float, float]    # 止坐标
    hit: Collision | None    # 该事件最早命中的碰撞（含禁区编号），未命中为 None


def build_execution_trace(motions: list[Motion],
                          tool_events: list[ToolChange],
                          zones: list[Zone],
                          start: tuple[float, float, float],
                          initial_radius: float) -> list[TraceEvent]:
    """构建多刀模式的执行事件轨迹（按执行顺序）。

    顺序：初始刀位（seq=0，刀号为空，初始半径）→ 解析出的静止换刀事件与
    运动段（seq 从 1 起，同行 T 的换刀事件先于本行运动）。
    静止事件按生效半径检查静止刀位；运动事件按该段生效半径检查，
    段内多禁区命中时按段内进度、禁区编号取最早。
    """
    trace: list[TraceEvent] = [TraceEvent(
        0, "initial", None, None, float(initial_radius), start, start,
        _static_collision(start, zones, initial_radius, None, 0, None))]

    events: list = list(tool_events) + list(motions)
    events.sort(key=lambda e: e.seq)
    for ev in events:
        if isinstance(ev, ToolChange):
            hit = _static_collision(ev.pos, zones, ev.tool_radius,
                                    ev.line_no, ev.seq, ev.tool_no)
            trace.append(TraceEvent(ev.seq, "tool_change", ev.line_no,
                                    ev.tool_no, ev.tool_radius,
                                    ev.pos, ev.pos, hit))
            continue
        best: Collision | None = None
        for zone in zones:  # 已按 zone_no 排序
            prog = _motion_hit(ev, zone, ev.tool_radius)
            if prog is None:
                continue
            c = Collision(ev.line_no, zone.zone_no, prog, ev.seq,
                          tool_no=ev.tool_no, tool_radius=ev.tool_radius)
            if best is None or _program_earlier(c, best):
                best = c
        trace.append(TraceEvent(ev.seq, "motion", ev.line_no, ev.tool_no,
                                ev.tool_radius, ev.start, ev.end, best))
    return trace


# ---------- 全程最小水平净间隙 ----------
#
# 净间隙 = 连续 XY 路径到填充多边形的最小距离 − 刀具半径。
# 仅统计刀尖 Z 位于该禁区闭合高度范围内的那部分运动：
# - 路径点在多边形外：距离为到最近边/顶点的正距离，距离恰等于刀半径时
#   刀具外沿刚好碰到边界（边界接触），净间隙为 0；
# - 路径点在多边形内部或边界上：到填充多边形的距离为 0，净间隙为 -刀半径（负）。

@dataclass(frozen=True)
class ClearancePoint:
    net: float                 # 最小净间隙（mm，可为负）
    line_no: int | None        # 原行号；无运动段/初始刀位/静止换刀时为 None
    order: int                 # 运动段序号（无运动段为 0；多刀模式为事件 seq）
    progress: float            # 段内进度（直线 t，圆弧 s）；静止刀位为 0
    zone_no: int
    point: tuple[float, float, float]  # 取得最小值时的刀尖坐标 (x, y, z)（mm）
    # 多刀模式/新报告固化：取得最小值时生效刀号与半径（初始刀位刀号为 None）
    tool_no: int | None = None
    tool_radius: float = 0.0


def _z_at(m: Motion, t: float) -> float:
    return m.start[2] + (m.end[2] - m.start[2]) * t


def _snap_net(raw_distance: float, tool_r: float) -> float:
    """距离（mm）换算净间隙：distance - tool_r；接触容差内对齐为 0。"""
    d = 0.0 if raw_distance <= CONTACT_EPS else raw_distance
    net = d - tool_r
    if abs(net) <= CONTACT_EPS:
        return 0.0
    return net


def _clearance_better(cp: ClearancePoint, best: ClearancePoint | None) -> bool:
    """等值（接触容差内）时：运动顺序、段内进度、禁区编号。"""
    if best is None:
        return True
    if cp.net < best.net - CONTACT_EPS:
        return True
    if cp.net > best.net + CONTACT_EPS:
        return False
    if cp.order != best.order:
        return cp.order < best.order
    if abs(cp.progress - best.progress) > EPS:
        return cp.progress < best.progress
    return cp.zone_no < best.zone_no


def _line_edge_closest(p0: XY, p1: XY, a: XY, b: XY,
                       window: list[tuple[float, float]]
                       ) -> tuple[float, float] | None:
    """运动线段 p0-p1（参数 t）在 window（t 区间）内到固定边 a-b 的最小原始距离及 t。

    把 [0,1] 按“运动点在边无限线上的投影参数 s 是否落在 [0,1]”切成若干区间：
    - 垂足段内：距离平方是关于 t 的二次式，取凸二次顶点 + 区间端点；
    - 垂足段外：距离由近端点决定（|P-a|² 或 |P-b|²），同样取顶点 + 端点。
    逐区间求极值后再与 window 求交，保证不遗漏 Z 窗口边界处的最小值。
    """
    d = (p1[0] - p0[0], p1[1] - p0[1])
    ab = (b[0] - a[0], b[1] - a[1])
    length2 = ab[0] * ab[0] + ab[1] * ab[1]

    # 各分段：(t_lo, t_hi, 该段距离平方二次式系数 q2 t² + q1 t + q0)
    pieces: list[tuple[float, float, float, float, float]] = []

    if length2 <= EPS * EPS:
        # 退化边：按点 a 处理
        dx, dy = p0[0] - a[0], p0[1] - a[1]
        pieces.append((0.0, 1.0, dot2(d, d),
                       2.0 * (d[0] * dx + d[1] * dy), dx * dx + dy * dy))
    else:
        ap = (p0[0] - a[0], p0[1] - a[1])
        s0 = (ab[0] * ap[0] + ab[1] * ap[1]) / length2
        sd = (ab[0] * d[0] + ab[1] * d[1]) / length2

        if abs(sd) <= EPS:
            # 投影位置恒定
            if 0.0 - EPS <= s0 <= 1.0 + EPS:
                w0 = (ap[0] - s0 * ab[0], ap[1] - s0 * ab[1])
                wd = (d[0] - sd * ab[0], d[1] - sd * ab[1])
                pieces.append((0.0, 1.0, dot2(wd, wd),
                               2.0 * (w0[0] * wd[0] + w0[1] * wd[1]),
                               dot2(w0, w0)))
            else:
                e = a if s0 < 0.0 else b
                ex, ey = p0[0] - e[0], p0[1] - e[1]
                pieces.append((0.0, 1.0, dot2(d, d),
                               2.0 * (d[0] * ex + d[1] * ey), ex * ex + ey * ey))
        else:
            # s(t)=s0+sd t 跨过 0 和 1 的事件参数
            cuts = [0.0, 1.0]
            for s_edge in (0.0, 1.0):
                t_cut = (s_edge - s0) / sd
                if EPS < t_cut < 1.0 - EPS:
                    cuts.append(t_cut)
            cuts.sort()

            def quad_for(lo: float, hi: float) -> tuple[float, float, float]:
                t_mid = (lo + hi) * 0.5
                s_mid = s0 + sd * t_mid
                if -EPS <= s_mid <= 1.0 + EPS:
                    w0 = (ap[0] - s0 * ab[0], ap[1] - s0 * ab[1])
                    wd = (d[0] - sd * ab[0], d[1] - sd * ab[1])
                    return (dot2(wd, wd),
                            2.0 * (w0[0] * wd[0] + w0[1] * wd[1]),
                            dot2(w0, w0))
                e = a if s_mid < 0.0 else b
                ex, ey = p0[0] - e[0], p0[1] - e[1]
                return (dot2(d, d),
                        2.0 * (d[0] * ex + d[1] * ey), ex * ex + ey * ey)

            for k in range(len(cuts) - 1):
                lo, hi = cuts[k], cuts[k + 1]
                q2, q1, q0 = quad_for(lo, hi)
                pieces.append((lo, hi, q2, q1, q0))

    # 逐段求与 window 交集内的最小值：凸二次顶点 + 交区间端点
    best_d = math.inf
    best_t: float | None = None

    def consider(t: float, q2: float, q1: float, q0: float) -> None:
        nonlocal best_d, best_t
        v = q2 * t * t + q1 * t + q0
        v = max(0.0, v)
        dv = math.sqrt(v)
        if best_t is None or dv < best_d - EPS or \
                (abs(dv - best_d) <= EPS and t < best_t):
            best_d, best_t = dv, t

    for lo, hi, q2, q1, q0 in pieces:
        for wlo, whi in window:
            clo = max(lo, wlo)
            chi = min(hi, whi)
            if clo > chi + EPS:
                continue
            clo = max(0.0, clo)
            chi = min(1.0, chi)
            if clo > chi + EPS:
                continue
            consider(clo, q2, q1, q0)
            consider(chi, q2, q1, q0)
            if q2 > EPS * EPS:
                tv = -q1 / (2.0 * q2)
                if clo + EPS < tv < chi - EPS:
                    consider(tv, q2, q1, q0)
    if best_t is None:
        return None
    return best_d, best_t


def _line_closest(m: Motion, zone: Zone) -> tuple[float, float] | None:
    """直线运动对单禁区：仅在 Z 窗口部分，返回 (最小原始距离, t)。"""
    zwin = _z_window(m, zone)
    if not zwin:
        return None
    p0 = (m.start[0], m.start[1])
    p1 = (m.end[0], m.end[1])
    if dot2((p1[0] - p0[0], p1[1] - p0[1]),
            (p1[0] - p0[0], p1[1] - p0[1])) <= EPS * EPS:
        # XY 静止：窗口内全程为同一点，取最早进入高度范围的进度
        return point_polygon_distance(p0, zone.polygon), zwin[0][0]

    # Z 窗口内有路径位于填充多边形内部/边界 => 距离 0；
    # 内部区间内最早点即该段最小值点（贴合 _line_earliest 的覆盖口径）。
    inside = _intersect(_merge(
        list(_line_inside_intervals(p0, p1, zone.polygon))
        + _vertex_passthrough_events(p0, p1, zone.polygon)
        + _segment_overlap_events(p0, p1, zone.polygon)),
        _merge(zwin))
    if inside:
        return 0.0, min(a for a, _ in inside)

    best_d = math.inf
    best_t = 1.0
    poly = zone.polygon
    n = len(poly)
    for i in range(n):
        found = _line_edge_closest(p0, p1, poly[i], poly[(i + 1) % n], zwin)
        if found is None:
            continue
        dv, t = found
        if dv < best_d - EPS or (abs(dv - best_d) <= EPS and t < best_t):
            best_d, best_t = dv, t
    return best_d, best_t


def _arc_closest(m: Motion, zone: Zone) -> tuple[float, float] | None:
    """圆弧运动对单禁区：仅在 Z 窗口（恒定 Z）部分，返回 (距离, s)。"""
    zwin = _z_window(m, zone)
    if not zwin:
        return None
    return arc_polygon_closest(m.center, m.radius, m.theta0, m.sweep,
                               zone.polygon)


def _motion_clearance_point(m: Motion, zone: Zone, tool_radius: float,
                            order: int) -> ClearancePoint | None:
    """单段运动对单禁区：仅在 Z 窗口部分求最小净间隙候选点。"""
    if m.kind == "line":
        found = _line_closest(m, zone)
    else:
        found = _arc_closest(m, zone)
    if found is None:
        return None
    d, prog = found
    prog = max(0.0, min(1.0, prog))
    if m.kind == "line":
        x = m.start[0] + (m.end[0] - m.start[0]) * prog
        y = m.start[1] + (m.end[1] - m.start[1]) * prog
        z = _z_at(m, prog)
    else:
        theta = m.theta0 + m.sweep * prog
        x = m.center[0] + m.radius * math.cos(theta)
        y = m.center[1] + m.radius * math.sin(theta)
        z = m.start[2]
    return ClearancePoint(_snap_net(d, tool_radius), m.line_no, order,
                          prog, zone.zone_no, (x, y, z))


def _static_clearance_point(pos: tuple[float, float, float], zone: Zone,
                            tool_radius: float, line_no: int | None,
                            order: int,
                            tool_no: int | None) -> ClearancePoint | None:
    """静止刀位（初始刀位/独立 T 行）对单禁区的净间隙候选。"""
    px, py, pz = pos
    if not (zone.z_min - EPS <= pz <= zone.z_max + EPS):
        return None
    d = point_polygon_distance((px, py), zone.polygon)
    return ClearancePoint(_snap_net(d, tool_radius), line_no, order, 0.0,
                          zone.zone_no,
                          (float(px), float(py), float(pz)),
                          tool_no=tool_no, tool_radius=tool_radius)


def minimum_clearance(motions: list[Motion], zones: list[Zone],
                      tool_radius: float,
                      start: tuple[float, float, float]) -> ClearancePoint | None:
    """全程最小水平净间隙（单刀模式）。

    对每段运动、每个禁区，仅在刀尖 Z 处于该禁区闭合高度范围的部分求连续 XY
    路径到填充多边形的最小距离，再减刀具半径；直线、圆弧、整圆及中途进入
    高度范围均覆盖。等值时按运动顺序、段内进度、禁区编号选取。
    全程（含无运动段时的初始刀位）均不在任何禁区高度内时返回 None。
    """
    best: ClearancePoint | None = None

    if not motions:
        px, py, pz = start
        for zone in zones:  # 已按 zone_no 排序
            if not (zone.z_min - EPS <= pz <= zone.z_max + EPS):
                continue
            d = point_polygon_distance((px, py), zone.polygon)
            cp = ClearancePoint(_snap_net(d, tool_radius), None, 0, 0.0,
                                zone.zone_no, (float(px), float(py), float(pz)))
            if _clearance_better(cp, best):
                best = cp
        return best

    for order, m in enumerate(motions):
        for zone in zones:
            cp = _motion_clearance_point(m, zone, tool_radius, order)
            if cp is not None and _clearance_better(cp, best):
                best = cp
    return best


def minimum_clearance_program(motions: list[Motion],
                              tool_events: list[ToolChange],
                              zones: list[Zone],
                              start: tuple[float, float, float],
                              initial_radius: float) -> ClearancePoint | None:
    """多刀模式全程最小水平净间隙。

    初始刀位（刀号为空、初始半径）先参与，随后按执行顺序（seq）合并
    静止换刀事件与运动段；每个事件使用其生效刀具的半径计算净间隙。
    等值先按执行顺序，再按段内进度、禁区编号裁决。
    全程均不在任何禁区高度内时返回 None。
    """
    best: ClearancePoint | None = None
    for zone in zones:  # 已按 zone_no 排序
        cp = _static_clearance_point(start, zone, initial_radius, None,
                                     0, None)
        if cp is not None and _clearance_better(cp, best):
            best = cp

    events: list = list(tool_events) + list(motions)
    events.sort(key=lambda e: e.seq)
    for ev in events:
        if isinstance(ev, ToolChange):
            for zone in zones:
                cp = _static_clearance_point(ev.pos, zone, ev.tool_radius,
                                             ev.line_no, ev.seq, ev.tool_no)
                if cp is not None and _clearance_better(cp, best):
                    best = cp
            continue
        for zone in zones:
            cp = _motion_clearance_point(ev, zone, ev.tool_radius, ev.seq)
            if cp is None:
                continue
            cp = ClearancePoint(cp.net, cp.line_no, ev.seq, cp.progress,
                                cp.zone_no, cp.point,
                                tool_no=ev.tool_no,
                                tool_radius=ev.tool_radius)
            if _clearance_better(cp, best):
                best = cp
    return best
