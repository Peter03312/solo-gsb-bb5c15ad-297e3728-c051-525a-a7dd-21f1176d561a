"""G-code 解析器。

支持的模态/指令：
- G0 / G1：快速定位 / 直线切削
- G2 / G3：XY 平面（G17）顺时针 / 逆时针圆弧插补，恒定 Z
- G17：XY 平面选择（默认）
- G20 / G21：英制英寸 / 公制毫米（默认 G21）；G20 下坐标自动换算为毫米
- G90 / G91：绝对坐标 / 增量坐标（默认 G90）

规则：
- 缺省轴沿用前值；I/J 始终相对弧起点，缺省视为 0；
- 圆弧只允许 XY 平面恒定 Z；I/J 同时为 0 或半径不一致等非法圆弧一律拒绝；
- 圆弧同起终点且 I/J 非零 => 整圆；
- T 刀号默认解析后忽略（单刀模式，与旧版一致）；当调用方传入
  tool_radius_map（刀号 -> 毫米半径）时启用多刀模式：
  * Tn 必须为非负整数，同一行至多一个 T，且 n 必须在映射中，否则拒绝；
  * Tn 在其所在行先于运动生效（半径作用于本行运动）；每次 T 切换
    （含与运动同行）都生成一次静止换刀事件，位置为切换时（本行运动前）
    刀位点，执行顺序先于本行运动：先按新半径检查换刀点，再检查运动；
  * 刀与半径模态延续到下次切换；首个 T 之前的运动沿用初始刀（刀号为空）。
- 除上述以外的任何运动/平面/坐标系指令（G18/G19/G54/G55/...、
  固定循环 G81 等、R 圆弧等）明确拒绝。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .geometry import EPS, GeometryError

INCH_TO_MM = 25.4

# 支持的 G 代码白名单
G_RAPID, G_LINE, G_CW, G_CCW = 0, 1, 2, 3
G_PLANE_XY = 17
G_UNIT_INCH, G_UNIT_MM = 20, 21
G_ABS, G_INCR = 90, 91
ALLOWED_G = {G_RAPID, G_LINE, G_CW, G_CCW, G_PLANE_XY,
             G_UNIT_INCH, G_UNIT_MM, G_ABS, G_INCR}
MOTION_G = {G_RAPID, G_LINE, G_CW, G_CCW}

# 允许出现的地址字（F/S/N/M 仅解析不参与运动；R 明确拒绝）
WORD_RE = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+\.?\d*|\.\d+))")
# 数值必须是合法有限数；禁止表达式
NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")


class GCodeError(ValueError):
    """G-code 非法或不受支持。携带原行号。"""

    def __init__(self, message: str, line_no: int | None = None):
        self.line_no = line_no
        prefix = f"第 {line_no} 行: " if line_no is not None else ""
        super().__init__(prefix + message)


@dataclass
class Motion:
    """一段连续运动（毫米单位），start/end 为刀位点（刀尖）XY/Z。"""
    kind: str               # "line" / "arc"
    line_no: int
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    # arc 专用：圆心（XY）、半径、起始角、有符号扫角（逆正顺负）
    center: tuple[float, float] | None = None
    radius: float | None = None
    theta0: float | None = None
    sweep: float | None = None
    # 多刀模式（tool_radius_map 启用）时固化本段生效刀具：
    # 首次 T 切换之前为初始刀，tool_no 为 None，tool_radius 为初始半径
    tool_no: int | None = None
    tool_radius: float = 0.0
    seq: int = 0            # 程序事件执行顺序（1 起，与静止换刀事件共用同一序号轴）


@dataclass(frozen=True)
class ToolChange:
    """T 切换时的静止换刀事件：刀位不运动，但刀具半径切换。

    多刀模式下每次 T 切换（含与运动同行）都生成：事件位置为切换时
    （本行运动前）刀位点，执行顺序先于本行运动，检查时先按新半径
    检查该静止刀位，再检查本行运动。
    """
    line_no: int
    pos: tuple[float, float, float]
    tool_no: int
    tool_radius: float
    seq: int


@dataclass
class ParseResult:
    motions: list[Motion] = field(default_factory=list)
    line_count: int = 0
    nonblank_count: int = 0
    final_position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # 多刀模式下的静止换刀事件（每次 T 切换，含与运动同行），按程序顺序排列
    tool_events: list[ToolChange] = field(default_factory=list)


def _strip_comment(line: str, line_no: int) -> str:
    """剥离分号注释与圆括号注释；括号不平衡则拒绝。"""
    depth = 0
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == ';':
            break
        if ch == '(':
            depth += 1
            i += 1
            continue
        if ch == ')':
            depth -= 1
            if depth < 0:
                raise GCodeError("括号注释不平衡（多余的 ')'）", line_no)
            i += 1
            continue
        if depth == 0:
            out.append(ch)
        i += 1
    if depth != 0:
        raise GCodeError("括号注释不平衡（缺少 ')'）", line_no)
    return ''.join(out)


def _tokenize(line: str, line_no: int) -> list[tuple[str, float]]:
    stripped = line.strip()
    if not stripped:
        return []
    if stripped == '%' or stripped.startswith('%') and stripped.rstrip('%').strip() == '':
        return []  # 程序起止标记
    tokens: list[tuple[str, float]] = []
    pos = 0
    s = stripped
    while pos < len(s):
        if s[pos].isspace():
            pos += 1
            continue
        m = WORD_RE.match(s, pos)
        if not m or m.start() != pos:
            raise GCodeError(f"无法识别的记号: {s[pos:pos + 8]!r}", line_no)
        letter = m.group(1).upper()
        num_text = m.group(2)
        if not NUMBER_RE.match(num_text):
            raise GCodeError(f"地址字 {letter} 的数值非法", line_no)
        value = float(num_text)
        if not math.isfinite(value):
            raise GCodeError(f"地址字 {letter} 的数值必须有限", line_no)
        tokens.append((letter, value))
        pos = m.end()
    return tokens


def parse_gcode(text: str,
                start_xyz: tuple[float, float, float],
                tool_radius_map: dict[int, float] | None = None,
                initial_tool_radius: float = 0.0) -> ParseResult:
    """解析 G-code 文本，返回按程序顺序排列的连续运动序列。

    start_xyz：毫米制初始刀尖坐标（绝对，后续 G90/G91 从该点开始模态延续）。

    tool_radius_map 为 None 时为单刀模式：T 字解析后忽略，与旧版一致。
    传入映射（可为空映射）时启用多刀模式：T 必须是非负整数、同行至多一个、
    刀号必须在映射中；Tn 先于本行运动生效；每次 T 切换（含与运动同行）
    都产出一次静止换刀事件（位置为本行运动前刀位，执行顺序先于本行运动）。
    initial_tool_radius 为初始刀位（首个 T 之前）的半径（mm）。
    """
    if not isinstance(text, str):
        raise GCodeError("gcode 必须为文本")

    multi_tool = tool_radius_map is not None
    if multi_tool:
        for _tno, _r in tool_radius_map.items():
            if not isinstance(_tno, int) or isinstance(_tno, bool) or _tno < 0:
                raise GCodeError("刀号必须为非负整数")
            if not isinstance(_r, (int, float)) or isinstance(_r, bool) \
                    or not math.isfinite(float(_r)) or _r < 0:
                raise GCodeError(f"刀号 {_tno} 的半径必须为非负有限毫米数")

    result = ParseResult(final_position=start_xyz)

    # 模态状态
    plane = G_PLANE_XY
    unit_is_inch = False
    absolute = True
    motion_modal: int | None = None

    x, y, z = start_xyz
    # 当前刀具（初始刀刀号为空）；多刀模式下随 T 字切换
    cur_tool_no: int | None = None
    cur_tool_radius = float(initial_tool_radius)
    event_seq = 0  # 程序事件序号（运动段与静止换刀事件共用，按解析顺序递增）
    lines = text.splitlines()
    result.line_count = len(lines)

    for idx, raw_line in enumerate(lines, start=1):
        body = _strip_comment(raw_line, idx)
        tokens = _tokenize(body, idx)
        if not tokens:
            continue
        result.nonblank_count += 1

        # 拆分 G 字与轴字；同一地址字重复明确拒绝
        g_codes: list[int] = []
        axes: dict[str, float] = {}
        t_codes: list[float] = []
        for letter, value in tokens:
            if letter == 'G':
                if value != int(value):
                    raise GCodeError(f"不支持的 G 代码 G{value:g}（仅支持整数）", idx)
                g = int(value)
                if g not in ALLOWED_G:
                    if g in (18, 19):
                        raise GCodeError("不支持的平面选择（仅支持 G17 XY 平面）", idx)
                    if 52 <= g <= 59 or g in (52.0,):
                        raise GCodeError("不支持的坐标系/工件偏移指令", idx)
                    raise GCodeError(f"不支持的 G 代码 G{g}（仅支持 G0/G1/G2/G3/G17/G20/G21/G90/G91）", idx)
                g_codes.append(g)
            elif letter in ("X", "Y", "Z", "I", "J"):
                if letter in axes:
                    raise GCodeError(f"同一行重复出现地址字 {letter}", idx)
                axes[letter] = value
            elif letter == 'R':
                raise GCodeError("不支持 R 形式圆弧（仅支持 I/J 圆心格式）", idx)
            elif letter == 'T':
                t_codes.append(value)
            elif letter in ("F", "S", "N", "M", "P", "L", "H", "D"):
                # 进给/转速/行号/辅助功能/补偿号等不参与几何运动，忽略
                pass
            else:
                raise GCodeError(f"不支持的地址字 {letter}", idx)

        # 多刀模式：T 必须为非负整数、同行至多一个、刀号已映射；
        # 未启用映射时 T 与旧版一样完全忽略
        if t_codes and multi_tool:
            if len(t_codes) > 1:
                raise GCodeError("同一行出现多个 T 刀号", idx)
            tv = t_codes[0]
            if tv != int(tv) or tv < 0:
                raise GCodeError(
                    f"刀号 T{tv:g} 非法（仅支持非负整数刀号）", idx)
            tno = int(tv)
            if tno not in tool_radius_map:
                raise GCodeError(f"刀号 T{tno} 未在 tool_radii 映射中", idx)
            cur_tool_no = tno
            cur_tool_radius = float(tool_radius_map[tno])
            # 每次 T 切换（含与运动同行）都记录一次静止换刀事件：
            # 位置为本行运动前刀位，执行顺序先于本行运动，
            # 检查时先按新半径检查该换刀点，再检查本行运动
            event_seq += 1
            result.tool_events.append(ToolChange(
                idx, (x, y, z), cur_tool_no, cur_tool_radius, event_seq))

        # 先处理模态 G 字（平面/单位/绝对增量），再确定本行运动
        line_motion: int | None = None
        for g in g_codes:
            if g == G_PLANE_XY:
                plane = G_PLANE_XY
            elif g == G_UNIT_INCH:
                unit_is_inch = True
            elif g == G_UNIT_MM:
                unit_is_inch = False
            elif g == G_ABS:
                absolute = True
            elif g == G_INCR:
                absolute = False
            elif g in MOTION_G:
                if line_motion is not None:
                    raise GCodeError("同一行出现多个运动指令", idx)
                line_motion = g

        if line_motion is not None:
            motion_modal = line_motion

        # 有轴字但没有任何运动模态：按 G0 处理（常见开机首行定位）
        effective_motion = motion_modal
        if effective_motion is None and axes:
            effective_motion = G_RAPID

        if effective_motion is None:
            continue  # 纯模态/辅助行，无几何运动（T 切换事件已在上面记录）

        scale = INCH_TO_MM if unit_is_inch else 1.0

        def resolve(letter: str, cur: float) -> float:
            if letter not in axes:
                return cur
            v = axes[letter] * scale
            return cur + v if not absolute else v
        # absolute 为 True 时使用绝对坐标值 v；G91 增量时叠加到当前值

        nx, ny, nz = resolve('X', x), resolve('Y', y), resolve('Z', z)
        start = (x, y, z)
        end = (nx, ny, nz)

        if effective_motion in (G_RAPID, G_LINE):
            # I/J 不允许搭配直线运动
            if 'I' in axes or 'J' in axes:
                raise GCodeError("I/J 只能用于 G2/G3 圆弧", idx)
            if any(not math.isfinite(v) for v in end):
                raise GCodeError("运动坐标必须为有限数值", idx)
            if (abs(nx - x) > EPS or abs(ny - y) > EPS
                    or abs(nz - z) > EPS):
                event_seq += 1
                result.motions.append(
                    Motion("line", idx, start, end,
                           tool_no=cur_tool_no, tool_radius=cur_tool_radius,
                           seq=event_seq))
        else:
            if plane != G_PLANE_XY:
                raise GCodeError("圆弧仅支持 G17 XY 平面", idx)
            # 圆弧必须恒定 Z：显式 Z 必须等于当前 Z（绝对）或增量 0
            if 'Z' in axes:
                z_target = resolve('Z', z)
                if abs(z_target - z) > EPS:
                    raise GCodeError("圆弧必须在恒定 Z 高度平面内（G2/G3 不允许 Z 变化）", idx)
            i = axes.get('I', 0.0) * scale
            j = axes.get('J', 0.0) * scale
            if abs(i) <= EPS and abs(j) <= EPS:
                raise GCodeError("圆弧 I/J 同时为 0，无法确定圆心", idx)
            if abs(nz - z) > EPS:
                raise GCodeError("圆弧必须在恒定 Z 高度平面内", idx)

            center = (x + i, y + j)
            r_start = math.hypot(x - center[0], y - center[1])
            r_end = math.hypot(nx - center[0], ny - center[1])
            if r_start <= EPS:
                raise GCodeError("弧起点与圆心重合，非法圆弧", idx)
            if abs(r_start - r_end) > max(1e-6, 1e-6 * r_start):
                raise GCodeError(
                    f"弧起点半径 {r_start:.6f} 与终点半径 {r_end:.6f} 不一致，非法圆弧", idx)

            theta0 = math.atan2(y - center[1], x - center[0])
            theta1 = math.atan2(ny - center[1], nx - center[0])
            clockwise = effective_motion == G_CW
            from .geometry import angle_diff
            if clockwise:
                sweep = -angle_diff(theta0, theta1)
            else:
                sweep = angle_diff(theta1, theta0)

            # 同起终点 + I/J 非零 => 整圆，扫角 ±2π
            if abs(nx - x) <= EPS and abs(ny - y) <= EPS:
                sweep = -2.0 * math.pi if clockwise else 2.0 * math.pi

            if abs(sweep) < EPS:
                raise GCodeError("圆弧扫掠角为零，非法圆弧", idx)

            event_seq += 1
            result.motions.append(
                Motion("arc", idx, start, end,
                       center=center, radius=r_start,
                       theta0=theta0, sweep=sweep,
                       tool_no=cur_tool_no, tool_radius=cur_tool_radius,
                       seq=event_seq))

        x, y, z = nx, ny, nz

    result.final_position = (x, y, z)
    return result
