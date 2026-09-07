#!/usr/bin/env python3
"""Print local arrivals as JSON lines and persist the delivery checkpoint.

Run collect separately. This example's handling step is printing, not replying.
Replace deliver() with your agent's completed work before advancing the checkpoint.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def deliver(message):
    print(json.dumps(message, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="Drain current arrivals, then exit")
    args = parser.parse_args()
    after = int(args.checkpoint.read_text()) if args.checkpoint.exists() else 0
    while True:
        result = subprocess.run([sys.executable, "-m", "boardmail", "--db", args.db,
            "wait", "--after", str(after), "--timeout", "0" if args.once else "60"],
            capture_output=True, text=True)
        value = json.loads(result.stdout)
        if result.returncode not in (0, 3):
            print(json.dumps(value), file=sys.stderr)
            return result.returncode
        if value["event"] == "timeout":
            print(json.dumps({"event": "timeout", "sources": value["sources"],
                              "history_complete": False}), file=sys.stderr)
            if args.once: return 0
            continue
        for message in value["messages"]: deliver(message)
        after = value["next_after"]
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.checkpoint.with_name(args.checkpoint.name+".tmp")
        temporary.write_text(str(after)+"\n")
        temporary.replace(args.checkpoint)


if __name__ == "__main__": raise SystemExit(main())
