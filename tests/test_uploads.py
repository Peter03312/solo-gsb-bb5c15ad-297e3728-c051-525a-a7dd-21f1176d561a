"""长 G-code 可恢复上传会话端到端测试。使用 SQLite 覆盖数据库。"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app import schemas  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(app)

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
MAX_CHUNK = schemas.MAX_CHUNK_BYTES
MAX_SESSION = schemas.MAX_SESSION_BYTES


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UploadSessionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.post("/zones", json={"zone_no": 700, "polygon": SQUARE,
                                    "z_min": -5, "z_max": 5})
        client.post("/zones/700/versions", json={
            "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
            "z_min": -5, "z_max": 5})

    def _create(self, **kw):
        body = {"zones": [{"zone_no": 700, "version": 1}],
                "start": [-5, 5, 0], "tool_radius": 0.0}
        body.update(kw)
        r = client.post("/uploads", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _gcode_bytes(self):
        return ("G21 G90 G17\n"
                "G1 X15 Y5 Z0\n").encode("utf-8")

    def _upload(self, sid, data, split_at, order=None):
        parts = [data[:split_at], data[split_at:]]
        order = order or range(len(parts))
        for i in order:
            r = client.put(f"/uploads/{sid}/chunks/{i}", content=parts[i])
            self.assertEqual(r.status_code, 200, r.text)
        return parts

    # ---------- 创建 ----------

    def test_01_create_session_pins_params_and_limits(self):
        s = self._create()
        self.assertEqual(len(s["session_id"]), 32)
        self.assertEqual(s["status"], "open")
        self.assertEqual(s["zones"], [{"zone_no": 700, "version": 1}])
        self.assertEqual(s["start"], [-5.0, 5.0, 0.0])
        self.assertEqual(s["max_chunk_bytes"], 64 * 1024)
        self.assertEqual(s["max_session_bytes"], 8 * 1024 * 1024)
        # 状态可查
        st = client.get(f"/uploads/{s['session_id']}").json()
        self.assertEqual(st["received_seqs"], [])
        self.assertEqual(st["total_bytes"], 0)
        self.assertIsNone(st["report_id"])

    def test_02_create_validates_zone_refs(self):
        r = client.post("/uploads", json={
            "zones": [{"zone_no": 999, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0})
        self.assertEqual(r.status_code, 404)
        r = client.post("/uploads", json={
            "zones": [{"zone_no": 700, "version": 9}],
            "start": [-5, 5, 0], "tool_radius": 0.0})
        self.assertEqual(r.status_code, 404)
        # 负刀半径、非布尔开关仍按单项口径 422
        r = client.post("/uploads", json={
            "zones": [{"zone_no": 700, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": -1})
        self.assertEqual(r.status_code, 422)
        r = client.post("/uploads", json={
            "zones": [{"zone_no": 700, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "include_min_clearance": "yes"})
        self.assertEqual(r.status_code, 422)

    # ---------- 块提交 ----------

    def test_03_out_of_order_and_status(self):
        s = self._create()
        sid = s["session_id"]
        data = self._gcode_bytes()
        p0, p1 = data[:10], data[10:]
        # 先到序号 1（乱序）
        r = client.put(f"/uploads/{sid}/chunks/1", content=p1)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["resubmitted"])
        self.assertEqual(body["received_seqs"], [1])
        self.assertEqual(body["size_bytes"], len(p1))
        self.assertEqual(body["chunk_sha256"], sha256_hex(p1))
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["received_seqs"], [1])
        self.assertEqual(st["total_bytes"], len(p1))
        # 再到序号 0
        r = client.put(f"/uploads/{sid}/chunks/0", content=p0)
        self.assertEqual(r.json()["received_seqs"], [0, 1])
        self.assertEqual(r.json()["total_bytes"], len(data))

    def test_04_same_seq_same_content_returns_receipt(self):
        s = self._create()
        sid = s["session_id"]
        r1 = client.put(f"/uploads/{sid}/chunks/0", content=b"G1 X1\n")
        r2 = client.put(f"/uploads/{sid}/chunks/0", content=b"G1 X1\n")
        self.assertEqual(r1.status_code, r2.status_code, 200)
        b1, b2 = r1.json(), r2.json()
        self.assertFalse(b1["resubmitted"])
        self.assertTrue(b2["resubmitted"])
        # 原回执要素一致
        self.assertEqual(b1["seq"], b2["seq"])
        self.assertEqual(b1["size_bytes"], b2["size_bytes"])
        self.assertEqual(b1["chunk_sha256"], b2["chunk_sha256"])
        # 不重复入库
        self.assertEqual(b2["received_count"], 1)

    def test_05_same_seq_different_content_conflict(self):
        s = self._create()
        sid = s["session_id"]
        client.put(f"/uploads/{sid}/chunks/0", content=b"G1 X1\n")
        r = client.put(f"/uploads/{sid}/chunks/0", content=b"G1 X2\n")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "chunk_conflict")
        # 原块不被覆盖
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["total_bytes"], len(b"G1 X1\n"))

    def test_06_chunk_size_limits_and_empty_chunk(self):
        s = self._create()
        sid = s["session_id"]
        # 恰好 64 KiB 允许
        r = client.put(f"/uploads/{sid}/chunks/0", content=b"a" * MAX_CHUNK)
        self.assertEqual(r.status_code, 200)
        # 超过 1 字节拒绝
        r = client.put(f"/uploads/{sid}/chunks/1", content=b"a" * (MAX_CHUNK + 1))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["detail"]["error"], "chunk_too_large")
        # 空块允许（UTF-8 空文本）
        r = client.put(f"/uploads/{sid}/chunks/2", content=b"")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["size_bytes"], 0)
        self.assertEqual(r.json()["chunk_sha256"],
                         hashlib.sha256(b"").hexdigest())
        # 非法 UTF-8 拒绝
        r = client.put(f"/uploads/{sid}/chunks/3", content=b"\xff\xfe")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "invalid_utf8")

    def test_07_session_total_limit(self):
        s = self._create()
        sid = s["session_id"]
        # 64 KiB * 128 = 8 MiB 允许；下一块超限
        for i in range(128):
            r = client.put(f"/uploads/{sid}/chunks/{i}",
                           content=b"a" * MAX_CHUNK)
            self.assertEqual(r.status_code, 200, (i, r.text))
        r = client.put(f"/uploads/{sid}/chunks/128", content=b"a")
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["detail"]["error"], "session_too_large")
        self.assertEqual(r.json()["detail"]["total_bytes"], MAX_SESSION + 1)

    def test_08_unknown_session_and_negative_seq(self):
        r = client.put("/uploads/deadbeef/chunks/0", content=b"G1 X1\n")
        self.assertEqual(r.status_code, 404)
        r = client.get("/uploads/deadbeef")
        self.assertEqual(r.status_code, 404)
        s = self._create()
        r = client.put(f"/uploads/{s['session_id']}/chunks/-1", content=b"x")
        self.assertEqual(r.status_code, 404)

    # ---------- 完成 ----------

    def test_09_complete_success_matches_single_review(self):
        s = self._create()
        sid = s["session_id"]
        data = self._gcode_bytes()
        self._upload(sid, data, split_at=12, order=[1, 0])
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["session_id"], sid)
        self.assertTrue(body["has_collision"])
        self.assertEqual(body["collision_line_no"], 2)
        self.assertEqual(body["collision_zone_no"], 700)
        # 与单项审查口径一致（除会话标识与报告编号）
        single = client.post("/reviews", json={
            "zones": [{"zone_no": 700, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": data.decode()}).json()
        for k, v in single.items():
            if k == "report_id":
                continue
            self.assertEqual(body[k], v, f"字段 {k} 与单项审查不一致")
        # 报告真实落库、不可变，行号按拼接后原文计算
        rid = body["report_id"]
        got = client.get(f"/reports/{rid}").json()
        self.assertEqual(got["collision_line_no"], 2)
        self.assertEqual(got["input_summary"]["gcode_sha256"],
                         sha256_hex(data))
        # 状态更新为 completed
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["report_id"], rid)
        self.assertEqual(st["block_count"], 2)

    def test_10_complete_replay_same_report_and_conflict(self):
        s = self._create()
        sid = s["session_id"]
        data = b"G1 X15 Y50 Z0\n"
        self._upload(sid, data, split_at=4)
        claim = {"block_count": 2, "sha256": sha256_hex(data)}
        r1 = client.post(f"/uploads/{sid}/complete", json=claim)
        self.assertEqual(r1.status_code, 201)
        rid = r1.json()["report_id"]
        # 相同完成请求重放：200 + 同一报告
        r2 = client.post(f"/uploads/{sid}/complete", json=claim)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json(), r1.json())
        # 不同块数声明 => 409
        r3 = client.post(f"/uploads/{sid}/complete",
                         json={"block_count": 1, "sha256": claim["sha256"]})
        self.assertEqual(r3.status_code, 409)
        self.assertEqual(r3.json()["detail"]["error"], "completion_conflict")
        # 不同摘要声明 => 409
        r4 = client.post(f"/uploads/{sid}/complete",
                         json={"block_count": 2, "sha256": "0" * 64})
        self.assertEqual(r4.status_code, 409)
        # 成功完成后拒绝新块
        r5 = client.put(f"/uploads/{sid}/chunks/9", content=b"x")
        self.assertEqual(r5.status_code, 409)
        self.assertEqual(r5.json()["detail"]["error"], "session_completed")
        # 报告没有多生成
        last = client.post("/reviews", json={
            "zones": [{"zone_no": 700, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"}).json()["report_id"]
        self.assertEqual(last, rid + 1)

    def test_11_missing_blocks_no_report(self):
        s = self._create()
        sid = s["session_id"]
        data = self._gcode_bytes()
        # 交 0、2，缺 1（三段不重叠切分）
        c0, c1, c2 = data[:8], data[8:12], data[12:]
        client.put(f"/uploads/{sid}/chunks/0", content=c0)
        client.put(f"/uploads/{sid}/chunks/2", content=c2)
        last_before = client.post("/reviews", json={
            "zones": [{"zone_no": 700, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"}).json()["report_id"]
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 3, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 409)
        d = r.json()["detail"]
        self.assertEqual(d["error"], "blocks_incomplete")
        self.assertEqual(d["missing_seqs"], [1])
        # 序号 2 仍在声明范围 [0,3) 内，不算多余块
        self.assertEqual(d["extra_seqs"], [])
        # 会话仍 open，补齐缺块 1 后即可按 3 块完成
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["status"], "open")
        client.put(f"/uploads/{sid}/chunks/1", content=c1)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 3, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201, r.text)
        # 缺块失败期间不产生报告：序列末端只因上面一次成功完成 +1
        last_after = client.post("/reviews", json={
            "zones": [{"zone_no": 700, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y50 Z0\n"}).json()["report_id"]
        self.assertEqual(last_after, last_before + 2)

    def test_12_digest_mismatch_no_report_session_stays_open(self):
        s = self._create()
        sid = s["session_id"]
        data = self._gcode_bytes()
        self._upload(sid, data, split_at=10)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": "0" * 64})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "sha256_mismatch")
        self.assertEqual(r.json()["detail"]["actual_sha256"],
                         sha256_hex(data))
        # 会话仍 open，可用正确声明完成
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["status"], "open")
        r2 = client.post(f"/uploads/{sid}/complete",
                         json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r2.status_code, 201)

    def test_13_invalid_gcode_no_report_session_stays_open(self):
        s = self._create()
        sid = s["session_id"]
        data = "G1 X1\nG54 G1 X2\n".encode()
        client.put(f"/uploads/{sid}/chunks/0", content=data[:7])
        client.put(f"/uploads/{sid}/chunks/1", content=data[7:])
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 422)
        d = r.json()["detail"]
        self.assertEqual(d["error"], "invalid_gcode")
        self.assertEqual(d["line_no"], 2)
        # 不留报告，会话保持 open
        self.assertEqual(client.get(f"/uploads/{sid}").json()["status"],
                         "open")

    def test_14_block_count_must_be_positive(self):
        s = self._create()
        sid = s["session_id"]
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 0, "sha256": "0" * 64})
        self.assertEqual(r.status_code, 422)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": "xyz"})
        self.assertEqual(r.status_code, 422)

    def test_15_utf8_multibyte_split_across_chunks(self):
        # 注释内含中文（多字节 UTF-8）。客户端必须在 UTF-8 字符边界切块：
        # 落在多字节字符中间的块本身不是合法 UTF-8，会被 422 拒绝且不入库；
        # 在合法边界切分后，拼接文本按原文行号解析。
        s = self._create()
        sid = s["session_id"]
        data = "(中文注释)\nG1 X15 Y50 Z0\n".encode("utf-8")
        cut = 8  # 落在一个三字节汉字中间
        self.assertRaises(UnicodeDecodeError, data[cut - 1:cut + 1].decode)
        r = client.put(f"/uploads/{sid}/chunks/0", content=data[:cut])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["error"], "invalid_utf8")
        # 该块未入库
        self.assertEqual(client.get(f"/uploads/{sid}").json()["received_count"],
                         0)
        # 在合法 UTF-8 边界切块重传序号 0
        cut2 = data.index("G1".encode())
        r = client.put(f"/uploads/{sid}/chunks/0", content=data[:cut2])
        self.assertEqual(r.status_code, 200)
        r = client.put(f"/uploads/{sid}/chunks/1", content=data[cut2:])
        self.assertEqual(r.status_code, 200)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201, r.text)
        # 路径 y=50 在禁区上方：无碰撞，中文注释不影响行号解析
        self.assertFalse(r.json()["has_collision"])
        self.assertEqual(r.json()["input_summary"]["gcode_lines"], 2)

    def test_16_multitool_and_clearance_params_pinned(self):
        s = self._create(start=[-5, 11, 0], tool_radius=0.0,
                         tool_radii={"2": 2.0},
                         include_min_clearance=True)
        sid = s["session_id"]
        data = ("G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n").encode()
        # 三块乱序
        cut1, cut2 = 14, 18
        client.put(f"/uploads/{sid}/chunks/2", content=data[cut2:])
        client.put(f"/uploads/{sid}/chunks/0", content=data[:cut1])
        client.put(f"/uploads/{sid}/chunks/1", content=data[cut1:cut2])
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 3, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["collision_line_no"], 3)
        self.assertEqual(body["collision_tool_no"], 2)
        self.assertEqual(body["collision_tool_radius_mm"], 2.0)
        self.assertEqual(body["min_clearance"]["tool_no"], 2)
        self.assertIsNotNone(body["execution_trace"])
        # 固定参数在状态中可回看
        st = client.get(f"/uploads/{sid}").json()
        self.assertEqual(st["tool_radii"], {"2": 2.0})
        self.assertTrue(st["include_min_clearance"])

    def test_17_pinned_version_independent_snapshot(self):
        # v1 碰撞、v2 平移后无碰撞：会话创建时固定 v1，结果不受 v2 影响
        s = self._create(zones=[{"zone_no": 700, "version": 1}])
        sid = s["session_id"]
        data = b"G1 X15 Y5 Z0\n"
        self._upload(sid, data, split_at=3)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()["has_collision"])
        s2 = self._create(zones=[{"zone_no": 700, "version": 2}])
        self._upload(s2["session_id"], data, split_at=3)
        r2 = client.post(f"/uploads/{s2['session_id']}/complete",
                         json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertFalse(r2.json()["has_collision"])

    def test_18_single_block_session(self):
        s = self._create()
        sid = s["session_id"]
        data = b"G1 X15 Y50 Z0\n"
        client.put(f"/uploads/{sid}/chunks/0", content=data)
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 1, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["has_collision"])
        # 声明块数与实际不符
        s2 = self._create()
        client.put(f"/uploads/{s2['session_id']}/chunks/0", content=data)
        r = client.post(f"/uploads/{s2['session_id']}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["missing_seqs"], [1])

    def test_19_extra_blocks_beyond_declared_count(self):
        s = self._create()
        sid = s["session_id"]
        data = b"G1 X1\nG1 X2\n"
        client.put(f"/uploads/{sid}/chunks/0", content=data[:6])
        client.put(f"/uploads/{sid}/chunks/1", content=data[6:])
        client.put(f"/uploads/{sid}/chunks/2", content=b"\n")
        r = client.post(f"/uploads/{sid}/complete",
                        json={"block_count": 2, "sha256": sha256_hex(data)})
        self.assertEqual(r.status_code, 409)
        d = r.json()["detail"]
        self.assertEqual(d["error"], "blocks_incomplete")
        self.assertEqual(d["extra_seqs"], [2])


class UploadPersistenceTests(unittest.TestCase):
    """会话与块跨服务重启（同库文件）保留。"""

    def test_sessions_survive_restart(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from app import models

        db_path = tempfile.mktemp(suffix=".db")
        engine_a = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False}, future=True)
        Base.metadata.create_all(engine_a)
        db = sessionmaker(bind=engine_a)()
        sid = "persist000000000000000000000000001"
        sess = models.UploadSession(
            session_id=sid, status="open",
            params={"zones": [{"zone_no": 700, "version": 1}],
                    "start": [-5, 5, 0], "tool_radius": 0.0,
                    "include_min_clearance": False, "tool_radii": None},
            total_bytes=3)
        db.add(sess)
        db.flush()
        db.add(models.UploadChunk(session_id=sess.id, seq=0,
                                  content="G1 ", size_bytes=3,
                                  content_sha256=hashlib.sha256(b"G1 ")
                                  .hexdigest()))
        db.commit()
        db.close()
        engine_a.dispose()

        # “重启”：新引擎打开同一数据库文件，行仍在
        engine_b = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False}, future=True)
        db2 = sessionmaker(bind=engine_b)()
        try:
            got = db2.scalar(select(models.UploadSession).where(
                models.UploadSession.session_id == sid))
            self.assertIsNotNone(got)
            self.assertEqual(got.status, "open")
            chunks = db2.scalars(
                select(models.UploadChunk).where(
                    models.UploadChunk.session_id == got.id)
                .order_by(models.UploadChunk.seq)).all()
            self.assertEqual([c.seq for c in chunks], [0])
            self.assertEqual(chunks[0].content, "G1 ")
        finally:
            db2.close()
            engine_b.dispose()
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
