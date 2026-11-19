"""[FIX-C28] Operator feedback to the GM: a lesson about an incident type, with WHY and the evidence.

The lesson goes into the pool GM's own memory (gm_society_gm.json), so it carries across games, campaigns
(I30 --gm-memory-from, prepare_scenario) and successors (who read that memory). Write it only while no game runs.

  python -B tools/gm_feedback.py --memory runs/<campaign>/agent-memory --list
  python -B tools/gm_feedback.py --memory runs/<campaign>/agent-memory --kind player_cut --prefer npc --avoid wait \
      --why "..." --evidence "..."
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GM_Agent_Society"))
import gm_memory  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", required=True, type=Path, help="agent-memory directory holding the pool GM's file")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--kind")
    ap.add_argument("--prefer")
    ap.add_argument("--avoid", action="append", default=[])
    ap.add_argument("--why")
    ap.add_argument("--evidence")
    args = ap.parse_args(argv)
    path = args.memory / ("gm_" + gm_memory.POOL_GM.replace(":", "_") + ".json")
    if not path.exists():
        sys.exit(f"no pool GM memory at {path}")
    mem = gm_memory.GMMemory(gm_memory.POOL_GM, directory=args.memory)
    if not args.list:
        if not (args.kind and args.why):
            sys.exit("--kind and --why are required (a lesson without its reason teaches nothing)")
        row = mem.add_lesson(args.kind, args.why, prefer=args.prefer, avoid=args.avoid, evidence=args.evidence)
        mem.save()
        print("lesson added:", json.dumps(row, ensure_ascii=False))
    for l in mem.lessons():
        print(("ACTIVE " if l.get("active", True) else "old    ") + json.dumps(l, ensure_ascii=False))


if __name__ == "__main__":
    main()
