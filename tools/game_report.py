"""Part 1 of the game report template (docs/게임보고서_양식_초안.md) for one game, from its logs.

    PYTHONUTF8=1 python -B tools/game_report.py runs/<campaign> <seed> [--out docs/x.md]

Sources: batch.json (order, conditions), runs/<game>/summary.json, full-record.json (engine state
and events), codex-calls.jsonl (what each agent saw and chose), agent-memory/*.json (final scores
the engine fed back to each agent). Prints markdown (or writes it with --out).

'남은 조합' is accuse_math.combinations_left in the agent's own deduction prompt (v10+: suspects x weapons x places
left in belief.remaining of that prompt), i.e. the number
the agent itself saw (same measure as tools/review_campaign.py).
"""
import collections
import json
import statistics as st
import sys
from pathlib import Path

TYPE_KO = {"explore": "탐색", "question": "질문", "trade": "거래", "prepare": "준비", "pass": "통과", "move": "이동",
           "accuse": "추리 도전"}
CAT_KO = {"suspect": "인물", "weapon": "흉기", "place": "장소"}


def short(aid):
    return aid.split(":")[-1][:6]


def load(camp, seed):
    b = json.loads((camp / "batch.json").read_text(encoding="utf-8"))
    done = [a for a in b["attempts"] if a["status"] == "completed"]
    attempt = next(a for a in done if a["seed"] == seed)
    order = 1 + sorted(a["seed"] for a in done).index(seed)
    summary = json.loads(Path(attempt["summary_file"]).read_text(encoding="utf-8"))
    gdir = Path(attempt["full_record_file"]).parent
    record = json.loads((gdir / "full-record.json").read_text(encoding="utf-8"))
    calls = []
    usage = collections.Counter()
    log = gdir / "codex-calls.jsonl"   # absent for fake-model runs: the per-agent sections are then empty
    for line in (open(log, encoding="utf-8") if log.exists() else []):
        x = json.loads(line)
        u = x.get("usage") or {}
        usage["input"] += u.get("input") or 0
        usage["output"] += u.get("output") or 0
        try:
            u, r = json.loads(x["user"]), json.loads(x["response"])
        except Exception:
            continue
        if isinstance(u, dict) and isinstance(r, dict):
            calls.append((u, r, x.get("usage") or {}))
    feedback = {}
    for f in (camp / "agent-memory").glob("*.json"):
        if f.name.startswith("gm_"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        for g in d.get("games") or []:
            if g.get("game_id") == record["game_id"]:
                feedback[d["agent_id"]] = g.get("feedback") or {}
    # the engine's own final feedback covers every player (also those who sat the game out)
    feedback.update(record["game"].get("final_feedback") or {})
    return b, attempt, order, summary, record, calls, usage, feedback


def pool_agents(camp):
    """Agent cards from the campaign's pool DB (MBTI, GM stand-in credits); empty when there is none."""
    import sqlite3
    path = camp / "pool.sqlite3"
    if not path.exists():
        return {}
    out = {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for aid, state in db.execute("select agent_id, state from agents"):
            out[aid] = json.loads(state)
    finally:
        db.close()
    return out


def report(camp, seed):
    b, attempt, order, s, rec, calls, usage, feedback = load(camp, seed)
    g = rec["game"]
    cond = attempt.get("conditions") or {}
    chars = {int(k): v for k, v in g["characters"].items()}
    by_agent = {p["agent_id"]: p for p in g["players"].values()}
    char_of = {aid: p["character_id"] for aid, p in by_agent.items()}
    agent_of = {cid: aid for aid, cid in char_of.items()}   # a corpse's character has no controller any more
    cards = pool_agents(camp)
    shared = collections.Counter()                          # post-game sharing: + received, - given
    for giver, sh in (g.get("sharing") or {}).items():
        for x in sh.get("shares") or []:
            shared[x["agent_id"]] += x["points"]
            shared[giver] -= x["points"]
    out = []
    w = out.append
    winners = s.get("winners") or []
    deaths = s.get("deaths") or []

    # per cycle, per character: what the agent saw and chose
    combos = collections.defaultdict(dict)          # cycle -> character -> combinations_left
    acts = collections.defaultdict(collections.Counter)
    accuses = []
    reasons = collections.defaultdict(list)          # cycle -> (character, action, consideration)
    for u, r, _ in calls:
        me = (u.get("me") or {}).get("character_id")
        cyc = u.get("cycle")
        if u.get("phase") == "deduction":
            am = u.get("accuse_math") or {}
            left = am.get("combinations_left")
            if left is None:   # v10+: no longer in accuse_math; the agent's own belief.remaining in the same prompt
                bel = u.get("belief") or {}
                sizes = [len((bel.get(k) or {}).get("remaining") or []) for k in ("suspect", "weapon", "place")]
                left = sizes[0] * sizes[1] * sizes[2] if all(k in bel for k in ("suspect", "weapon", "place")) else None
            if left is not None:
                combos[cyc][me] = left
            if (r.get("action") or {}).get("type") == "accuse":
                accuses.append((cyc, me, left, r.get("consideration") or r.get("reason") or ""))
        elif u.get("phase") == "interaction":
            t = (r.get("action") or {}).get("type")
            acts[cyc][t] += 1
            if t in ("trade", "question") and len(reasons[cyc]) < 40:
                reasons[cyc].append((me, t, str(r.get("consideration") or "")[:120]))
    trades = list(g["trades"].values())
    settled = collections.Counter(t["cycle"] for t in trades if t["status"] == "settled")
    proposed = collections.Counter(t["cycle"] for t in trades)
    missions_by_cycle = collections.Counter()
    for p in g["players"].values():
        for m in p.get("missions") or []:
            if m.get("achieved"):
                missions_by_cycle[m.get("achieved_cycle")] += 1

    # ---- 0
    w(f"## 0. 개요 (시드 {seed})\n")
    w("| 항목 | 값 |\n|---|---|")
    w(f"| 게임 | `{rec['game_id']}` · 캠페인 `{camp.name}` · {order}번째 판 · 시드 {seed} |")
    w(f"| 조건 | 마감 {cond.get('deadline')}사이클 · 플레이어 {len(g['players'])}명 · 모델 {cond.get('model')} · "
      f"상호작용 `{cond.get('interaction_order')}` · 단서 흐름 `{cond.get('clue_flow')}` |")
    res = (f"정답자 {len(winners)}명" if winners else "정답자 없음") + f", {s['cycles']}사이클 종료 ({s['resolution']})"
    if deaths:
        res += f", 오답 사망 {len(deaths)}명"
    w(f"| 결과 | {res} |")
    w(f"| 규모 | 소요 {round(s['elapsed_seconds'] / 60)}분 · 모델 호출 {s['model_calls']}회 · "
      f"입력 {usage['input'] / 1e6:.1f}M / 출력 {usage['output'] / 1e6:.2f}M 토큰 |\n")
    final = [v for v in combos.get(s["cycles"], {}).values()]
    first = [v for v in combos.get(1, {}).values()]
    if first and final:
        w(f"> 남은 조합 중앙값: 1사이클 {st.median(first):g} → 마지막 {st.median(final):g} (최소 {min(final)}). "
          f"거래 제안 {len(trades)}건 중 성사 {sum(settled.values())}건. 추리 도전 {len(accuses)}건.\n")

    # ---- 1.2
    w("## 1. 배정표\n")
    w("| 캐릭터 | 직업 · 스킬 | 관찰/화술/위압 | 에이전트 | MBTI | 서브 미션 (달성) |\n|---|---|---|---|---|---|")
    for cid in sorted(chars):
        c = chars[cid]
        aid = c.get("controller") or agent_of.get(cid, "")
        p = by_agent.get(aid) or {}
        ms = " / ".join(f"{m['mission_id']}{'✓' if m.get('achieved') else ''}" for m in p.get("missions") or [])
        st_ = c.get("stats") or {}
        skill = (c.get("skill") or {}).get("effect", "")
        w(f"| {cid} {c['name']} | {c['job']} · {skill} | {st_.get('observe')}/{st_.get('speech')}/{st_.get('pressure')} | "
          f"`{short(aid)}` | {c.get('mbti')} | {ms or '-'} |")
    w("")

    # ---- 2
    w("## 2. 사건 설정\n")
    per_room = collections.defaultdict(collections.Counter)
    clues = g["clues"] if isinstance(g["clues"], list) else list(g["clues"].values())
    for c in clues:
        per_room[c.get("source_room")][c.get("kind")] += 1
    w("| 칸 | " + " | ".join(str(r) for r in sorted(k for k in per_room if k is not None)) + " |")
    w("|---|" + "---|" * len([k for k in per_room if k is not None]))
    for kind in ("definite", "indirect"):
        w(f"| {'확실' if kind == 'definite' else '간접'} | " +
          " | ".join(str(per_room[r][kind]) for r in sorted(k for k in per_room if k is not None)) + " |")
    w("\n정답은 4장에서 공개합니다.\n")

    # ---- 3
    w("## 3. 전개 (사이클별)\n")
    w("| 사이클 | 탐색 | 질문 | 거래 행동 | 거래 제안 | 성사 | 미션 달성 | 남은 조합 중앙값 (최소) |\n|---|---|---|---|---|---|---|---|")
    for cyc in range(1, s["cycles"] + 1):
        a = acts.get(cyc, {})
        cb = list(combos.get(cyc, {}).values())
        cbs = f"{st.median(cb):g} ({min(cb)})" if cb else "-"
        w(f"| {cyc} | {a.get('explore', 0)} | {a.get('question', 0)} | {a.get('trade', 0)} | {proposed.get(cyc, 0)} | "
          f"{settled.get(cyc, 0)} | {missions_by_cycle.get(cyc, 0)} | {cbs} |")
    w("")
    # turning points: biggest drop in median combinations, most trades settled
    meds = {c: st.median(v.values()) for c, v in combos.items() if v}
    drops = sorted(((meds[c - 1] - meds[c], c) for c in meds if c - 1 in meds), reverse=True)[:2]

    w("**전환점**\n")
    for rank, (d, c) in enumerate(drops):
        w(f"- {c}사이클: 남은 조합 중앙값이 {meds[c - 1]:g} → {meds[c]:g}로 {'가장' if rank == 0 else '두 번째로'} 크게 줄었다.")
    if settled:
        top = max(settled.values())
        cycles = sorted(c for c, n in settled.items() if n == top)
        w(f"- 거래 성사가 가장 많았던 사이클: {', '.join(map(str, cycles))} (각 {top}건)")
    for cyc, me, left, why in accuses:
        w(f"- {cyc}사이클: {me} {chars.get(me, {}).get('name', '')}가 남은 조합 {left}개에서 추리 도전. 이유: \"{str(why)[:160]}\"")
    if drops:
        c = drops[0][1]
        sample = reasons.get(c - 1, [])[:3] + reasons.get(c, [])[:3]
        if sample:
            w(f"\n그 무렵 판단 이유(발췌, {c - 1}~{c}사이클):\n")
            for me, t, why in sample:
                w(f"- {me} {chars.get(me, {}).get('name', '')} · {TYPE_KO.get(t, t)}: \"{why}\"")
    w("")

    # ---- 4
    truth = g.get("truth") or {}
    w("## 4. 최종 결과\n")
    w(f"정답: 인물 {truth.get('suspect')}, 흉기 {truth.get('weapon')}, 장소 {truth.get('place')}\n")
    w("| 인물 (MBTI) | 결과 | 마지막 남은 조합 | 확실 단서 | 미션 달성 | 게임 점수 | 판 뒤 분배 | 최종 점수 |\n"
      "|---|---|---|---|---|---|---|---|")
    last = {}                                   # character -> (cycle, combinations) of its last deduction seen
    for cyc in sorted(k for k in combos if k is not None):
        for cid, v in combos[cyc].items():
            last[cid] = (cyc, v)
    rows = []
    for aid, p in by_agent.items():
        cid = p["character_id"]
        c = chars[cid]
        fb = feedback.get(aid, {})
        definite = sum(x.get("kind") == "definite" for x in (c.get("information") or {}).values())
        if fb.get("outcome") == "not_counted":
            result = ("집계 제외(GM 승계)" if "stood in" in str(fb.get("not_counted_reason"))
                      else "집계 제외(끊긴 뒤 NPC)")
        elif aid in deaths:
            result = "사망(오답)"
        elif any(v.get("agent_id") == aid for v in winners):
            result = "정답"
        else:
            result = "생존"
        lc = last.get(cid)
        left = "-" if lc is None else (f"{lc[1]}" if lc[0] == s["cycles"] else f"{lc[1]} ({lc[0]}사이클)")
        score = fb.get("final_score")
        total = None if score is None else score + shared.get(aid, 0)
        rows.append((total if total is not None else -999,
                     f"| {cid} {c['name']} ({c.get('mbti')}) | {result} | {left} | {definite} | "
                     f"{sum(bool(m.get('achieved')) for m in p.get('missions') or [])} | {'-' if score is None else score} | "
                     f"{shared.get(aid, 0):+d} | {'-' if total is None else total} |"))
    for _, line in sorted(rows, key=lambda x: -x[0]):
        w(line)
    w("\n'마지막 남은 조합'은 그 인물이 마지막으로 추리 판단을 한 사이클의 값이다(마지막 사이클이 아니면 괄호에 사이클). "
      "'판 뒤 분배'는 1위 정답자가 나눠 준 점수(받으면 +, 준 사람은 −)이고, 최종 점수 = 게임 점수 + 판 뒤 분배다.\n")
    # [A61/A62] disconnection handling seen in this game
    stand_ins = g.get("gm_stand_ins") or []
    if stand_ins:
        w("**GM 승계** (원래 GM이 끊겨 대신 맡은 에이전트, 그 판은 자기 평균 점수로 인정)\n")
        for aid in stand_ins:
            card = cards.get(aid) or {}
            credit = next((x for x in card.get("gm_stand_in") or [] if x.get("game_id") == g["id"]), None)
            how = ("인정 기록 없음(규칙 꺼짐)" if credit is None else
                   f"{credit['final_score']}점 인정 ({'자기 평균' if credit['score_basis'] == 'own_average' else '과거 기록 없음: 그 판 평균'})")
            w(f"- {card.get('fixed_mbti', short(aid))}: {how}")
        w("")
    lost = g.get("cycles_lost_to_waiting")
    if lost:
        w(f"**GM 기다림 비용**: 기다림 결정으로 마감 {lost}사이클을 잃었다(마감 {g.get('deadline_original')} → {g['rules']['deadline_cycles']}).\n")
    return "\n".join(out)


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    text = report(Path(argv[0]), int(argv[1]))
    if "--out" in argv:
        Path(argv[argv.index("--out") + 1]).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main(sys.argv[1:])
