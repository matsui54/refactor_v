#!/usr/bin/env python3
"""
vwdb_cli.py: AIエージェント用 波形採取CLIクライアント
Usage:
  vwdb_cli.py list --scope tb.u_core.u_lsu
  vwdb_cli.py find --sig tb.u_core.u_lsu.state --val WAIT
  vwdb_cli.py slice --clk tb.clk --center 10220 --cycles 5 --sigs req_valid,req_ready,state,addr
"""
import sys
import json
import socket
import argparse

SOCKET_PATH = "/tmp/vwdb_agent.sock"

def query_daemon(payload: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCKET_PATH)
            s.sendall(json.dumps(payload).encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"Error: Cannot connect to daemon at {SOCKET_PATH}. Is vwdb_daemon.py running?", file=sys.stderr)
        sys.exit(1)

    res = json.loads(b"".join(chunks).decode("utf-8"))
    if not res.get("ok", False):
        print(f"Daemon error: {res.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)
    return res

def print_delta_slice(rows: list, sigs: list, center_time: int):
    headers = ["Cy", "Time"] + sigs
    print(" | ".join(f"{h:<10}" for h in headers))
    print("-+-".join("-" * 10 for _ in headers))

    prev_vals = None
    idle_count = 0
    for r in rows:
        vals = r["vals"]
        if prev_vals is not None and vals == prev_vals and r["time"] != center_time:
            idle_count += 1
            continue
        if idle_count > 0:
            print(f" ... ({idle_count} cycles unchanged) ...")
            idle_count = 0

        cells = [f"{r['cy']:<10}", f"{r['time']:<10}"]
        for s in sigs:
            v = str(vals.get(s, "?"))
            # 前サイクルと同じ値は '.' に置き換えてトークンを節約
            cells.append(f"{'.' if (prev_vals and prev_vals.get(s) == v) else v:<10}")
        marker = " <-- CENTER" if r["time"] == center_time else ""
        print(" | ".join(cells) + marker)
        prev_vals = vals.copy()

    if idle_count > 0:
        print(f" ... ({idle_count} cycles unchanged) ...")

def main():
    ap = argparse.ArgumentParser(description="Waveform query CLI for AI Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--scope", required=True)

    p_find = sub.add_parser("find")
    p_find.add_argument("--sig", required=True)
    p_find.add_argument("--val", required=True)

    p_slice = sub.add_parser("slice")
    p_slice.add_argument("--clk", required=True)
    p_slice.add_argument("--center", type=int, required=True)
    p_slice.add_argument("--cycles", type=int, default=10)
    p_slice.add_argument("--sigs", required=True, help="Comma-separated signal names")

    args = ap.parse_args()
    if args.cmd == "list":
        res = query_daemon({"cmd": "list", "scope": args.scope})
        for s in res.get("data", []):
            print(f"{s['name']} [{s['width']}b] ({s['type']})")
    elif args.cmd == "find":
        res = query_daemon({"cmd": "find", "sig": args.sig, "val": args.val})
        print(f"Matched timestamps: {res.get('data', {}).get('timestamps', [])}")
    elif args.cmd == "slice":
        sig_list = [x.strip() for x in args.sigs.split(",")]
        res = query_daemon({"cmd": "slice", "clk": args.clk, "sigs": sig_list, "center": args.center, "cycles": args.cycles})
        print_delta_slice(res.get("data", []), sig_list, args.center)

if __name__ == "__main__":
    main()
