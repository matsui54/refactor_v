#!/usr/bin/env python3
"""
vwdb_daemon.py: Verisium Debug バッチモード上で常駐し、.vwdb クエリを処理するワーカー
"""
import os
import json
import socket

SOCKET_PATH = "/tmp/vwdb_agent.sock"

class VwdbEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # TODO: ここで Verisium Debug API を用いて .vwdb をオープンしハンドルを保持
        # self.db = verisium.open_db(db_path)
        print(f"[Daemon] Loaded waveform DB: {db_path}")

    def list_signals(self, scope: str):
        # TODO: 指定スコープ内の信号名・幅・型一覧をAPIで取得
        return [
            {"name": "req_valid", "width": 1, "type": "logic"},
            {"name": "req_ready", "width": 1, "type": "logic"},
            {"name": "state", "width": 3, "type": "enum(fsm_t)"},
            {"name": "addr", "width": 32, "type": "logic"},
        ]

    def find_condition(self, signal: str, target_val: str, max_hits: int = 10):
        # TODO: APIのトランザクション検索等で signal == target_val となる時刻リストを取得
        return {"signal": signal, "value": target_val, "timestamps": [10210, 10260, 15400][:max_hits]}

    def slice_window(self, clk: str, signals: list, center_time: int, num_cycles: int):
        # TODO: APIで center_time 前後 num_cycles 分のクロック立ち上がり時刻と各信号の値を取得
        # enum型などは数値ではなくシンボル名（"IDLE"等）で取得できるとAIの理解度が向上する
        return [
            {"cy": 1020, "time": 10200, "vals": {"req_valid": "0", "req_ready": "1", "state": "IDLE", "addr": "0x0"}},
            {"cy": 1021, "time": 10210, "vals": {"req_valid": "1", "req_ready": "1", "state": "REQ",  "addr": "0x80001000"}},
            {"cy": 1022, "time": 10220, "vals": {"req_valid": "0", "req_ready": "0", "state": "WAIT", "addr": "0x80001000"}},
            {"cy": 1023, "time": 10230, "vals": {"req_valid": "0", "req_ready": "0", "state": "WAIT", "addr": "0x80001000"}},
            {"cy": 1024, "time": 10240, "vals": {"req_valid": "0", "req_ready": "1", "state": "DONE", "addr": "0x80001000"}},
        ]

def serve(db_path: str):
    engine = VwdbEngine(db_path)
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(SOCKET_PATH)
        server.listen(5)
        print(f"[Daemon] Listening on {SOCKET_PATH}...")
        while True:
            conn, _ = server.accept()
            with conn:
                data = conn.recv(65536).decode("utf-8")
                if not data:
                    continue
                req = json.loads(data)
                cmd = req.get("cmd")
                try:
                    if cmd == "list":
                        res = {"ok": True, "data": engine.list_signals(req["scope"])}
                    elif cmd == "find":
                        res = {"ok": True, "data": engine.find_condition(req["sig"], req["val"], req.get("limit", 10))}
                    elif cmd == "slice":
                        res = {"ok": True, "data": engine.slice_window(req["clk"], req["sigs"], req["center"], req["cycles"])}
                    else:
                        res = {"ok": False, "error": f"Unknown command: {cmd}"}
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                conn.sendall(json.dumps(res).encode("utf-8"))

if __name__ == "__main__":
    import sys
    serve(sys.argv[1] if len(sys.argv) > 1 else "sim.vwdb")
