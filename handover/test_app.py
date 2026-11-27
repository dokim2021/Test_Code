import contextlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import app


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        app.init_db(self.db)
        self.conn = app.connect(self.db)
        app.create_vehicles(self.conn, ["1호차"])
        self.vid = 1

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def items(self):
        return app.get_vehicle(self.conn, self.vid)["items"]

    def first_item(self):
        return self.items()[0]["id"]

    def complete(self, item_id):
        app.apply_step(self.conn, item_id, "install", "pass", "설치자")
        app.apply_step(self.conn, item_id, "verify", "pass", "확인자")
        app.apply_step(self.conn, item_id, "confirm", "pass", "컨펌자")



class LogicTest(_Base):
    def test_checklist_created(self):
        items = self.items()
        self.assertEqual(len(items), 25)
        self.assertEqual(sum(i["grp"] == "camera" for i in items), 8)
        self.assertEqual(sum(i["grp"] == "lidar" for i in items), 8)
        self.assertEqual(sum(not i["required"] for i in items), 1)

    def test_duplicate_vehicle_skipped(self):
        r = app.create_vehicles(self.conn, ["1호차", "2호차"])
        self.assertEqual(r, {"created": ["2호차"], "skipped": ["1호차"]})

    def test_order_enforced(self):
        iid = self.first_item()
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, iid, "verify", "pass", "확인자")
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, iid, "confirm", "pass", "컨펌자")
        app.apply_step(self.conn, iid, "install", "pass", "설치자")
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, iid, "confirm", "pass", "컨펌자")

    def test_same_person_rejected(self):
        iid = self.first_item()
        app.apply_step(self.conn, iid, "install", "pass", "A")
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, iid, "verify", "pass", "A")
        app.apply_step(self.conn, iid, "verify", "pass", "B")
        for person in ("A", "B"):
            with self.assertRaises(app.StepError):
                app.apply_step(self.conn, iid, "confirm", "pass", person)
        self.assertEqual(app.apply_step(self.conn, iid, "confirm", "pass", "C")["state"], "confirmed")

    def test_fail_requires_memo_and_goes_to_rework(self):
        iid = self.first_item()
        app.apply_step(self.conn, iid, "install", "pass", "A")
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, iid, "verify", "fail", "B")
        self.assertEqual(
            app.apply_step(self.conn, iid, "verify", "fail", "B", "영상 안 나옴")["state"], "rework"
        )
        self.assertEqual(app.get_vehicle(self.conn, self.vid)["stage"], "rework")
        # 재설치 후 이전 확인자는 초기화, 이력은 누적
        app.apply_step(self.conn, iid, "install", "pass", "A")
        item = self.items()[0]
        self.assertIsNone(item["verified_by"])
        self.assertEqual([e["step"] for e in item["events"]], ["install", "verify", "install"])

    def test_optional_item_does_not_block_handover(self):
        for i in self.items():
            if i["required"]:
                self.complete(i["id"])
        optional = [i for i in self.items() if not i["required"]][0]
        self.assertEqual(optional["state"], "pending")
        self.assertEqual(app.get_vehicle(self.conn, self.vid)["stage"], "ready")

    def test_handover_flow(self):
        items = self.items()
        required = [i for i in items if i["required"]]
        for i in required[:-1]:
            self.complete(i["id"])
        with self.assertRaises(app.StepError):
            app.handover(self.conn, self.vid, "우리팀")
        self.complete(required[-1]["id"])
        app.handover(self.conn, self.vid, "우리팀")
        v = app.get_vehicle(self.conn, self.vid)
        self.assertEqual(v["stage"], "handed_over")
        with self.assertRaises(app.StepError):
            app.handover(self.conn, self.vid, "우리팀")
        optional = [i for i in items if not i["required"]][0]
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, optional["id"], "install", "pass", "A")


class CycleTest(_Base):
    """개조 → 점검 → 광주 출고 → 고장 → 점검 → (개조) → 출고 사이클."""

    def release(self):
        for i in self.items():
            if i["required"] and i["state"] != "confirmed":
                if i["state"] in ("pending", "rework"):
                    app.apply_step(self.conn, i["id"], "install", "pass", "설치자")
                if self.items()[i["seq"]]["state"] == "installed":
                    app.apply_step(self.conn, i["id"], "verify", "pass", "확인자")
                app.apply_step(self.conn, i["id"], "confirm", "pass", "컨펌자")
        app.handover(self.conn, self.vid, "우리팀")

    def stage(self):
        return app.get_vehicle(self.conn, self.vid)["stage"]

    def test_fault_only_after_release(self):
        with self.assertRaises(app.StepError):
            app.report_fault(self.conn, self.vid, "운영", "카메라 영상 끊김")
        self.release()
        with self.assertRaises(app.StepError):
            app.report_fault(self.conn, self.vid, "운영", "")
        app.report_fault(self.conn, self.vid, "운영", "카메라 영상 끊김")
        self.assertEqual(self.stage(), "broken")
        v = app.get_vehicle(self.conn, self.vid)
        self.assertIsNone(v["handover_at"])
        self.assertEqual(v["fault_reason"], "카메라 영상 끊김")

    def test_steps_blocked_while_broken(self):
        self.release()
        app.report_fault(self.conn, self.vid, "운영", "고장")
        with self.assertRaises(app.StepError):
            app.apply_step(self.conn, self.first_item(), "verify", "pass", "X")
        with self.assertRaises(app.StepError):
            app.handover(self.conn, self.vid, "우리팀")

    def test_recheck_validation(self):
        with self.assertRaises(app.StepError):
            app.start_recheck(self.conn, self.vid, "점검자", [self.first_item()])
        self.release()
        app.report_fault(self.conn, self.vid, "운영", "고장")
        with self.assertRaises(app.StepError):
            app.start_recheck(self.conn, self.vid, "점검자", [])
        with self.assertRaises(app.StepError):
            app.start_recheck(self.conn, self.vid, "점검자", [99999])
        optional = [i for i in self.items() if not i["required"]][0]
        with self.assertRaises(app.StepError):
            app.start_recheck(self.conn, self.vid, "점검자", [optional["id"]])

    def test_fault_recheck_pass_then_release(self):
        """고장 → 점검 합격 → 바로 출고 (개조 생략)."""
        self.release()
        app.report_fault(self.conn, self.vid, "운영", "카메라 1 영상 없음")
        iid = self.first_item()
        r = app.start_recheck(self.conn, self.vid, "점검자", [iid])
        self.assertEqual(r["cycle"], 2)
        self.assertEqual(self.stage(), "verifying")
        item = self.items()[0]
        self.assertEqual(item["state"], "installed")
        self.assertEqual(item["events"][-1]["step"], "recheck")
        # 다른 항목은 컨펌 상태 유지
        self.assertEqual(self.items()[1]["state"], "confirmed")
        app.apply_step(self.conn, iid, "verify", "pass", "확인자2")
        app.apply_step(self.conn, iid, "confirm", "pass", "컨펌자2")
        self.assertEqual(self.stage(), "ready")
        app.handover(self.conn, self.vid, "우리팀")
        kinds = [h["kind"] for h in app.get_vehicle(self.conn, self.vid)["history"]]
        self.assertEqual(kinds, ["release", "fault", "recheck", "release"])

    def test_fault_recheck_fail_goes_back_to_modify(self):
        """고장 → 점검 불합격 → 재개조 → 점검 → 출고."""
        self.release()
        app.report_fault(self.conn, self.vid, "운영", "라이다 1 데이터 없음")
        iid = self.items()[8]["id"]
        app.start_recheck(self.conn, self.vid, "점검자", [iid])
        app.apply_step(self.conn, iid, "verify", "fail", "확인자2", "커넥터 파손")
        self.assertEqual(self.stage(), "rework")
        app.apply_step(self.conn, iid, "install", "pass", "설치자2")
        app.apply_step(self.conn, iid, "verify", "pass", "확인자2")
        app.apply_step(self.conn, iid, "confirm", "pass", "컨펌자2")
        app.handover(self.conn, self.vid, "우리팀")
        self.assertEqual(self.stage(), "handed_over")
        # 두 번째 고장도 가능 (사이클 반복)
        app.report_fault(self.conn, self.vid, "운영", "재발")
        app.start_recheck(self.conn, self.vid, "점검자", [iid])
        self.assertEqual(app.get_vehicle(self.conn, self.vid)["cycle"], 3)


class MigrationTest(unittest.TestCase):
    def test_old_db_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "old.db")
            with contextlib.closing(app.sqlite3.connect(db)) as c:
                c.execute("CREATE TABLE vehicles (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
                          " created_at TEXT NOT NULL, handover_by TEXT, handover_at TEXT)")
                c.execute("INSERT INTO vehicles (name, created_at) VALUES ('1호차', 'x')")
                c.commit()
            app.init_db(db)
            with contextlib.closing(app.connect(db)) as c:
                self.assertEqual(c.execute("SELECT cycle FROM vehicles").fetchone()[0], 1)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = app.make_server("127.0.0.1", 0, str(Path(cls.tmp.name) / "h.db"))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with contextlib.closing(urllib.request.urlopen(req)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_api_flow(self):
        names = ",".join(f"{i}호차" for i in range(1, 61))
        status, r = self.call("/api/vehicles", {"names": names})
        self.assertEqual((status, len(r["created"])), (200, 60))
        status, vehicles = self.call("/api/vehicles")
        self.assertEqual(len(vehicles), 60)
        self.assertTrue(all(v["stage"] == "installing" for v in vehicles))

        _, v = self.call("/api/vehicles/1")
        iid = v["items"][0]["id"]
        self.assertEqual(self.call(f"/api/items/{iid}/steps",
                                   {"step": "install", "result": "pass", "person": "A"})[0], 200)
        status, err = self.call(f"/api/items/{iid}/steps",
                                {"step": "verify", "result": "pass", "person": "A"})
        self.assertEqual(status, 400)
        self.assertIn("설치자", err["error"])
        self.assertEqual(self.call("/api/vehicles/1/handover", {"person": "우리팀"})[0], 400)
        self.assertEqual(self.call("/api/vehicles/999")[0], 404)
        self.assertEqual(self.call("/api/vehicles/1/fault", {"person": "운영", "reason": "x"})[0], 400)
        self.assertEqual(self.call("/api/vehicles/1/recheck", {"person": "p", "item_ids": [iid]})[0], 400)

    def test_static_and_traversal(self):
        with contextlib.closing(urllib.request.urlopen(self.base + "/")) as r:
            self.assertIn("인계 체크", r.read().decode())
        status, _ = self.call("/static/../app.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
