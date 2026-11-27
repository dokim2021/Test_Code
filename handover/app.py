"""NE-PE-X 개조 차량 인계 체크 서버.

Python 표준 라이브러리만 사용한다 (추가 설치 없음).
실행: python3 app.py [--host 0.0.0.0] [--port 8000] [--db handover.db]
"""
import argparse
import contextlib
import json
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_BODY = 1024 * 1024

# (그룹, 항목명, 인계 필수 여부)
CHECKLIST = (
    [("camera", f"카메라 {i}", True) for i in range(1, 9)]
    + [("lidar", f"라이다 {i}", True) for i in range(1, 9)]
    + [
        ("thor", "Thor 설치", True),
        ("thor", "Thor - 로깅 시스템", True),
        ("thor", "Thor - 알고리즘 동작", True),
        ("thor", "Thor - 센서 연결", True),
        ("orin", "Orin 설치", True),
        ("orin", "Orin - 뷰어", True),
        ("orin", "Orin - 로깅 애플리케이션", True),
        ("orin", "Orin - 업로더", True),
        ("orin", "Orin - 관제 시스템 (관제팀 담당)", False),
    ]
)

# 단계별 허용 상태 → (합격 시 상태, 불합격 시 상태)
STEPS = {
    "install": ({"pending", "rework"}, "installed", None),
    "verify": ({"installed"}, "verified", "rework"),
    "confirm": ({"verified"}, "confirmed", "rework"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    handover_by TEXT,
    handover_at TEXT,
    fault_by TEXT,
    fault_at TEXT,
    fault_reason TEXT,
    cycle INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS vehicle_events (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    kind TEXT NOT NULL,
    person TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    cycle INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    seq INTEGER NOT NULL,
    grp TEXT NOT NULL,
    name TEXT NOT NULL,
    required INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    installed_by TEXT,
    verified_by TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    step TEXT NOT NULL,
    result TEXT NOT NULL,
    person TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_vehicle ON items(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_events ON vehicle_events(vehicle_id);
"""

# 이전 버전 DB 에 없는 컬럼
MIGRATIONS = {
    "fault_by": "ALTER TABLE vehicles ADD COLUMN fault_by TEXT",
    "fault_at": "ALTER TABLE vehicles ADD COLUMN fault_at TEXT",
    "fault_reason": "ALTER TABLE vehicles ADD COLUMN fault_reason TEXT",
    "cycle": "ALTER TABLE vehicles ADD COLUMN cycle INTEGER NOT NULL DEFAULT 1",
}


class StepError(ValueError):
    """규칙 위반 (HTTP 400)."""


class NotFound(LookupError):
    """대상 없음 (HTTP 404)."""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    with contextlib.closing(connect(db_path)) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(vehicles)")}
        for col, sql in MIGRATIONS.items():
            if cols and col not in cols:
                conn.execute(sql)
        conn.executescript(SCHEMA)


def create_vehicles(conn, names):
    """차량 등록. 이미 있는 이름은 건너뛴다."""
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        raise StepError("등록할 차량 이름이 없습니다.")
    created, skipped = [], []
    ts = now()
    for name in dict.fromkeys(names):
        if conn.execute("SELECT 1 FROM vehicles WHERE name = ?", (name,)).fetchone():
            skipped.append(name)
            continue
        vid = conn.execute(
            "INSERT INTO vehicles (name, created_at) VALUES (?, ?)", (name, ts)
        ).lastrowid
        conn.executemany(
            "INSERT INTO items (vehicle_id, seq, grp, name, required, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, i, g, n, int(r), ts) for i, (g, n, r) in enumerate(CHECKLIST)],
        )
        created.append(name)
    return {"created": created, "skipped": skipped}


def vehicle_stage(items, handed_over, broken=False):
    required = [i for i in items if i["required"]]
    if broken:
        return "broken"
    if handed_over:
        return "handed_over"
    if all(i["state"] == "confirmed" for i in required):
        return "ready"
    if any(i["state"] == "rework" for i in items):
        return "rework"
    if any(i["state"] == "pending" for i in required):
        return "installing"
    if any(i["state"] == "installed" for i in required):
        return "verifying"
    return "confirming"


def _summary(vehicle, items):
    groups = {}
    for i in items:
        g = groups.setdefault(i["grp"], {"done": 0, "total": 0})
        if i["required"]:
            g["total"] += 1
            g["done"] += i["state"] == "confirmed"
    required = [i for i in items if i["required"]]
    return {
        "id": vehicle["id"],
        "name": vehicle["name"],
        "handover_by": vehicle["handover_by"],
        "handover_at": vehicle["handover_at"],
        "fault_by": vehicle["fault_by"],
        "fault_at": vehicle["fault_at"],
        "fault_reason": vehicle["fault_reason"],
        "cycle": vehicle["cycle"],
        "stage": vehicle_stage(
            items, vehicle["handover_at"] is not None, vehicle["fault_at"] is not None
        ),
        "confirmed": sum(i["state"] == "confirmed" for i in required),
        "required": len(required),
        "rework": sum(i["state"] == "rework" for i in items),
        "groups": groups,
    }


def list_vehicles(conn):
    vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
    items_by_vehicle = {}
    for row in conn.execute("SELECT * FROM items ORDER BY vehicle_id, seq"):
        items_by_vehicle.setdefault(row["vehicle_id"], []).append(dict(row))
    return [_summary(v, items_by_vehicle.get(v["id"], [])) for v in vehicles]


def get_vehicle(conn, vehicle_id):
    vehicle = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    if vehicle is None:
        raise NotFound("차량을 찾을 수 없습니다.")
    items = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM items WHERE vehicle_id = ? ORDER BY seq", (vehicle_id,)
        )
    ]
    events = {}
    for e in conn.execute(
        "SELECT e.* FROM events e JOIN items i ON i.id = e.item_id"
        " WHERE i.vehicle_id = ? ORDER BY e.id",
        (vehicle_id,),
    ):
        events.setdefault(e["item_id"], []).append(dict(e))
    for i in items:
        i["events"] = events.get(i["id"], [])
    result = _summary(vehicle, items)
    result["items"] = items
    result["history"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM vehicle_events WHERE vehicle_id = ? ORDER BY id", (vehicle_id,)
        )
    ]
    return result


def _log_vehicle(conn, vehicle_id, kind, person, memo, cycle, ts):
    conn.execute(
        "INSERT INTO vehicle_events (vehicle_id, kind, person, memo, cycle, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (vehicle_id, kind, person, memo, cycle, ts),
    )


def apply_step(conn, item_id, step, result, person, memo=""):
    """설치/확인/컨펌 단계를 기록한다. 규칙 위반 시 StepError."""
    person = (person or "").strip()
    memo = (memo or "").strip()
    if step not in STEPS:
        raise StepError("알 수 없는 단계입니다.")
    if result not in ("pass", "fail"):
        raise StepError("결과는 pass 또는 fail 이어야 합니다.")
    if not person:
        raise StepError("작업자 이름을 입력하세요.")
    if step == "install" and result != "pass":
        raise StepError("설치 단계는 완료(pass)만 기록할 수 있습니다.")
    if result == "fail" and not memo:
        raise StepError("불합격 시 사유(메모)를 입력하세요.")

    item = conn.execute(
        "SELECT i.*, v.handover_at, v.fault_at FROM items i"
        " JOIN vehicles v ON v.id = i.vehicle_id"
        " WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise NotFound("항목을 찾을 수 없습니다.")
    if item["handover_at"]:
        raise StepError("이미 광주 출고된 차량입니다.")
    if item["fault_at"]:
        raise StepError("고장 상태입니다. 먼저 점검을 시작하세요.")

    allowed, pass_state, fail_state = STEPS[step]
    if item["state"] not in allowed:
        raise StepError(f"현재 상태({item['state']})에서는 이 단계를 진행할 수 없습니다.")
    if step in ("verify", "confirm") and person == item["installed_by"]:
        raise StepError("설치자는 같은 항목을 확인/컨펌할 수 없습니다.")
    if step == "confirm" and person == item["verified_by"]:
        raise StepError("확인자는 같은 항목을 컨펌할 수 없습니다.")

    new_state = pass_state if result == "pass" else fail_state
    ts = now()
    if step == "install":
        conn.execute(
            "UPDATE items SET state = ?, installed_by = ?, verified_by = NULL,"
            " updated_at = ? WHERE id = ?",
            (new_state, person, ts, item_id),
        )
    elif step == "verify":
        conn.execute(
            "UPDATE items SET state = ?, verified_by = ?, updated_at = ? WHERE id = ?",
            (new_state, person, ts, item_id),
        )
    else:
        conn.execute(
            "UPDATE items SET state = ?, updated_at = ? WHERE id = ?",
            (new_state, ts, item_id),
        )
    conn.execute(
        "INSERT INTO events (item_id, step, result, person, memo, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, step, result, person, memo, ts),
    )
    return {"state": new_state}


def handover(conn, vehicle_id, person):
    """광주 출고(우리 팀 인수) 처리. 필수 항목이 모두 컨펌되어야 한다."""
    person = (person or "").strip()
    if not person:
        raise StepError("인수자 이름을 입력하세요.")
    vehicle = get_vehicle(conn, vehicle_id)
    if vehicle["stage"] == "handed_over":
        raise StepError("이미 광주 출고된 차량입니다.")
    if vehicle["stage"] != "ready":
        raise StepError("필수 항목이 모두 컨펌되어야 출고할 수 있습니다.")
    ts = now()
    conn.execute(
        "UPDATE vehicles SET handover_by = ?, handover_at = ? WHERE id = ?",
        (person, ts, vehicle_id),
    )
    _log_vehicle(conn, vehicle_id, "release", person, "", vehicle["cycle"], ts)
    return {"stage": "handed_over"}


def report_fault(conn, vehicle_id, person, reason):
    """광주 출고된 차량의 고장 등록. 차량은 '고장' 상태가 된다."""
    person = (person or "").strip()
    reason = (reason or "").strip()
    if not person:
        raise StepError("작업자 이름을 입력하세요.")
    if not reason:
        raise StepError("고장 내용을 입력하세요.")
    vehicle = get_vehicle(conn, vehicle_id)
    if vehicle["stage"] != "handed_over":
        raise StepError("광주 출고된 차량만 고장 등록할 수 있습니다.")
    ts = now()
    conn.execute(
        "UPDATE vehicles SET handover_by = NULL, handover_at = NULL,"
        " fault_by = ?, fault_at = ?, fault_reason = ? WHERE id = ?",
        (person, ts, reason, vehicle_id),
    )
    _log_vehicle(conn, vehicle_id, "fault", person, reason, vehicle["cycle"], ts)
    return {"stage": "broken"}


def start_recheck(conn, vehicle_id, person, item_ids):
    """고장 차량의 점검 시작. 선택한 항목을 '확인 대기'로 되돌리고 회차를 올린다."""
    person = (person or "").strip()
    if not person:
        raise StepError("작업자 이름을 입력하세요.")
    vehicle = get_vehicle(conn, vehicle_id)
    if vehicle["stage"] != "broken":
        raise StepError("고장 상태인 차량만 점검을 시작할 수 있습니다.")
    if not isinstance(item_ids, list) or not item_ids:
        raise StepError("다시 점검할 항목을 하나 이상 선택하세요.")
    by_id = {i["id"]: i for i in vehicle["items"]}
    try:
        chosen = [by_id[int(x)] for x in dict.fromkeys(item_ids)]
    except (KeyError, TypeError, ValueError):
        raise StepError("이 차량의 항목이 아닙니다.")
    if any(i["state"] != "confirmed" for i in chosen):
        raise StepError("컨펌 완료된 항목만 다시 점검할 수 있습니다.")
    ts = now()
    cycle = vehicle["cycle"] + 1
    reason = vehicle["fault_reason"] or ""
    for i in chosen:
        conn.execute(
            "UPDATE items SET state = 'installed', verified_by = NULL, updated_at = ?"
            " WHERE id = ?",
            (ts, i["id"]),
        )
        conn.execute(
            "INSERT INTO events (item_id, step, result, person, memo, created_at)"
            " VALUES (?, 'recheck', 'pass', ?, ?, ?)",
            (i["id"], person, f"{cycle}회차 재점검 (고장: {reason})", ts),
        )
    conn.execute(
        "UPDATE vehicles SET fault_by = NULL, fault_at = NULL, fault_reason = NULL,"
        " cycle = ? WHERE id = ?",
        (cycle, vehicle_id),
    )
    names = ", ".join(i["name"] for i in chosen)
    _log_vehicle(conn, vehicle_id, "recheck", person, f"{len(chosen)}개 항목: {names}", cycle, ts)
    return {"stage": "verifying", "cycle": cycle}


class Handler(BaseHTTPRequestHandler):
    db_path = "handover.db"
    write_lock = threading.Lock()

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise StepError("요청이 너무 큽니다.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise StepError("JSON 형식이 올바르지 않습니다.")
        if not isinstance(data, dict):
            raise StepError("JSON 객체가 필요합니다.")
        return data

    def _run(self, fn, write=False):
        try:
            lock = self.write_lock if write else contextlib.nullcontext()
            with lock, contextlib.closing(connect(self.db_path)) as conn, conn:
                result = fn(conn)
            self._send(200, result)
        except StepError as e:
            self._send(400, {"error": str(e)})
        except NotFound as e:
            self._send(404, {"error": str(e)})

    def _static(self, name):
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR or not path.is_file():
            return self._send(404, {"error": "not found"})
        types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
        ctype = types.get(path.suffix, "application/octet-stream") + "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/vehicles":
            return self._run(list_vehicles)
        m = re.fullmatch(r"/api/vehicles/(\d+)", path)
        if m:
            return self._run(lambda c: get_vehicle(c, int(m.group(1))))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except StepError as e:
            return self._send(400, {"error": str(e)})
        if path == "/api/vehicles":
            names = re.split(r"[,\n]", str(body.get("names", "")))
            return self._run(lambda c: create_vehicles(c, names), write=True)
        m = re.fullmatch(r"/api/items/(\d+)/steps", path)
        if m:
            return self._run(
                lambda c: apply_step(
                    c, int(m.group(1)), body.get("step"), body.get("result"),
                    body.get("person"), body.get("memo"),
                ),
                write=True,
            )
        m = re.fullmatch(r"/api/vehicles/(\d+)/handover", path)
        if m:
            return self._run(
                lambda c: handover(c, int(m.group(1)), body.get("person")), write=True
            )
        m = re.fullmatch(r"/api/vehicles/(\d+)/fault", path)
        if m:
            return self._run(
                lambda c: report_fault(c, int(m.group(1)), body.get("person"), body.get("reason")),
                write=True,
            )
        m = re.fullmatch(r"/api/vehicles/(\d+)/recheck", path)
        if m:
            return self._run(
                lambda c: start_recheck(
                    c, int(m.group(1)), body.get("person"), body.get("item_ids")
                ),
                write=True,
            )
        self._send(404, {"error": "not found"})


def make_server(host, port, db_path):
    init_db(db_path)
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    return ThreadingHTTPServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(description="NE-PE-X 개조 차량 인계 체크 서버")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(BASE_DIR / "handover.db"))
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"http://{args.host}:{args.port} (DB: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
