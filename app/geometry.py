"""二维毫米制几何运算。

所有坐标均为毫米。多边形统一视为“填充区域”：
- 点落在多边形内部或边界上 => 到多边形距离为 0；
- 否则距离为到所有边的最小点-线段距离。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

# 绝对容差（毫米）
EPS = 1e-9
# 判定接触/越界时对刀半径做的正向容差（毫米），保证“边界接触也算碰撞”
CONTACT_EPS = 1e-8

XY = tuple[float, float]


# ---------- 基础向量运算 ----------

def sub(a: XY, b: XY) -> XY:
    return (a[0] - b[0], a[1] - b[1])


def add(a: XY, b: XY) -> XY:
    return (a[0] + b[0], a[1] + b[1])


def mul(a: XY, k: float) -> XY:
    return (a[0] * k, a[1] * k)


def cross(a: XY, b: XY) -> float:
    return a[0] * b[1] - a[1] * b[0]


def dot(a: XY, b: XY) -> float:
    return a[0] * b[0] + a[1] * b[1]


def norm(a: XY) -> float:
    return math.hypot(a[0], a[1])


def dist(a: XY, b: XY) -> float:
    return norm(sub(a, b))


def point_segment_distance(p: XY, a: XY, b: XY) -> float:
    ab = sub(b, a)
    length2 = dot(ab, ab)
    if length2 <= EPS * EPS:
        return dist(p, a)
    t = dot(sub(p, a), ab) / length2
    t = max(0.0, min(1.0, t))
    q = add(a, mul(ab, t))
    return dist(p, q)


def on_segment(p: XY, a: XY, b: XY, tol: float = EPS) -> bool:
    """p 是否在线段 ab 上（含端点）。"""
    if abs(cross(sub(b, a), sub(p, a))) > tol:
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def segments_intersect_proper(a: XY, b: XY, c: XY, d: XY, tol: float = EPS) -> bool:
    """线段 ab 与 cd 是否存在“真相交”（相对内部交叉）。"""
    r = sub(b, a)
    s = sub(d, c)
    rxs = cross(r, s)
    if abs(rxs) <= tol:
        return False  # 平行不做真相交
    t = cross(sub(c, a), s) / rxs
    u = cross(sub(c, a), r) / rxs
    return tol < t < 1.0 - tol and tol < u < 1.0 - tol


def signed_area(poly: Sequence[XY]) -> float:
    area = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area * 0.5


def point_in_polygon(p: XY, poly: Sequence[XY]) -> bool:
    """点是否在多边形内部或边界上（射线法 + 边界判定）。"""
    n = len(poly)
    for i in range(n):
        if on_segment(p, poly[i], poly[(i + 1) % n]):
            return True
    inside = False
    px, py = p
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > px:
                inside = not inside
    return inside


def point_polygon_distance(p: XY, poly: Sequence[XY]) -> float:
    """点到填充多边形的距离：内部/边界为 0。"""
    if point_in_polygon(p, poly):
        return 0.0
    n = len(poly)
    return min(
        point_segment_distance(p, poly[i], poly[(i + 1) % n])
        for i in range(n)
    )


# ---------- 多边形校验 ----------

class GeometryError(ValueError):
    """非法几何。"""


def normalize_polygon(raw: Iterable) -> list[XY]:
    """把外部输入规范化为 [(x, y), ...]，并校验为合法简单多边形。"""
    try:
        pts = [(float(p[0]), float(p[1])) for p in raw]
    except (TypeError, ValueError, IndexError, KeyError):
        raise GeometryError("多边形顶点必须为 [[x, y], ...] 形式的二维数值数组")

    if any(not math.isfinite(x) or not math.isfinite(y) for x, y in pts):
        raise GeometryError("多边形坐标必须为有限数值")

    # 去掉与首点重复的闭合点
    while len(pts) > 1 and dist(pts[0], pts[-1]) <= EPS:
        pts.pop()
    # 去掉连续重复点
    cleaned: list[XY] = []
    for p in pts:
        if not cleaned or dist(cleaned[-1], p) > EPS:
            cleaned.append(p)
    if len(cleaned) >= 2 and dist(cleaned[0], cleaned[-1]) <= EPS:
        cleaned.pop()
    if len(cleaned) < 3:
        raise GeometryError("多边形至少需要 3 个互不重合的顶点")

    # 面积阈值按多边形尺度相对判定（单位 mm²），避免放过极小退化三角形
    xs = [p[0] for p in cleaned]
    ys = [p[1] for p in cleaned]
    scale = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    if abs(signed_area(cleaned)) <= 1e-9 * scale * scale:
        raise GeometryError("多边形面积近似为零，属非法几何")

    n = len(cleaned)
    for i in range(n):
        a, b = cleaned[i], cleaned[(i + 1) % n]
        if dist(a, b) <= EPS:
            raise GeometryError("多边形存在零长度边")
        for j in range(i + 2, n):
            # 相邻首尾边（i=0, j=n-1）共享顶点，不算相交
            if i == 0 and j == n - 1:
                continue
            c, d = cleaned[j], cleaned[(j + 1) % n]
            if segments_intersect_proper(a, b, c, d):
                raise GeometryError("多边形存在自相交边，属非法简单多边形")
            # 共享顶点外的顶点落在对方边上（自接触）也拒绝
            for p, q, r in ((a, c, d), (b, c, d), (c, a, b), (d, a, b)):
                if not (dist(p, q) <= EPS or dist(p, r) <= EPS) and on_segment(p, q, r):
                    raise GeometryError("多边形边自接触，属非法简单多边形")
    return cleaned


def validate_z_range(z_min: float, z_max: float) -> None:
    if not (math.isfinite(z_min) and math.isfinite(z_max)):
        raise GeometryError("Z 高度范围必须为有限数值")
    if z_min > z_max:
        raise GeometryError("z_min 不得大于 z_max")


# ---------- 圆弧工具 ----------

def angle_diff(a1: float, a2: float) -> float:
    """从角 a1 沿运动方向到角 a2 的有符号扫角（运动方向由调用方约定）。"""
    d = (a2 - a1) % (2.0 * math.pi)
    return d


def signed_sweep(start_angle: float, end_angle: float, clockwise: bool) -> float:
    """实际扫掠角：逆时针为正，顺时针为负。同起终点表示整圆（±2π）。"""
    if clockwise:
        sweep = -angle_diff(start_angle, end_angle)
    else:
        sweep = angle_diff(end_angle, start_angle)
    return sweep


def angle_on_subarc(theta: float, theta0: float, sweep: float) -> bool:
    """theta 是否属于从 theta0 出发、有符号扫角为 sweep 的子弧（含端点）。"""
    if sweep >= 0.0:
        d = (theta - theta0) % (2.0 * math.pi)
        return d <= sweep + EPS
    d = (theta0 - theta) % (2.0 * math.pi)
    return d <= -sweep + EPS


def arc_segment_distance(center: XY, radius: float, theta0: float, sweep: float,
                         a: XY, b: XY) -> float:
    """圆弧（含整圆）与线段 ab 之间的最小距离。

    候选包括：
    1) 弧与线段相交（距离 0）；
    2) 弧端点到线段的垂足距离；
    3) 圆心到线段的垂足落在圆上且该点位于弧上（相切/近圆侧）；
    4) 线段端点到圆的径向最近点且位于弧上（线段端点在圆内时取 0）。
    """
    p0 = add(center, (radius * math.cos(theta0), radius * math.sin(theta0)))
    end = theta0 + sweep
    p1 = add(center, (radius * math.cos(end), radius * math.sin(end)))
    best = min(point_segment_distance(p0, a, b), point_segment_distance(p1, a, b))

    # 圆心到线段的垂足
    ab = sub(b, a)
    length2 = dot(ab, ab)
    if length2 > EPS * EPS:
        tc = dot(sub(center, a), ab) / length2
        tc_c = max(0.0, min(1.0, tc))
        foot = add(a, mul(ab, tc_c))
        if 0.0 - EPS <= tc <= 1.0 + EPS:
            v = sub(foot, center)
            nv = norm(v)
            if nv > EPS:
                q = add(center, mul(v, radius / nv))
                if angle_on_subarc(math.atan2(v[1], v[0]), theta0, sweep):
                    best = min(best, dist(q, foot))
        # 线段端点到圆最近点
        for ep in (a, b):
            v = sub(ep, center)
            d = norm(v)
            if d <= EPS:
                continue
            theta = math.atan2(v[1], v[0])
            if angle_on_subarc(theta, theta0, sweep):
                radial = abs(d - radius)
                best = min(best, radial)

    # 弧与线段是否相交（解二次方程）
    # |center - (a + t ab)|^2 = r^2, t in [0,1], 且交点角在弧上
    A = dot(ab, ab)
    if A > EPS * EPS:
        ac = sub(a, center)
        B = 2.0 * dot(ac, ab)
        C = dot(ac, ac) - radius * radius
        disc = B * B - 4.0 * A * C
        if disc >= -EPS:
            disc = max(0.0, disc)
            sq = math.sqrt(disc)
            for t in ((-B - sq) / (2.0 * A), (-B + sq) / (2.0 * A)):
                if -EPS <= t <= 1.0 + EPS:
                    q = add(a, mul(ab, max(0.0, min(1.0, t))))
                    theta = math.atan2(q[1] - center[1], q[0] - center[0])
                    if angle_on_subarc(theta, theta0, sweep):
                        return 0.0
    return best


def arc_segment_closest(center: XY, radius: float, theta0: float, sweep: float,
                        a: XY, b: XY) -> list[tuple[float, float]]:
    """圆弧（含整圆）与线段 ab 之间取得最小距离的候选点。

    返回 [(distance, theta), ...]：distance 为弧上角度 theta 处的点到线段的
    距离（theta 保证位于弧上，含端点）。距离平方极值只可能出现在：
    1) 弧端点到线段的垂足；
    2) 圆心到线段垂足方向的圆上点（该点位于弧上）；
    3) 线段端点径向方向的圆上点（该点位于弧上）；
    4) 弧与线段的交点（距离 0）。
    收集全部候选交由调用方按距离/进度比较，可保证不漏掉最小值。
    """
    cands: list[tuple[float, float]] = []

    def consider(d: float, theta: float) -> None:
        cands.append((max(0.0, d), theta))

    end = theta0 + sweep
    p0 = add(center, (radius * math.cos(theta0), radius * math.sin(theta0)))
    p1 = add(center, (radius * math.cos(end), radius * math.sin(end)))
    # 弧端点（整圆时 p1 与 p0 同点，调用方按进度 s 取较小者）
    consider(point_segment_distance(p0, a, b), theta0)
    consider(point_segment_distance(p1, a, b), end)

    ab = sub(b, a)
    length2 = dot(ab, ab)
    if length2 <= EPS * EPS:
        # 退化边（点）：径向最近点
        v = sub(a, center)
        nv = norm(v)
        if nv > EPS and angle_on_subarc(math.atan2(v[1], v[0]), theta0, sweep):
            consider(abs(nv - radius), math.atan2(v[1], v[0]))
        elif nv <= EPS:
            consider(radius, theta0)
        return cands

    # 圆心到线段的垂足
    tc = dot(sub(center, a), ab) / length2
    if -EPS <= tc <= 1.0 + EPS:
        foot = add(a, mul(ab, max(0.0, min(1.0, tc))))
        v = sub(foot, center)
        nv = norm(v)
        if nv > EPS:
            theta = math.atan2(v[1], v[0])
            if angle_on_subarc(theta, theta0, sweep):
                consider(abs(nv - radius), theta)
        else:
            consider(radius, theta0)

    # 线段端点到圆的径向最近点（端点在圆内时径向差为其到圆距离）
    for ep in (a, b):
        v = sub(ep, center)
        nv = norm(v)
        if nv <= EPS:
            consider(radius, theta0)
            continue
        theta = math.atan2(v[1], v[0])
        if angle_on_subarc(theta, theta0, sweep):
            consider(abs(nv - radius), theta)

    # 弧与线段相交 => 距离 0（解 |a + t ab - center|² = r²）
    A = dot(ab, ab)
    ac = sub(a, center)
    B = 2.0 * dot(ac, ab)
    C = dot(ac, ac) - radius * radius
    disc = B * B - 4.0 * A * C
    if disc >= -EPS:
        sq = math.sqrt(max(0.0, disc))
        for t in ((-B - sq) / (2.0 * A), (-B + sq) / (2.0 * A)):
            if -EPS <= t <= 1.0 + EPS:
                q = add(a, mul(ab, max(0.0, min(1.0, t))))
                w = sub(q, center)
                if norm(w) > EPS:
                    theta = math.atan2(w[1], w[0])
                    if angle_on_subarc(theta, theta0, sweep):
                        consider(0.0, theta)
    return cands


def arc_progress_of_angle(theta: float, theta0: float, sweep: float) -> float:
    """弧上角度 theta 对应的进度 s∈[0,1]（theta 必须位于弧上）。"""
    total = abs(sweep)
    if total <= EPS:
        return 0.0
    if sweep >= 0.0:
        d = (theta - theta0) % (2.0 * math.pi)
    else:
        d = (theta0 - theta) % (2.0 * math.pi)
    return max(0.0, min(1.0, d / total))


def arc_polygon_closest(center: XY, radius: float, theta0: float, sweep: float,
                        poly: Sequence[XY]) -> tuple[float, float]:
    """圆弧到填充多边形的最小距离及取得它的弧上进度 s∈[0,1]。

    距离 0（穿入内部/与边界接触，含数值接触容差）时，进度取弧上最早点；
    正距离情形由弧端点到多边形距离与各边弧-边候选的解析极值比较得到。
    """
    if subarc_hits(center, radius, theta0, sweep, poly, 0.0):
        return 0.0, earliest_arc_hit(center, radius, theta0, sweep, poly, 0.0)

    best_d = math.inf
    best_s = 1.0

    def track(d: float, s: float) -> None:
        nonlocal best_d, best_s
        if d < best_d - EPS or (abs(d - best_d) <= EPS and s < best_s):
            best_d, best_s = d, s

    # 弧端点到填充多边形
    for theta in (theta0, theta0 + sweep):
        p = add(center, (radius * math.cos(theta), radius * math.sin(theta)))
        track(point_polygon_distance(p, poly),
              arc_progress_of_angle(theta, theta0, sweep))

    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        for d, theta in arc_segment_closest(center, radius, theta0, sweep, a, b):
            track(d, arc_progress_of_angle(theta, theta0, sweep))
    return best_d, best_s


def subarc_hits(center: XY, radius: float, theta0: float, sweep: float,
                poly: Sequence[XY], tool_r: float) -> bool:
    """子弧是否与“多边形向外膨胀 tool_r 的区域”相交（含边界接触）。"""
    rr = tool_r + CONTACT_EPS
    n = len(poly)

    # 弧上代表点落在多边形内（含整圆覆盖多边形等情形：采样三点 + 边距离）
    for theta in (theta0, theta0 + sweep, theta0 + sweep * 0.5):
        p = add(center, (radius * math.cos(theta), radius * math.sin(theta)))
        if point_in_polygon(p, poly):
            return True

    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if arc_segment_distance(center, radius, theta0, sweep, a, b) <= rr:
            return True
    return False


def earliest_arc_hit(center: XY, radius: float, theta0: float, sweep: float,
                     poly: Sequence[XY], tool_r: float,
                     divisions: int = 256, iterations: int = 48) -> float:
    """在确认弧与多边形碰撞后，二分求最早碰撞角对应的进度 s∈[0,1]。"""
    total = abs(sweep)
    direction = 1.0 if sweep >= 0 else -1.0

    def hit_at(s_lo: float, s_hi: float) -> bool:
        sub_sweep = direction * (s_hi - s_lo) * total
        return subarc_hits(center, radius, theta0 + s_lo * sweep, sub_sweep,
                           poly, tool_r)

    # 1) 均匀细分，定位第一个“自身碰撞”的分片（此前各分片全部不碰撞）
    step = 1.0 / divisions
    k_first = divisions - 1
    for k in range(divisions):
        if hit_at(k * step, (k + 1) * step):
            k_first = k
            break
    lo, hi = k_first * step, min(1.0, (k_first + 1) * step)

    # 2) 在 [lo, hi] 内二分最早碰撞点：前缀 [lo, mid] 碰撞则收缩左界
    for _ in range(iterations):
        mid = (lo + hi) * 0.5
        if hit_at(lo, mid):
            hi = mid
        else:
            lo = mid
    return hi
