"""[FIX-C16] Replay a campaign's regression traces (textbook II ch10, regression trace collection).

    python tools/replay_traces.py runs/<campaign>/regression_traces.jsonl [more files ...]

GM incident traces carry the GM views that changed before the incident, the view when it opened
and the stall threshold in force. Each is fed to a fresh IncidentWatch (memory in RAM, recorded
clock) and must be diagnosed as the same incident type again; a mismatch means a code change
altered how the GM reads that situation. Exit code 1 on any mismatch.

Runner records (failed games) and model outages are listed for the record; they are not replayed.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from GM_Agent_Society.gm_memory import GMMemory, IncidentWatch, expand_view  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def replay(record):
    """Returns (ok, diagnosed kinds) for one GM incident trace."""
    clock = Clock()
    memory = GMMemory("replay:gm", directory="")
    threshold = record.get("threshold")
    if threshold:
        memory.stall_threshold = lambda stage: threshold
    watch = IncidentWatch(memory, clock)
    for t, view in record.get("views") or []:
        clock.t = t
        watch.observe(expand_view(view))
    clock.t = record["opened_at"]
    if record.get("open_view"):
        watch.observe(expand_view(record["open_view"]))
    kinds = sorted({inc["kind"] for inc in watch.open.values()})
    return record["kind"] in kinds, kinds


def main(paths):
    failures, listed = 0, Counter()
    for path in paths:
        if not Path(path).exists():   # no incident, no failed game, no outage: nothing was traced
            print(f"no traces: {path}")
            continue
        for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("source") == "gm" and record.get("type") == "incident" and record.get("open_view"):
                ok, kinds = replay(record)
                listed["gm incident replayed"] += 1
                if not ok:
                    failures += 1
                    print(f"MISMATCH {path}:{number}: recorded {record['kind']}, replay diagnosed {kinds or 'nothing'}")
            else:
                listed[f"{record.get('source')} {record.get('type') or record.get('kind')} (listed only)"] += 1
    for key, n in sorted(listed.items()):
        print(f"{n:5d}  {key}")
    print("replay:", "OK" if not failures else f"{failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1:]))
