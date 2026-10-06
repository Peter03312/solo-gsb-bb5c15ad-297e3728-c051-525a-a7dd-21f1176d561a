"""端到端与单元测试。使用 SQLite 覆盖数据库，无需 PostgreSQL。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import gcode as gc  # noqa: E402
from app import models  # noqa: E402
from app.collision import (Zone, build_execution_trace, check_path,
                           check_program, initial_collision,
                           minimum_clearance,
                           minimum_clearance_program)  # noqa: E402
from app.geometry import GeometryError, normalize_polygon  # noqa: E402
from app.main import app  # noqa: E402
from app.database import Base, engine  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(app)

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
SAFE = (-50.0, -50.0, 0.0)


def parse(text, start=SAFE):
    return gc.parse_gcode(text, start)


class GeometryTests(unittest.TestCase):
    def test_bad_polygons(self):
        with self.assertRaises(GeometryError):
            normalize_polygon([[0, 0], [1, 1]])
        with self.assertRaises(GeometryError):
            normalize_polygon([[0, 0], [1, 0], [0.5, 0]])  # 零面积
        with self.assertRaises(GeometryError):  # 蝴蝶结自相交
            normalize_polygon([[0, 0], [10, 10], [10, 0], [0, 10]])

    def test_closing_duplicate_point_accepted(self):
        poly = normalize_polygon([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        self.assertEqual(len(poly), 4)


class LineCollisionTests(unittest.TestCase):
    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def test_mid_segment_hit_not_endpoints(self):
        # 两端都在方块外侧，但路径横穿方块内部（仅采样端点会漏检）
        motions = parse("G90 G21 G17\nG1 X-5 Y5\nG1 X15 Y5\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 3)

    def test_pass_above_zone(self):
        motions = parse("G1 X-5 Y20\nG1 X15 Y20\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.5))

    def test_tool_radius_touch_at_distance(self):
        # 路径距边恰为 1mm，刀半径 1 => 边界接触算碰撞
        motions = parse("G1 X-5 Y11\nG1 X15 Y11\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.99))
        hit = check_path(motions, [self.zone], 1.0)
        self.assertIsNotNone(hit)

    def test_z_out_of_range_no_hit(self):
        motions = parse("G1 X-5 Y5 Z10\nG1 X15 Y5 Z10\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.0))
        # 斜线 Z 从范围外穿入范围内：途中碰撞
        motions2 = parse("G1 X-5 Y5 Z20\nG1 X15 Y5 Z0\n").motions
        hit = check_path(motions2, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_diagonal_through_corner(self):
        motions = parse("G1 X-2 Y-2\nG1 X12 Y12\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 2)

    def test_grazing_tangent_contact(self):
        # 与边重合贴边（从边外沿 X 轴进入）
        motions = parse("G1 X-5 Y0\nG1 X10 Y0\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_line_completely_inside(self):
        motions = gc.parse_gcode("G1 X2 Y2\nG1 X8 Y8\n",
                                 (2.0, 2.0, 0.0)).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)


class ArcCollisionTests(unittest.TestCase):
    def setUp(self):
        # 弧：圆心 (0,0) 半径 10，从 (10,0) 逆时针整圆；禁区跨在圆顶 (0,10)
        self.zone = Zone(7, normalize_polygon(
            [[-1, 9], [1, 9], [1, 11], [-1, 11]]), -5.0, 5.0)

    def test_full_circle_hits_mid_arc(self):
        text = "G90 G17 G21\nG1 X10 Y0 Z0\nG3 X10 Y0 I-10 J0\n"
        motions = gc.parse_gcode(text, SAFE).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 3)
        self.assertEqual(hit.zone_no, 7)
        # 首个碰撞约在 1/4 圆处（进度 ~0.25）
        self.assertAlmostEqual(hit.progress, 0.25, delta=0.02)

    def test_clockwise_full_circle(self):
        text = "G1 X10 Y0\nG2 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_arc_misses(self):
        # 禁区在圆外 (0,20) 附近
        zone = Zone(1, normalize_polygon([[19, -1], [21, -1], [21, 1], [19, 1]]),
                    -5, 5)
        text = "G1 X10 Y0\nG3 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        self.assertIsNone(check_path(motions, [zone], 0.0))

    def test_arc_contact_with_radius(self):
        # 弧半径 10，禁区边在半径 10.5 处：刀半径 0.5 恰好接触
        zone = Zone(1, normalize_polygon(
            [[-1, 10.5], [1, 10.5], [1, 12], [-1, 12]]), -5, 5)
        text = "G1 X10 Y0\nG3 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        self.assertIsNone(check_path(motions, [zone], 0.4))
        self.assertIsNotNone(check_path(motions, [zone], 0.5))

    def test_semicircle_inside_zone(self):
        # 整个半圆都在大禁区内
        zone = Zone(1, normalize_polygon(
            [[-15, -15], [15, -15], [15, 15], [-15, 15]]), -5, 5)
        text = "G90\nG1 X10 Y0 Z50\nG1 Z0\nG3 X-10 Y0 I-10 J0\n"
        motions = gc.parse_gcode(text, (10.0, 0.0, 50.0)).motions
        hit = check_path(motions, [zone], 0.0)
        self.assertIsNotNone(hit)
        # 第 3 行 Z 向下降进入高度范围时已位于禁区内
        self.assertEqual(hit.line_no, 3)


class ParserModalTests(unittest.TestCase):
    def test_incremental_and_unit(self):
        # G20 英寸：X 走 1 英寸 = 25.4mm；G91 增量
        r = gc.parse_gcode("G20 G91\nG1 X1 Y0\n", (0, 0, 0))
        self.assertAlmostEqual(r.final_position[0], 25.4, places=6)

    def test_missing_axis_inherits(self):
        r = gc.parse_gcode("G90\nG1 X5 Y7 Z3\nG1 X8\n", (0, 0, 0))
        self.assertEqual(r.final_position, (8.0, 7.0, 3.0))

    def test_arc_z_change_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0 Z0\nG3 X0 Y10 Z1 I-10 J0\n", (0, 0, 0))

    def test_arc_zero_ij_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0\nG3 X0 Y10\n", (0, 0, 0))

    def test_arc_radius_mismatch_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0\nG3 X0 Y9 I-10 J0\n", (0, 0, 0))

    def test_unsupported_g_commands(self):
        for bad in ("G54 G1 X1", "G18 G1 X1", "G81 X1 Y1 Z-1", "G28"):
            with self.assertRaises(gc.GCodeError):
                gc.parse_gcode(bad, (0, 0, 0))

    def test_r_arc_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X0 Y0\nG2 X10 Y0 R5\n", (0, 0, 0))

    def test_comments_and_line_numbers(self):
        text = "G90 ; start\n(hole path)\nN10 G1 X5 Y5\n"
        r = gc.parse_gcode(text, (0, 0, 0))
        # 第 1 行 G90 是有效非空行；第 2 行纯注释；第 3 行运动
        self.assertEqual(r.nonblank_count, 2)
        self.assertEqual(r.motions[0].line_no, 3)

    def test_unbalanced_paren_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X1 (oops\n", (0, 0, 0))

    def test_arc_is_constant_z_incremental_z0(self):
        r = gc.parse_gcode("G90\nG1 X10 Y0 Z2\nG91\nG3 X0 Y0 I-10 J0\n",
                           (0, 0, 0))
        self.assertEqual(len(r.motions), 2)
        self.assertAlmostEqual(abs(r.motions[1].sweep), 6.2831853, places=5)

    def test_ij_relative_to_start(self):
        r = gc.parse_gcode("G1 X10 Y0\nG3 X0 Y10 I-10 J0\n", (0, 0, 0))
        arc = r.motions[1]
        self.assertAlmostEqual(arc.center[0], 0.0)
        self.assertAlmostEqual(arc.center[1], 0.0)


class InitialPointTests(unittest.TestCase):
    def test_initial_inside(self):
        zone = Zone(1, normalize_polygon(SQUARE), -5, 5)
        self.assertEqual(initial_collision((5, 5, 0), [zone], 0.0), 1)
        self.assertIsNone(initial_collision((5, 5, 9), [zone], 0.0))


class ApiTests(unittest.TestCase):
    def test_01_full_flow(self):
        # 登记禁区
        r = client.post("/zones", json={
            "zone_no": 100, "polygon": SQUARE, "z_min": -5, "z_max": 5,
            "description": "基座"})
        self.assertEqual(r.status_code, 201, r.text)

        # 非法几何拒绝
        r = client.post("/zones", json={
            "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
            "z_min": -5, "z_max": 5})
        self.assertEqual(r.status_code, 422)

        # 横穿禁区 => 碰撞
        gcode = "G90 G21 G17\nG1 X-5 Y5 Z0\nG1 X15 Y5 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0, "gcode": gcode})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["has_collision"])
        self.assertEqual(body["collision_line_no"], 3)
        self.assertEqual(body["collision_zone_no"], 100)

        # 无碰撞刀路
        gcode_ok = "G1 X-5 Y50 Z0\nG1 X15 Y50 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [-5, 50, 0], "tool_radius": 0.0, "gcode": gcode_ok})
        body = r.json()
        self.assertFalse(body["has_collision"])
        self.assertIsNone(body["collision_line_no"])

        # 非法 gcode 拒绝，且不留报告
        existing = client.get("/zones/100").json()
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [0, 0, 0], "tool_radius": 0.0,
            "gcode": "G54 G1 X1\n"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(client.get("/zones/100").json(), existing)

        # 不存在的禁区/版本
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 999, "version": 1}],
            "start": [0, 0, 0], "tool_radius": 0.0, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 404)
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 9}],
            "start": [0, 0, 0], "tool_radius": 0.0, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 404)

        # 刀具半径非法
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [0, 0, 0], "tool_radius": -1, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 422)

    def test_02_versions_and_immutable_report(self):
        r = client.post("/zones", json={
            "polygon": SQUARE, "z_min": -5, "z_max": 5})
        no = r.json()["zone_no"]
        r = client.post("/reviews", json={
            "zones": [{"zone_no": no, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"})
        self.assertTrue(r.json()["has_collision"])
        report_v1 = r.json()["report_id"]

        # 修改 => 不可变新版本（方块平移到 (100,100) 附近）
        far = [[100, 100], [110, 100], [110, 110], [100, 110]]
        r = client.post(f"/zones/{no}/versions",
                        json={"polygon": far, "z_min": -5, "z_max": 5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["version"], 2)

        # 旧报告不随禁区更新改变
        old = client.get(f"/reports/{report_v1}").json()
        self.assertEqual(old["used_zone_versions"], {str(no): 1})
        self.assertTrue(old["has_collision"])

        # 同一刀路按 v2 审查 => 无碰撞
        r = client.post("/reviews", json={
            "zones": [{"zone_no": no, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"})
        self.assertFalse(r.json()["has_collision"])

        detail = client.get(f"/zones/{no}").json()
        self.assertEqual([v["version"] for v in detail["versions"]], [1, 2])

    def test_03_multiple_zones_first_one_wins(self):
        client.post("/zones", json={"zone_no": 200, "polygon": SQUARE,
                                   "z_min": -5, "z_max": 5})
        poly2 = [[11, 0], [20, 0], [20, 10], [11, 10]]
        client.post("/zones", json={"zone_no": 300, "polygon": poly2,
                                   "z_min": -5, "z_max": 5})
        gcode = "G1 X30 Y5 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 300, "version": 1},
                      {"zone_no": 200, "version": 1}],
            "start": [-10, 5, 0], "tool_radius": 0.0, "gcode": gcode})
        body = r.json()
        self.assertTrue(body["has_collision"])
        # 路径先进入 200 号禁区（x=10 边早于 300 的 x=11 边）
        self.assertEqual(body["collision_zone_no"], 200)


class ClearanceTests(unittest.TestCase):
    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def _clr(self, text, start, r=0.0, zones=None):
        motions = gc.parse_gcode(text, start).motions
        return minimum_clearance(motions, zones or [self.zone], r, start)

    def test_line_outside_positive(self):
        # y=11 与边距离 1，刀半径 0.5 => 净间隙 0.5
        cp = self._clr("G1 X-5 Y11\nG1 X15 Y11\n", (-5.0, 11.0, 0.0), 0.5)
        self.assertAlmostEqual(cp.net, 0.5, places=9)
        self.assertEqual(cp.zone_no, 1)
        self.assertEqual(cp.line_no, 2)
        # 最近点为垂足段内任意点，按段内进度取最早（x=0）
        self.assertAlmostEqual(cp.point[0], 0.0, places=9)
        self.assertAlmostEqual(cp.point[1], 11.0, places=9)

    def test_line_penetration_negative(self):
        # 横穿方块：路径穿入填充多边形内部，距离 0 - 刀半径 => 净间隙 -0.5；
        # 第一个零距离点在进入边界 x=0 处
        cp = self._clr("G1 X-5 Y5\nG1 X15 Y5\n", (-5.0, 5.0, 0.0), 0.5)
        self.assertEqual(cp.net, -0.5)
        self.assertAlmostEqual(cp.point[0], 0.0, places=9)
        self.assertAlmostEqual(cp.point[1], 5.0, places=9)
        self.assertEqual(cp.line_no, 2)

    def test_line_inside_negative(self):
        # 起点已在多边形内部，唯一运动段全程侵入 => 净间隙为 -刀半径
        motions = gc.parse_gcode("G1 X8 Y8\n", (2.0, 2.0, 0.0)).motions
        cp = minimum_clearance(motions, [self.zone], 0.5, (2.0, 2.0, 0.0))
        self.assertAlmostEqual(cp.net, -0.5, places=9)
        self.assertEqual(cp.line_no, 1)
        self.assertEqual(tuple(round(v, 9) for v in cp.point), (2.0, 2.0, 0.0))

    def test_boundary_contact_zero(self):
        # 路径 y=11 与边 y=10 距离恰为刀半径 1：刀具外沿刚好接触边界 => 0
        cp = self._clr("G1 X-5 Y11\nG1 X15 Y11\n", (-5.0, 11.0, 0.0), 1.0)
        self.assertEqual(cp.net, 0.0)
        # 刀半径 0，路径本身贴边：距离 0 - 0 = 0
        cp0 = self._clr("G1 X-5 Y10\nG1 X15 Y10\n", (-5.0, 10.0, 0.0), 0.0)
        self.assertEqual(cp0.net, 0.0)
        # 刀半径 > 0 且路径落在填充多边形边界上：距离 0 - r 为负（侵入）
        cpneg = self._clr("G1 X-5 Y10\nG1 X15 Y10\n", (-5.0, 10.0, 0.0), 1.0)
        self.assertEqual(cpneg.net, -1.0)

    def test_closest_only_inside_z_window(self):
        # Z 从 10 降到 -10：t<=0.25 时 z>=5 不在窗口；窗口起点 t=0.25
        # 对应 x=0（边界，距离 0）——整体最小值为边界接触 0。
        cp = self._clr("G1 X-5 Y5 Z10\nG1 X15 Y5 Z-10\n",
                       (-5.0, 5.0, 10.0), 0.0)
        self.assertEqual(cp.net, 0.0)
        self.assertAlmostEqual(cp.point[0], 0.0, places=8)
        self.assertAlmostEqual(cp.point[2], 5.0, places=8)

    def test_positive_minimum_partial_window(self):
        # 窗口只覆盖路径中段 t∈[0.25,0.75]（z 从 10 降到 -10），y=15 在方块外：
        # 该段到方块的最近点 (10,10)，最小距离 5，在窗口进入时刻 t=0.25（x=0）取得
        cp = self._clr("G1 X-5 Y15 Z10\nG1 X15 Y15 Z-10\n",
                       (-5.0, 15.0, 10.0), 0.0)
        self.assertAlmostEqual(cp.net, 5.0, places=7)
        self.assertAlmostEqual(cp.point[0], 0.0, places=7)
        self.assertAlmostEqual(cp.point[1], 15.0, places=7)
        self.assertAlmostEqual(cp.point[2], 5.0, places=7)
        self.assertEqual(cp.line_no, 2)

    def test_all_outside_z_window_empty(self):
        cp = self._clr("G1 X-5 Y5 Z20\nG1 X15 Y5 Z20\n",
                       (-5.0, 5.0, 20.0), 0.0)
        self.assertIsNone(cp)

    def test_full_circle(self):
        # 圆心 (0,0) 半径 10 整圆；禁区为圆顶 2x2 方块，弧从右边 x=1 处进入
        poly = normalize_polygon([[-1, 9], [1, 9], [1, 11], [-1, 11]])
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.0,
                       zones=[Zone(7, poly, -5.0, 5.0)])
        self.assertEqual(cp.net, 0.0)
        self.assertEqual(cp.line_no, 2)
        self.assertEqual(cp.zone_no, 7)
        self.assertAlmostEqual(cp.progress, 0.23406, delta=1e-4)

    def test_arc_contact_radius_zero_clearance(self):
        poly = normalize_polygon([[-1, 10.5], [1, 10.5], [1, 12], [-1, 12]])
        # 刀半径 0.5 恰好接触 => 0；0.4 => 0.1
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.5,
                       zones=[Zone(1, poly, -5.0, 5.0)])
        self.assertEqual(cp.net, 0.0)
        self.assertAlmostEqual(cp.point[1], 10.0, places=8)
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.4,
                       zones=[Zone(1, poly, -5.0, 5.0)])
        self.assertAlmostEqual(cp.net, 0.1, places=8)

    def test_no_motions_uses_initial_position(self):
        cp = minimum_clearance([], [self.zone], 0.5, (5.0, 5.0, 0.0))
        self.assertIsNotNone(cp)
        self.assertAlmostEqual(cp.net, -0.5, places=9)
        self.assertIsNone(cp.line_no)
        self.assertEqual(cp.point, (5.0, 5.0, 0.0))
        # 初始点不在任何禁区高度 => 空
        self.assertIsNone(
            minimum_clearance([], [self.zone], 0.0, (5.0, 5.0, 9.0)))
        # 空程序（仅注释/标记）同样按初始刀位
        parsed = gc.parse_gcode("(only comment)\n%\n", (-5.0, 5.0, 0.0))
        cp = minimum_clearance(parsed.motions, [self.zone], 0.0, (-5.0, 5.0, 0.0))
        self.assertAlmostEqual(cp.net, 5.0, places=9)

    def test_tie_break_order_then_progress_then_zone(self):
        # 两条等距运动：取顺序更早的段
        z1 = Zone(1, normalize_polygon(
            [[10, -1], [12, -1], [12, 1], [10, 1]]), -5.0, 5.0)
        motions = gc.parse_gcode(
            "G1 X0 Y0\nG1 X11 Y0\nG1 X0 Y0\nG1 X11 Y0\n", (0.0, 0.0, 0.0)).motions
        cp = minimum_clearance(motions, [z1], 0.0, (0.0, 0.0, 0.0))
        self.assertEqual(cp.line_no, 2)  # 第一条到达 y=0 附近的运动
        # 同段同进度、两个等距禁区 => 小编号
        za = Zone(2, normalize_polygon(
            [[10, 0], [12, 0], [12, 2], [10, 2]]), -5.0, 5.0)
        zb = Zone(3, normalize_polygon(
            [[10, -2], [12, -2], [12, 0], [10, 0]]), -5.0, 5.0)
        motions = gc.parse_gcode("G1 X11 Y0\n", (0.0, 0.0, 0.0)).motions
        cp = minimum_clearance(motions, [za, zb], 0.0, (0.0, 0.0, 0.0))
        self.assertEqual(cp.zone_no, 2)

    def test_vertical_move_xy_static(self):
        # XY 不动（在禁区外 5mm），Z 从高空下降进入窗口：进入点 z=5
        cp = self._clr("G1 X-5 Y5 Z20\nG1 X-5 Y5 Z0\n",
                       (-5.0, 5.0, 20.0), 0.0)
        self.assertAlmostEqual(cp.net, 5.0, places=9)
        self.assertAlmostEqual(cp.point[2], 5.0, places=8)


class ClearanceApiTests(unittest.TestCase):
    ZONE_NO = 400

    def setUp(self):
        existing = client.get(f"/zones/{self.ZONE_NO}")
        if existing.status_code == 404:
            r = client.post("/zones", json={
                "zone_no": self.ZONE_NO, "polygon": SQUARE,
                "z_min": -5, "z_max": 5})
            self.assertEqual(r.status_code, 201, r.text)

    def _review(self, **kw):
        body = {
            "zones": [{"zone_no": 400, "version": 1}],
            "start": [-5.0, 5.0, 0.0], "tool_radius": 0.5,
            "gcode": "G1 X15 Y5 Z0\n"}
        body.update(kw)
        return client.post("/reviews", json=body)

    def test_opt_in_returns_and_persists(self):
        # y=11 与方块上边距离 1，刀半径 1 => 刀具外沿恰好接触，净间隙 0
        r = self._review(include_min_clearance=True, start=[-5.0, 11.0, 0.0],
                         tool_radius=1.0, gcode="G1 X15 Y11 Z0\n")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["min_clearance_enabled"])
        mc = b["min_clearance"]
        self.assertEqual(mc["min_clearance_mm"], 0.0)
        self.assertEqual(mc["line_no"], 1)
        self.assertEqual(mc["zone_no"], 400)
        self.assertAlmostEqual(mc["point_mm"][0], 0.0, places=8)
        self.assertAlmostEqual(mc["point_mm"][1], 11.0, places=8)
        # 接触即碰撞：首碰撞结论仍按既有规则，与最小点独立计算
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)
        # 报告查询一致（固化）
        rid = b["report_id"]
        got = client.get(f"/reports/{rid}").json()
        self.assertTrue(got["min_clearance_enabled"])
        self.assertEqual(got["min_clearance"], mc)

    def test_opt_in_negative_clearance_separate_collision(self):
        # 横穿禁区内部 => 最小净间隙为 -刀半径（负值），同时首碰撞规则不变
        r = self._review(include_min_clearance=True)
        b = r.json()
        self.assertAlmostEqual(b["min_clearance"]["min_clearance_mm"], -0.5,
                               places=9)
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)

    def test_opt_out_compatible(self):
        r = self._review()
        b = r.json()
        self.assertFalse(b["min_clearance_enabled"])
        self.assertIsNone(b["min_clearance"])
        rid = b["report_id"]
        got = client.get(f"/reports/{rid}").json()
        self.assertFalse(got["min_clearance_enabled"])
        self.assertIsNone(got["min_clearance"])

    def test_empty_when_never_in_z_range(self):
        r = self._review(start=[-5.0, 5.0, 20.0],
                         gcode="G1 X15 Y5 Z20\n",
                         include_min_clearance=True)
        b = r.json()
        self.assertTrue(b["min_clearance_enabled"])
        self.assertIsNone(b["min_clearance"])
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertIsNone(got["min_clearance"])

    def test_no_motion_initial_position(self):
        r = self._review(start=[5.0, 5.0, 0.0], gcode="(nothing here)\n",
                         include_min_clearance=True)
        b = r.json()
        self.assertEqual(b["input_summary"]["motion_count"], 0)
        mc = b["min_clearance"]
        self.assertAlmostEqual(mc["min_clearance_mm"], -0.5, places=9)
        self.assertIsNone(mc["line_no"])
        self.assertEqual(mc["point_mm"], [5.0, 5.0, 0.0])

    def test_invalid_input_no_report(self):
        # 先取一份成功报告作为“最后报告号”锚点
        ok = self._review(include_min_clearance=False)
        last_id = ok.json()["report_id"]
        # 非法刀半径/非法 G-code/非法开关类型 => 422；不存在禁区/版本 => 404
        bad422 = (
            {"tool_radius": -1.0, "include_min_clearance": True},
            {"gcode": "G54 G1 X1\n", "include_min_clearance": True},
            {"include_min_clearance": "yes"},
            {"include_min_clearance": 1},
            {"include_min_clearance": ["x"]},
            {"include_min_clearance": None},
        )
        for bad in bad422:
            r = self._review(**bad)
            self.assertEqual(r.status_code, 422, r.text)
        r = self._review(zones=[{"zone_no": 999, "version": 1}],
                         include_min_clearance=True)
        self.assertEqual(r.status_code, 404)
        # 全部拒绝后没有新报告：last_id 存在而下一号 404
        self.assertEqual(client.get(f"/reports/{last_id}").status_code, 200)
        self.assertEqual(client.get(f"/reports/{last_id + 1}").status_code, 404)

    def test_immutable_report_snapshot(self):
        r1 = self._review(include_min_clearance=True)
        rid = r1.json()["report_id"]
        mc1 = r1.json()["min_clearance"]
        # 禁区改版（平移），旧报告固化值不变
        client.post("/zones/400/versions", json={
            "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
            "z_min": -5, "z_max": 5})
        got = client.get(f"/reports/{rid}").json()
        self.assertEqual(got["min_clearance"], mc1)


class MultiToolParserTests(unittest.TestCase):
    START = (-5.0, 11.0, 0.0)

    def parse(self, text, tmap=None, r0=0.0):
        if tmap is None:
            return gc.parse_gcode(text, self.START)
        return gc.parse_gcode(text, self.START, tool_radius_map=tmap,
                              initial_tool_radius=r0)

    def test_legacy_t_ignored(self):
        r = self.parse("T5 M6\nG1 X3 Y0\n")
        self.assertEqual(len(r.motions), 1)
        self.assertEqual(r.tool_events, [])

    def test_mapping_empty_means_all_t_rejected(self):
        with self.assertRaises(gc.GCodeError):
            self.parse("T0\nG1 X1\n", tmap={})

    def test_tool_takes_effect_on_same_line_motion(self):
        r = self.parse("T2 G1 X15 Y11\n", tmap={2: 3.0}, r0=0.5)
        m = r.motions[0]
        self.assertEqual(m.tool_no, 2)
        self.assertEqual(m.tool_radius, 3.0)
        # 同行 T 与运动分别记录：换刀事件先于本行运动，位置为运动前刀位
        self.assertEqual(len(r.tool_events), 1)
        ev = r.tool_events[0]
        self.assertEqual(ev.line_no, 1)
        self.assertEqual(ev.tool_no, 2)
        self.assertEqual(ev.tool_radius, 3.0)
        self.assertEqual(ev.pos, self.START)
        self.assertLess(ev.seq, m.seq)
        # T 之前的运动沿用初始刀（刀号为空）
        r2 = self.parse("G1 X0 Y11\nT2\nG1 X15 Y11\n", tmap={2: 3.0}, r0=0.5)
        self.assertIsNone(r2.motions[0].tool_no)
        self.assertEqual(r2.motions[0].tool_radius, 0.5)
        self.assertEqual(r2.motions[1].tool_no, 2)

    def test_standalone_t_emits_static_event_with_new_radius(self):
        r = self.parse("T2\nG1 X15 Y11\n", tmap={2: 3.0}, r0=0.5)
        self.assertEqual(len(r.motions), 1)
        ev = r.tool_events[0]
        self.assertEqual(ev.line_no, 1)
        self.assertEqual(ev.tool_no, 2)
        self.assertEqual(ev.tool_radius, 3.0)
        self.assertEqual(ev.pos, self.START)
        self.assertEqual([ev.seq, r.motions[0].seq], [1, 2])

    def test_tool_modal_persists_until_next_change(self):
        r = self.parse("T1\nG1 X0 Y11\nG1 X5 Y11\nT2\nG1 X15 Y11\n",
                       tmap={1: 1.0, 2: 2.0}, r0=0.0)
        self.assertEqual([m.tool_no for m in r.motions], [1, 1, 2])
        self.assertEqual([m.tool_radius for m in r.motions], [1.0, 1.0, 2.0])

    def test_zero_tool_no_allowed(self):
        r = self.parse("T0 G1 X1 Y11\n", tmap={0: 1.0}, r0=0.0)
        self.assertEqual(r.motions[0].tool_no, 0)

    def test_invalid_t_rejected(self):
        cases = (
            "T1.5 G1 X1\n",   # 非整数
            "T-1 G1 X1\n",    # 负数
            "T9 G1 X1\n",     # 未映射刀号
            "T1 T2 G1 X1\n",  # 同行多个 T
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(gc.GCodeError):
                    self.parse(text, tmap={1: 1.0, 2: 1.0})

    def test_zero_length_t_line_becomes_static_event(self):
        # T + 轴字但终点与当前点重合：无运动段，按独立 T 行静止换刀
        r = self.parse("T2 X-5 Y11\n", tmap={2: 3.0}, r0=0.5)
        self.assertEqual(r.motions, [])
        self.assertEqual(len(r.tool_events), 1)
        self.assertEqual(r.tool_events[0].tool_no, 2)


class MultiToolEngineTests(unittest.TestCase):
    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def check(self, text, start=(-5.0, 11.0, 0.0), r0=0.0, tmap=None,
              zones=None):
        p = gc.parse_gcode(text, start, tool_radius_map=tmap,
                           initial_tool_radius=r0)
        return check_program(p.motions, p.tool_events, zones or [self.zone],
                             start, r0)

    def test_small_then_large_tool_collision_uses_event_radius(self):
        # y=11 距方块上边/角最近 1mm：初始 r=0 段只在 x<0 的安全区，
        # T1(r=2) 后横穿 => 碰撞，命中半径/刀号随事件固化
        c = self.check("G1 X-2 Y11\nT1\nG1 X15 Y11\n", tmap={1: 2.0})
        self.assertIsNotNone(c)
        self.assertEqual(c.line_no, 3)
        self.assertEqual(c.tool_no, 1)
        self.assertEqual(c.tool_radius, 2.0)

    def test_large_then_small_tool_avoids_collision(self):
        # 初始 r=2 的段停在远处安全区（(-5,50)），换 r=0.5 后沿 y=11 横穿：
        # 最近距离 1mm > 0.5 => 无碰撞
        c = self.check("G1 X-5 Y50\nT1\nG1 X15 Y11\n",
                       r0=2.0, tmap={1: 0.5})
        self.assertIsNone(c)

    def test_standalone_t_static_collision(self):
        # 静止点 (0,11) 距上边 1mm：换 r=2 的刀 => 静止即碰撞，行号为 T 行
        c = self.check("G1 X0 Y11\nT1\nG1 X15 Y11\n",
                       start=(-5.0, 11.0, 0.0), tmap={1: 2.0})
        self.assertIsNotNone(c)
        self.assertEqual(c.line_no, 2)
        self.assertEqual(c.tool_no, 1)
        self.assertEqual(c.tool_radius, 2.0)

    def test_initial_position_hit_has_null_tool_and_radius_initial(self):
        c = self.check("(nothing)\n", start=(5.0, 5.0, 0.0), r0=0.5,
                       tmap={1: 9.0})
        self.assertIsNotNone(c)
        self.assertIsNone(c.line_no)
        self.assertIsNone(c.tool_no)
        self.assertEqual(c.tool_radius, 0.5)

    def test_tie_execution_order_then_progress_then_zone(self):
        # 初始刀位 (0,11)：距 1 号禁区 1mm，r=1 恰好接触（seq=0）；
        # 后续独立 T 行 r=1 静止同样接触（seq=1）=> 取初始刀位（顺序更早）
        z2 = Zone(2, normalize_polygon([[0, -10], [10, -10], [10, 0], [0, 0]]),
                  -5.0, 5.0)
        c = self.check("T1\n", start=(0.0, 11.0, 0.0), r0=1.0,
                       tmap={1: 1.0}, zones=[self.zone, z2])
        self.assertIsNotNone(c)
        self.assertIsNone(c.line_no)
        self.assertIsNone(c.tool_no)
        # 同点同时被两禁区接触：取小编号
        z3 = Zone(3, normalize_polygon([[0, 11], [10, 11], [10, 12], [0, 12]]),
                  -5.0, 5.0)
        c2 = self.check("(x)\n", start=(0.0, 11.0, 0.0), r0=1.0,
                        tmap={1: 1.0}, zones=[z3, self.zone])
        self.assertEqual(c2.zone_no, 1)


class MultiToolClearanceEngineTests(unittest.TestCase):
    def setUp(self):
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def clr(self, text, start, r0, tmap):
        p = gc.parse_gcode(text, start, tool_radius_map=tmap,
                           initial_tool_radius=r0)
        return minimum_clearance_program(p.motions, p.tool_events,
                                         [self.zone], start, r0)

    def test_clearance_uses_radius_at_event(self):
        # 初始 r=2 段净间隙 1-2=-1；换 r=0.5 后同高度段净间隙 0.5 => 全局 -1
        cp = self.clr("G1 X0 Y11\nT1\nG1 X15 Y11\n",
                      (-5.0, 11.0, 0.0), 2.0, {1: 0.5})
        self.assertEqual(cp.net, -1.0)
        self.assertEqual(cp.line_no, 1)
        self.assertIsNone(cp.tool_no)          # 初始刀
        self.assertEqual(cp.tool_radius, 2.0)

    def test_clearance_static_tool_change_candidate(self):
        # 初始点远离禁区（净间隙大）；T 行静止点 (0,11) r=2 => -1
        cp = self.clr("G1 X0 Y11\nT1\nG1 X15 Y11\n",
                      (-5.0, 11.0, 0.0), 0.0, {1: 2.0})
        self.assertEqual(cp.net, -1.0)
        self.assertEqual(cp.line_no, 2)
        self.assertEqual(cp.tool_no, 1)
        self.assertEqual(cp.tool_radius, 2.0)
        self.assertEqual(cp.point, (0.0, 11.0, 0.0))

    def test_clearance_initial_position_null_line_and_tool(self):
        cp = self.clr("(only comment)\n", (5.0, 5.0, 0.0), 0.5, {1: 9.0})
        self.assertEqual(cp.net, -0.5)
        self.assertIsNone(cp.line_no)
        self.assertIsNone(cp.tool_no)
        self.assertEqual(cp.tool_radius, 0.5)


class MultiToolApiTests(unittest.TestCase):
    ZONE_NO = 600

    def setUp(self):
        if client.get(f"/zones/{self.ZONE_NO}").status_code == 404:
            r = client.post("/zones", json={
                "zone_no": self.ZONE_NO, "polygon": SQUARE,
                "z_min": -5, "z_max": 5})
            self.assertEqual(r.status_code, 201, r.text)

    def _review(self, **kw):
        body = {
            "zones": [{"zone_no": self.ZONE_NO, "version": 1}],
            "start": [-5.0, 11.0, 0.0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y11 Z0\n"}
        body.update(kw)
        return client.post("/reviews", json=body)

    def test_multi_tool_collision_freezes_line_tool_radius(self):
        # 中转点 (-2,11) 距方块角 (0,10) 为 sqrt(5)≈2.236mm：r=2 静止安全，
        # 随后 r=2 横穿 y=11（最近 1mm）途中碰撞 => 命中行 3、刀 2、半径 2
        r = self._review(
            gcode="G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n",
            tool_radii={"2": 2.0}, include_min_clearance=True)
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 3)
        self.assertEqual(b["collision_zone_no"], self.ZONE_NO)
        self.assertEqual(b["collision_tool_no"], 2)
        self.assertEqual(b["collision_tool_radius_mm"], 2.0)
        mc = b["min_clearance"]
        self.assertEqual(mc["tool_no"], 2)
        self.assertEqual(mc["tool_radius_mm"], 2.0)
        self.assertEqual(mc["min_clearance_mm"], -1.0)
        self.assertEqual(b["input_summary"]["tool_map_enabled"], True)
        self.assertEqual(b["input_summary"]["tool_radii_mm"], {"2": 2.0})
        # 固化：GET 一致
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertEqual(got["collision_tool_no"], 2)
        self.assertEqual(got["collision_tool_radius_mm"], 2.0)
        self.assertEqual(got["min_clearance"], mc)

    def test_initial_tool_hit_freezes_null_tool(self):
        r = self._review(start=[5.0, 5.0, 0.0], tool_radius=0.5,
                         gcode="(park)\n", tool_radii={"1": 9.0})
        b = r.json()
        self.assertTrue(b["has_collision"])
        self.assertIsNone(b["collision_line_no"])
        self.assertIsNone(b["collision_tool_no"])
        self.assertEqual(b["collision_tool_radius_mm"], 0.5)

    def test_standalone_t_static_hit_line_is_t_line(self):
        r = self._review(start=[0.0, 11.0, 0.0],
                         gcode="G1 X0 Y11\nT3\nG1 X5 Y11\n",
                         tool_radii={3: 2.0})
        b = r.json()
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 2)
        self.assertEqual(b["collision_tool_no"], 3)
        self.assertEqual(b["collision_tool_radius_mm"], 2.0)

    def test_small_tool_after_switch_no_collision(self):
        # 初始大刀段只在安全区抬到远处，换 r=0.5 后斜插到 y=11 横穿高度
        r = self._review(
            gcode="G1 X-5 Y50 Z0\nT1\nG1 X15 Y11 Z0\n",
            tool_radius=2.0, tool_radii={"1": 0.5})
        b = r.json()
        self.assertFalse(b["has_collision"], r.text)
        self.assertIsNone(b["collision_tool_no"])

    def test_invalid_tool_mapping_422(self):
        ok = self._review(tool_radii={"1": 1.0})
        last_id = ok.json()["report_id"]
        bad = (
            {"tool_radii": {"-1": 1.0}},        # 负刀号
            {"tool_radii": {"1.5": 1.0}},       # 非整数刀号
            {"tool_radii": {"abc": 1.0}},       # 非数字刀号
            {"tool_radii": {"1": -0.1}},        # 负半径
            {"tool_radii": {"1": "x"}},         # 非数值半径
            {"tool_radii": {"1": True}},        # 布尔半径
            {"tool_radii": "not-a-map"},        # 非映射对象
            {"tool_radii": [["1", 1.0]]},       # 非映射对象
        )
        for extra in bad:
            rr = self._review(**extra)
            self.assertEqual(rr.status_code, 422, (extra, rr.text))
        # 非法 T 字（未映射/非整数/多个 T）同样 422
        for gcode in ("T9\n", "T1.5\n", "T1 T2\n"):
            rr = self._review(gcode=gcode, tool_radii={"1": 1.0})
            self.assertEqual(rr.status_code, 422, (gcode, rr.text))
            self.assertEqual(rr.json()["detail"]["error"], "invalid_gcode")
        # 全部拒绝后没有新报告
        self.assertEqual(client.get(f"/reports/{last_id + 1}").status_code, 404)

    def test_empty_map_rejects_t_but_t_free_program_ok(self):
        r = self._review(gcode="G1 X15 Y50 Z0\n", tool_radii={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["input_summary"]["tool_map_enabled"])
        r2 = self._review(gcode="T0\n", tool_radii={})
        self.assertEqual(r2.status_code, 422)

    def test_legacy_without_map_t_ignored_and_fields_null(self):
        r = self._review(gcode="T7 M6\nG1 X15 Y11 Z0\n", tool_radius=0.5)
        b = r.json()
        self.assertEqual(b["input_summary"].get("tool_map_enabled"), None)
        self.assertIsNone(b["collision_tool_no"])
        self.assertIsNone(b["collision_tool_radius_mm"])
        # GET 同样兼容
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertIsNone(got["collision_tool_no"])
        self.assertIsNone(got["collision_tool_radius_mm"])


class ExecutionTraceParserTests(unittest.TestCase):
    """多刀模式解析：每次 T 切换（含与运动同行）都产生静止换刀事件。"""

    START = (-5.0, 11.0, 0.0)

    def parse(self, text, tmap, r0=0.0):
        return gc.parse_gcode(text, self.START, tool_radius_map=tmap,
                              initial_tool_radius=r0)

    def test_same_line_t_and_motion_recorded_separately(self):
        p = self.parse("G1 X0 Y11\nT2 G1 X15 Y11\n", {2: 3.0}, r0=0.5)
        self.assertEqual(len(p.motions), 2)
        self.assertEqual(len(p.tool_events), 1)
        ev = p.tool_events[0]
        # 换刀点为本行运动前刀位 (0,11)，执行顺序先于本行运动
        self.assertEqual(ev.line_no, 2)
        self.assertEqual(ev.pos, (0.0, 11.0, 0.0))
        self.assertEqual(ev.tool_no, 2)
        self.assertEqual(ev.tool_radius, 3.0)
        self.assertLess(ev.seq, p.motions[1].seq)

    def test_every_t_switch_emits_event_in_order(self):
        p = self.parse("T1\nT2\nG1 X1 Y11\nT1 G1 X2 Y11\n", {1: 1.0, 2: 2.0})
        self.assertEqual([e.tool_no for e in p.tool_events], [1, 2, 1])
        # 换刀事件与运动段共用同一执行序号轴，合并后严格递增
        seqs = sorted([e.seq for e in p.tool_events]
                      + [m.seq for m in p.motions])
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        # 独立 T 行与零长度 T 行同样只产生一次换刀事件
        p2 = self.parse("T2\nT2 X-5 Y11\n", {2: 2.0})
        self.assertEqual(len(p2.tool_events), 2)
        self.assertEqual(p2.motions, [])


class ExecutionTraceEngineTests(unittest.TestCase):
    """执行事件轨迹：顺序、字段与命中；首碰撞对应首个命中事件。"""

    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def trace(self, text, start=(-5.0, 11.0, 0.0), r0=0.0, tmap=None,
              zones=None):
        p = gc.parse_gcode(text, start, tool_radius_map=tmap,
                           initial_tool_radius=r0)
        return build_execution_trace(p.motions, p.tool_events,
                                     zones or [self.zone], start, r0)

    def test_trace_order_and_fields(self):
        tr = self.trace("G1 X-2 Y11\nT2\nG1 X15 Y11\n", tmap={2: 2.0})
        self.assertEqual([e.kind for e in tr],
                         ["initial", "motion", "tool_change", "motion"])
        self.assertEqual([e.seq for e in tr], [0, 1, 2, 3])
        init = tr[0]
        self.assertIsNone(init.line_no)
        self.assertIsNone(init.tool_no)
        self.assertEqual(init.tool_radius, 0.0)
        self.assertEqual(init.start, init.end)
        tc = tr[2]
        self.assertEqual(tc.line_no, 2)
        self.assertEqual(tc.tool_no, 2)
        self.assertEqual(tc.tool_radius, 2.0)
        self.assertEqual(tc.start, tc.end)  # 静止换刀：起止相同
        # 命中：行 3 运动 r=2 横穿 y=11（最近 1mm）=> 命中禁区 1
        self.assertIsNone(tr[1].hit)
        self.assertIsNone(tc.hit)  # (-2,11) 距角 (0,10) 为 sqrt(5) > 2
        self.assertIsNotNone(tr[3].hit)
        self.assertEqual(tr[3].hit.zone_no, 1)
        self.assertEqual(tr[3].start, (-2.0, 11.0, 0.0))
        self.assertEqual(tr[3].end, (15.0, 11.0, 0.0))

    def test_first_collision_is_first_hit_event(self):
        text = "G1 X-2 Y11\nT2\nG1 X15 Y11\n"
        tr = self.trace(text, tmap={2: 2.0})
        first_hit = next(e for e in tr if e.hit is not None)
        p = gc.parse_gcode(text, (-5.0, 11.0, 0.0), tool_radius_map={2: 2.0})
        c = check_program(p.motions, p.tool_events, [self.zone],
                          (-5.0, 11.0, 0.0), 0.0)
        self.assertEqual(first_hit.kind, "motion")
        self.assertEqual((c.line_no, c.zone_no, c.tool_no, c.tool_radius),
                         (first_hit.line_no, first_hit.hit.zone_no,
                          first_hit.tool_no, first_hit.tool_radius))

    def test_same_line_t_checked_before_motion(self):
        # 同行 T 换大刀：换刀点 (0,11) 静止即碰撞（r=2，距离 1mm）
        tr = self.trace("T1 G1 X5 Y11\n", start=(0.0, 11.0, 0.0),
                        tmap={1: 2.0})
        self.assertEqual([e.kind for e in tr],
                         ["initial", "tool_change", "motion"])
        self.assertIsNone(tr[0].hit)        # 初始 r=0，距离 1 不碰
        self.assertIsNotNone(tr[1].hit)     # 先按新半径检查换刀点：命中
        self.assertIsNotNone(tr[2].hit)     # 再检查运动：起点同位置也命中
        self.assertEqual(tr[1].line_no, 1)
        self.assertEqual(tr[1].hit.zone_no, 1)
        self.assertEqual(tr[1].hit.tool_no, 1)

    def test_min_clearance_follows_same_event_order(self):
        # 同行 T：换刀点静止净间隙 -1（r=2，距离 1），与运动起点候选等值，
        # 按执行顺序换刀事件（seq 更小）胜出，行号同为第 1 行
        p = gc.parse_gcode("T1 G1 X5 Y11\n", (0.0, 11.0, 0.0),
                           tool_radius_map={1: 2.0})
        cp = minimum_clearance_program(p.motions, p.tool_events, [self.zone],
                                       (0.0, 11.0, 0.0), 0.0)
        self.assertEqual(cp.net, -1.0)
        self.assertEqual(cp.line_no, 1)
        self.assertEqual(cp.tool_no, 1)
        self.assertEqual(cp.point, (0.0, 11.0, 0.0))


class ExecutionTraceApiTests(unittest.TestCase):
    ZONE_NO = 700

    def setUp(self):
        if client.get(f"/zones/{self.ZONE_NO}").status_code == 404:
            r = client.post("/zones", json={
                "zone_no": self.ZONE_NO, "polygon": SQUARE,
                "z_min": -5, "z_max": 5})
            self.assertEqual(r.status_code, 201, r.text)

    def _review(self, **kw):
        body = {
            "zones": [{"zone_no": self.ZONE_NO, "version": 1}],
            "start": [-5.0, 11.0, 0.0], "tool_radius": 0.0,
            "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n",
            "tool_radii": {"2": 2.0}}
        body.update(kw)
        return client.post("/reviews", json=body)

    def test_trace_returned_and_frozen_in_report(self):
        r = self._review(include_min_clearance=True)
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        trace = b["execution_trace"]
        self.assertEqual([e["kind"] for e in trace],
                         ["initial", "motion", "tool_change", "motion"])
        self.assertEqual([e["seq"] for e in trace], [0, 1, 2, 3])
        # 每项字段完整：原行号、刀号、半径、起止坐标、命中禁区或空
        e0 = trace[0]
        self.assertIsNone(e0["line_no"])
        self.assertIsNone(e0["tool_no"])
        self.assertEqual(e0["tool_radius_mm"], 0.0)
        self.assertEqual(e0["start_mm"], [-5.0, 11.0, 0.0])
        self.assertEqual(e0["end_mm"], [-5.0, 11.0, 0.0])
        self.assertIsNone(e0["hit_zone_no"])
        e2 = trace[2]
        self.assertEqual(e2["kind"], "tool_change")
        self.assertEqual(e2["line_no"], 2)
        self.assertEqual(e2["tool_no"], 2)
        self.assertEqual(e2["tool_radius_mm"], 2.0)
        self.assertEqual(e2["start_mm"], [-2.0, 11.0, 0.0])
        self.assertEqual(e2["end_mm"], [-2.0, 11.0, 0.0])
        self.assertIsNone(e2["hit_zone_no"])
        e3 = trace[3]
        self.assertEqual(e3["hit_zone_no"], self.ZONE_NO)
        self.assertEqual(e3["start_mm"], [-2.0, 11.0, 0.0])
        self.assertEqual(e3["end_mm"], [15.0, 11.0, 0.0])
        # 首碰撞对应轨迹中首个命中事件
        first_hit = next(e for e in trace if e["hit_zone_no"] is not None)
        self.assertEqual(first_hit["line_no"], b["collision_line_no"])
        self.assertEqual(first_hit["hit_zone_no"], b["collision_zone_no"])
        self.assertEqual(first_hit["tool_no"], b["collision_tool_no"])
        self.assertEqual(first_hit["tool_radius_mm"],
                         b["collision_tool_radius_mm"])
        # 轨迹随报告固化：GET 与提交响应一致
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertEqual(got["execution_trace"], trace)

    def test_trace_immutable_after_zone_new_version(self):
        r = self._review()
        rid = r.json()["report_id"]
        trace = r.json()["execution_trace"]
        # 禁区出新版本（平移到远处），旧报告轨迹不变
        client.post(f"/zones/{self.ZONE_NO}/versions", json={
            "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
            "z_min": -5, "z_max": 5})
        got = client.get(f"/reports/{rid}").json()
        self.assertEqual(got["execution_trace"], trace)
        self.assertTrue(got["has_collision"])

    def test_same_line_t_trace_and_collision(self):
        # 同行 T 换大刀：换刀点静止即命中，轨迹分别记录 T 与运动
        r = self._review(start=[0.0, 11.0, 0.0],
                         gcode="T1 G1 X5 Y11 Z0\n",
                         tool_radii={"1": 2.0})
        b = r.json()
        trace = b["execution_trace"]
        self.assertEqual([e["kind"] for e in trace],
                         ["initial", "tool_change", "motion"])
        self.assertIsNone(trace[0]["hit_zone_no"])
        self.assertEqual(trace[1]["hit_zone_no"], self.ZONE_NO)
        self.assertEqual(trace[2]["hit_zone_no"], self.ZONE_NO)
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)
        self.assertEqual(b["collision_tool_no"], 1)
        self.assertEqual(b["collision_tool_radius_mm"], 2.0)

    def test_single_tool_request_and_old_report_trace_null(self):
        # 单刀请求（不传 tool_radii）：T 忽略，轨迹为 null
        r = client.post("/reviews", json={
            "zones": [{"zone_no": self.ZONE_NO, "version": 1}],
            "start": [-5.0, 11.0, 0.0], "tool_radius": 0.5,
            "gcode": "T9 M6\nG1 X15 Y11 Z0\n"})
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertIsNone(b["execution_trace"])
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertIsNone(got["execution_trace"])

    def test_invalid_t_still_422_no_report(self):
        ok = self._review()
        last_id = ok.json()["report_id"]
        r = self._review(gcode="T9\n", tool_radii={"1": 1.0})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "invalid_gcode")
        # 非法 T 不留报告
        self.assertEqual(client.get(f"/reports/{last_id + 1}").status_code,
                         404)


class BatchReviewApiTests(unittest.TestCase):
    """批量审查：同一事务、逐项错误、batch_id 幂等/冲突。"""

    @classmethod
    def setUpClass(cls):
        client.post("/zones", json={"zone_no": 500, "polygon": SQUARE,
                                    "z_min": -5, "z_max": 5})
        # 第二版本平移到远处，用于验证各项独立引用版本快照
        client.post("/zones/500/versions", json={
            "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
            "z_min": -5, "z_max": 5})

    def _item(self, **kw):
        item = {"zones": [{"zone_no": 500, "version": 1}],
                "start": [-5, 5, 0], "tool_radius": 0.0,
                "gcode": "G1 X15 Y5 Z0\n"}
        item.update(kw)
        return item

    def _post(self, batch_id, items):
        return client.post("/reviews/batch",
                           json={"batch_id": batch_id, "items": items})

    def _last_report_id(self):
        # 提交一条确定无碰撞的单项审查，用其 report_id 定位报告序列末端
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 500, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0, "gcode": "G1 X15 Y5 Z0\n"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["report_id"]

    def test_01_happy_path_ordered_results(self):
        items = [
            self._item(task_id="hit"),                       # v1 横穿 => 碰撞
            self._item(task_id="safe", start=[-5, 50, 0],
                       gcode="G1 X15 Y50 Z0\n"),             # 上方经过 => 无碰撞
            self._item(task_id="mt", start=[-5, 11, 0],
                       tool_radii={"2": 2.0},
                       gcode="G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n",
                       include_min_clearance=True),          # 多刀 + 净间隙
        ]
        r = self._post("B-001", items)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["batch_id"], "B-001")
        self.assertEqual(body["item_count"], 3)
        results = body["results"]
        self.assertEqual([x["index"] for x in results], [0, 1, 2])
        self.assertEqual([x["task_id"] for x in results],
                         ["hit", "safe", "mt"])
        # 报告按输入顺序生成，编号递增
        ids = [x["result"]["report_id"] for x in results]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 3)
        # 各项结果与单项审查口径一致
        r0, r1, r2 = (x["result"] for x in results)
        self.assertTrue(r0["has_collision"])
        self.assertEqual(r0["collision_zone_no"], 500)
        self.assertFalse(r1["has_collision"])
        self.assertTrue(r2["has_collision"])
        self.assertEqual(r2["collision_tool_no"], 2)
        self.assertEqual(r2["collision_tool_radius_mm"], 2.0)
        self.assertIsNotNone(r2["execution_trace"])
        self.assertIsNotNone(r2["min_clearance"])
        self.assertEqual(r2["used_zone_versions"], {"500": 1})
        # 报告真实落库且不可变查询一致
        for x in results:
            got = client.get(
                f"/reports/{x['result']['report_id']}").json()
            self.assertEqual(got["has_collision"],
                             x["result"]["has_collision"])
            self.assertEqual(got["used_zone_versions"],
                             x["result"]["used_zone_versions"])

    def test_02_items_independent_zone_versions(self):
        # 同一禁区不同版本：v1 碰撞、v2 无碰撞，互不影响
        items = [self._item(task_id="v1"),
                 self._item(task_id="v2",
                            zones=[{"zone_no": 500, "version": 2}])]
        r = self._post("B-002", items)
        self.assertEqual(r.status_code, 201, r.text)
        a, b = (x["result"] for x in r.json()["results"])
        self.assertTrue(a["has_collision"])
        self.assertEqual(a["used_zone_versions"], {"500": 1})
        self.assertFalse(b["has_collision"])
        self.assertEqual(b["used_zone_versions"], {"500": 2})

    def test_03_any_invalid_item_fails_whole_batch(self):
        last_id = self._last_report_id()
        items = [
            self._item(task_id="ok1"),
            self._item(task_id="bad-gcode", gcode="G54 G1 X1\n"),
            self._item(task_id="bad-zone",
                       zones=[{"zone_no": 999, "version": 1}]),
            self._item(task_id="ok2"),
        ]
        r = self._post("B-003", items)
        self.assertEqual(r.status_code, 422, r.text)
        body = r.json()
        self.assertEqual(body["error"], "invalid_batch")
        self.assertEqual(body["batch_id"], "B-003")
        # 逐项错误：仅列出失败项，含输入顺序号与任务标识
        failed = body["failed_items"]
        self.assertEqual([f["index"] for f in failed], [1, 2])
        self.assertEqual([f["task_id"] for f in failed],
                         ["bad-gcode", "bad-zone"])
        self.assertEqual(failed[0]["error"]["error"], "invalid_gcode")
        self.assertIn("不存在", failed[1]["error"])
        # 整批不生成报告：序列末端未推进
        self.assertEqual(self._last_report_id(), last_id + 1)
        # 失败的批次不占用 batch_id：修正后可同名成功提交
        items[1]["gcode"] = "G1 X15 Y50 Z0\n"
        items[2]["zones"] = [{"zone_no": 500, "version": 1}]
        r = self._post("B-003", items)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["item_count"], 4)

    def test_04_invalid_task_identifier_rejected(self):
        # 非法任务标识（空格/非法字符）=> 422，整批不落报告
        last_id = self._last_report_id()
        r = self._post("B-004", [self._item(task_id="bad id!")])
        self.assertEqual(r.status_code, 422)
        # 批内任务标识重复 => 422
        r = self._post("B-004", [self._item(task_id="dup"),
                                 self._item(task_id="dup")])
        self.assertEqual(r.status_code, 422)
        # 非法 batch_id 同样 422
        r = self._post("bad batch!", [self._item()])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self._last_report_id(), last_id + 1)

    def test_05_item_count_bounds(self):
        r = self._post("B-005", [])
        self.assertEqual(r.status_code, 422)
        r = self._post("B-005", [self._item() for _ in range(51)])
        self.assertEqual(r.status_code, 422)
        r = self._post("B-005", [self._item() for _ in range(50)])
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["item_count"], 50)

    def test_06_replay_returns_first_results(self):
        items = [self._item(task_id="a"),
                 self._item(task_id="b", start=[-5, 50, 0],
                            gcode="G1 X15 Y50 Z0\n")]
        r = self._post("B-006", items)
        self.assertEqual(r.status_code, 201, r.text)
        first = r.json()
        last_id = self._last_report_id()

        # 相同 batch_id + 相同内容重放：200，逐项结果与首次完全一致
        r = self._post("B-006", items)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), first)
        # 重放不生成新报告
        self.assertEqual(self._last_report_id(), last_id + 1)

    def test_07_same_batch_id_different_content_conflicts(self):
        r = self._post("B-007", [self._item()])
        self.assertEqual(r.status_code, 201, r.text)
        last_id = self._last_report_id()
        # 相同 batch_id、不同内容 => 409，且不生成新报告
        r = self._post("B-007", [self._item(gcode="G1 X15 Y50 Z0\n")])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["error"], "batch_conflict")
        self.assertEqual(self._last_report_id(), last_id + 1)


class SchemaMigrationTests(unittest.TestCase):
    """旧库（缺少 v1.1/v1.2/v1.3 新列）启动时幂等补齐，旧报告读取兼容。"""

    def test_legacy_db_gets_columns_and_old_report_reads(self):
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app import main as mainmod

        legacy = create_engine(
            "sqlite:///" + tempfile.mktemp(suffix=".db"),
            connect_args={"check_same_thread": False}, future=True)
        with legacy.begin() as conn:
            conn.execute(text(
                "CREATE TABLE reports (id INTEGER PRIMARY KEY, "
                "created_at DATETIME, zone_versions JSON, input_summary JSON, "
                "has_collision BOOLEAN, collision_line_no INTEGER, "
                "collision_zone_no INTEGER, conclusion TEXT)"))
            conn.execute(text(
                "INSERT INTO reports (zone_versions, input_summary, "
                "has_collision, collision_line_no, collision_zone_no, "
                "conclusion) VALUES (:zv, :sm, 0, NULL, NULL, 'no_collision')"),
                {"zv": json.dumps({"1": 1}),
                 "sm": json.dumps({"tool_radius_mm": 0.5})})

        mainmod._ensure_report_columns(legacy)
        cols = {c["name"] for c in inspect(legacy).get_columns("reports")}
        for name in ("min_clearance_enabled", "min_clearance",
                     "collision_tool_no", "collision_tool_radius",
                     "execution_trace"):
            self.assertIn(name, cols)
        # 再跑一次幂等
        mainmod._ensure_report_columns(legacy)

        Session = sessionmaker(bind=legacy)
        db = Session()
        try:
            report = db.get(models.Report, 1)
            self.assertIsNone(report.collision_tool_no)
            self.assertIsNone(report.collision_tool_radius)
            self.assertFalse(bool(report.min_clearance_enabled))
            self.assertIsNone(report.min_clearance)
            self.assertIsNone(report.execution_trace)
        finally:
            db.close()
            legacy.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
