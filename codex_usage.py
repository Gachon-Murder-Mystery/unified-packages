"""Codex quota and campaign token usage.

    python codex_usage.py                                  # current Codex quota, once
    python codex_usage.py --watch runs/arm-20c             # log every 10 min until the campaign stops

Codex reports quota as a percentage of a time window (Pro: weekly), not as tokens, so the
watcher also sums the tokens the campaign actually sent (runs/<game>/codex-calls.jsonl).
Samples go to <campaign>/usage.csv; noteworthy changes (quota %, game finished/failed,
campaign paused/finished) go to <campaign>/usage-events.log and stdout.
"""
import argparse
import csv
import datetime as dt
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from offline_llm import find_codex

ROOT = Path(__file__).resolve().parent
KST = dt.timezone(dt.timedelta(hours=9))


def now():
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def quota(executable=None, timeout=30):
    """account/rateLimits/read through `codex app-server` (the JSON-RPC the Codex app uses)."""
    proc = subprocess.Popen([executable or find_codex(), "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def reply(request_id):
        end = time.time() + timeout
        while time.time() < end:
            try:
                message = json.loads(lines.get(timeout=max(0.1, end - time.time())))
            except (queue.Empty, ValueError):
                continue
            if message.get("id") == request_id:
                return message
        raise TimeoutError("codex app-server did not answer")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-usage", "version": "1"}}})
        reply(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        result = reply(2)["result"]
    finally:
        proc.kill()
    limits = result["rateLimits"]
    primary = limits.get("primary") or {}
    return {"used_percent": primary.get("usedPercent"),
            "window_days": round((primary.get("windowDurationMins") or 0) / 1440, 1),
            "resets_at": dt.datetime.fromtimestamp(primary["resetsAt"], KST).strftime("%Y-%m-%d %H:%M")
            if primary.get("resetsAt") else None,
            "credits": (limits.get("credits") or {}).get("balance"),
            "plan": limits.get("planType"),
            "reset_credits": (result.get("rateLimitResetCredits") or {}).get("availableCount"),
            "limit_reached": limits.get("rateLimitReachedType")}


def campaign(directory):
    """Progress and tokens of one campaign directory."""
    manifest = json.loads((directory / "batch.json").read_text(encoding="utf-8"))
    attempts = manifest["attempts"]
    started = min((a["started_at"] for a in attempts), default=time.time())
    calls = tokens_in = tokens_out = 0
    for path in (ROOT / "runs").glob("*/codex-calls.jsonl"):
        if path.stat().st_mtime < started:
            continue  # belongs to an earlier run
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                usage = json.loads(line).get("usage") or {}
            except ValueError:
                continue
            calls += 1
            tokens_in += usage.get("input") or 0
            tokens_out += usage.get("output") or 0
    last = attempts[-1] if attempts else {}
    return {"completed": sum(a["status"] == "completed" for a in attempts),
            "target": manifest.get("target_count"), "last_seed": last.get("seed"),
            "last_status": last.get("status"), "calls": calls,
            "input_tokens": tokens_in, "output_tokens": tokens_out}


def watch(directory, interval):
    directory = Path(directory).resolve()
    csv_path, events = directory / "usage.csv", directory / "usage-events.log"
    fields = ["time", "used_percent", "credits", "resets_at", "completed", "target", "last_seed",
              "last_status", "calls", "input_tokens", "output_tokens"]
    new = not csv_path.exists()
    previous = {}

    def event(text):
        line = f"{now()} {text}"
        with events.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)

    while True:
        try:
            q = quota()
        except Exception as exc:  # one failed probe must not end the watch
            q = {}
            event(f"quota probe failed: {type(exc).__name__}: {exc}")
        c = campaign(directory)
        row = {"time": now(), **{k: q.get(k) for k in ("used_percent", "credits", "resets_at")}, **c}
        with csv_path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if new:
                writer.writeheader()
                new = False
            writer.writerow(row)
        if not previous:
            event(f"watch started: quota {row['used_percent']}% used (resets {row['resets_at']}), "
                  f"games {c['completed']}/{c['target']}, calls {c['calls']}")
        else:
            if row["used_percent"] != previous["used_percent"]:
                event(f"quota {previous['used_percent']}% -> {row['used_percent']}% "
                      f"(games {c['completed']}, calls {c['calls']}, input {c['input_tokens']:,} tokens)")
            if c["completed"] != previous["completed"]:
                event(f"game finished: {c['completed']}/{c['target']} done, quota {row['used_percent']}%, "
                      f"calls {c['calls']}, input {c['input_tokens']:,} / output {c['output_tokens']:,} tokens")
            if q.get("limit_reached"):
                event(f"RATE LIMIT REACHED: {q['limit_reached']}")
        if c["last_status"] in ("failed", "interrupted"):
            event(f"CAMPAIGN PAUSED: seed {c['last_seed']} {c['last_status']} (see its stderr.log); quota {row['used_percent']}%")
            return
        if c["target"] and c["completed"] >= c["target"]:
            event(f"CAMPAIGN FINISHED: {c['completed']}/{c['target']}, quota {row['used_percent']}%, "
                  f"calls {c['calls']}, input {c['input_tokens']:,} tokens")
            return
        previous = row
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", type=Path, help="campaign directory to follow")
    parser.add_argument("--interval", type=int, default=600, help="seconds between samples")
    args = parser.parse_args()
    if args.watch:
        watch(args.watch, args.interval)
    else:
        print(json.dumps(quota(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
