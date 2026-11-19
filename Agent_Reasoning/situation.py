"""[FIX-B41/B42] One agent's own in-game memory of the situation, apart from the clues.

User decisions 2026-11-09: every agent (and the GM) holds its own memory; information about the
current situation must not be pushed out first-in-first-out regardless of importance (the old
prompt showed the last 8 heard claims: a refusal heard at cycle 5 was gone by cycle 12); what
others say is a basis for *inference* ("가능성"), never certain evidence.

Textbook II ch.6: the rolling context window loses information "regardless of how relevant or
important it is"; a knowledge graph keeps entities and their relations; note-taking lets the
model write its own notes. Here, scaled down:

  SituationMemory  entity cards (one per character): holdings evidence per clue type, what the
                   character did in response to me, what it said (latest per topic), where it was
                   last seen, contradictions. Evidence is superseded, never evicted by count.
      grades       "확인"   the engine guarantees it: a declared trade offer passed the engine's
                           possession check; a clue the character handed me (trade, answer);
                           a clue I handed it (trades copy clues: holders never lose one).
                   "가능성" the character's own words (or nothing better). Kept with the words, the
                           cycle, and who it met since (clues spread by trades).
  Notes            notes the agent writes itself (add / edit / drop / pin). Never dropped
                   automatically: when full, the agent is told to edit or drop one.
"""

TYPE_KO = {"suspect": "인물", "weapon": "무기", "place": "장소", "character_room": "인물 위치",
           "weapon_room": "흉기 위치", "companion": "동행"}
DEFINITE = ("suspect", "weapon", "place")
INDIRECT = ("character_room", "weapon_room", "companion")

# words -> clue type, most specific first ("흉기 위치" before "흉기", "인물 위치" before "인물")
_TOPICS = (("weapon_room", ("흉기 위치", "무기 위치", "흉기의 위치", "무기의 위치")),
           ("character_room", ("인물 위치", "자정 위치", "있던 곳", "있던 칸", "있었던 곳")),
           ("companion", ("동행", "함께 있")),
           ("weapon", ("무기", "흉기")),
           ("place", ("장소", "범행 장소")),
           ("suspect", ("용의자", "인물", "범인")))
_LACK = ("없", "갖고 있지 않", "가지고 있지 않", "못 드", "못 줘", "드릴 수 없", "줄 수 없", "모릅", "몰라")
_HAVE = ("드릴", "드리겠", "줄게", "주겠", "있어요", "있습니다", "있다", "갖고 있", "가지고 있", "제공")

NOTE_CAP = 10
NOTE_PINS = 3
NOTE_LEN = 120
NOTE_OPS_PER_TURN = 3


def topics_of(text):
    """Clue types a sentence talks about (keyword level; the raw words are always kept too)."""
    found, rest = [], text or ""
    for kind, words in _TOPICS:
        for w in words:
            if w in rest:
                found.append(kind)
                rest = rest.replace(w, " ")   # "흉기 위치" must not also count as "흉기"
                break
    return found


def stance_of(text):
    t = text or ""
    if any(w in t for w in _LACK):
        return "없음"
    if any(w in t for w in _HAVE):
        return "있음"
    return None


class SituationMemory:
    def __init__(self):
        self.cards = {}           # character id -> card
        self.contacts = {}        # character id -> [(cycle, other id, how)]

    # ---------- write ----------
    def card(self, cid):
        cid = int(cid) if isinstance(cid, (int, str)) and str(cid).lstrip("-").isdigit() else cid
        return self.cards.setdefault(cid, {"holdings": {}, "trades": [], "questions": [], "asked_me": [],
                                           "said": {}, "last_seen": None, "contradictions": []})

    def evidence(self, cid, kind, state, grade, cycle, why):
        """Supersede, never evict. A confirmed 'has' stays (clues are copied, never lost);
        a later 'possible lack' against it is a contradiction worth remembering."""
        if cid is None or kind not in TYPE_KO:
            return
        c = self.card(cid)
        old = c["holdings"].get(kind)
        new = {"state": state, "grade": grade, "cycle": cycle, "why": why}
        if old and old["grade"] == "확인" and old["state"] == "있음":
            if state == "없음":
                c["contradictions"].append({"cycle": cycle, "what": f"{TYPE_KO[kind]}: {why} ↔ c{old['cycle']} {old['why']}"})
            if grade != "확인":
                return
        if old and grade == "가능성" and old["grade"] == "확인" and old["cycle"] > cycle:
            return
        c["holdings"][kind] = new

    def heard(self, cid, cycle, context, text):
        """Words of a character: latest per topic; each topic it names becomes a '가능성'."""
        if cid is None or not text:
            return
        c = self.card(cid)
        kinds = topics_of(text)
        stance = stance_of(text)
        for key in (kinds or [context or "other"]):
            c["said"][key] = {"cycle": cycle, "context": context, "text": text}
        if stance:
            for kind in kinds:
                self.evidence(cid, kind, stance, "가능성", cycle, f"본인 말: \"{text[:60]}\"")

    def offered(self, cid, kinds, cycle):
        """A declared offer reached me only after the engine checked the declarer holds each type."""
        for kind in kinds or []:
            self.evidence(cid, kind, "있음", "확인", cycle, f"거래에서 {TYPE_KO.get(kind, kind)} 유형을 선언(엔진이 보유 확인)")

    def gave_me(self, cid, kinds, cycle, how):
        for kind in kinds or []:
            self.evidence(cid, kind, "있음", "확인", cycle, f"{how}로 내게 {TYPE_KO.get(kind, kind)} 단서를 줌")

    def got_from_me(self, cid, kinds, cycle):
        for kind in kinds or []:
            self.evidence(cid, kind, "있음", "확인", cycle, f"내가 거래로 {TYPE_KO.get(kind, kind)} 단서를 줌(복사본)")

    def trade(self, cid, entry):
        if cid is not None:
            self.card(cid)["trades"].append(entry)

    def question(self, cid, entry):
        if cid is not None:
            self.card(cid)["questions"].append(entry)

    def asked_me(self, cid, cycle, approach):
        if cid is not None:
            self.card(cid)["asked_me"].append({"cycle": cycle, "approach": approach})

    def placements(self, rows):
        """Public placement per cycle: last seen room and who met whom (clues spread by meeting)."""
        for row in rows or []:
            cycle = row.get("cycle")
            rooms = {}
            for c, r in (row.get("rooms") or {}).items():
                rooms.setdefault(r, []).append(int(c))
            for room, people in rooms.items():
                for p in people:
                    card = self.card(p)
                    if not card["last_seen"] or (card["last_seen"]["cycle"] or 0) <= (cycle or 0):
                        card["last_seen"] = {"room": room, "cycle": cycle}
                    for q in people:
                        if q != p:
                            seen = self.contacts.setdefault(p, [])
                            if (cycle, q, "같은 칸") not in seen:
                                seen.append((cycle, q, "같은 칸"))

    def pairs(self, rows, cycle):
        for row in rows or []:
            a, t = row.get("asker_id"), row.get("target_id")
            if a is None or t is None:
                continue
            for p, q in ((a, t), (t, a)):
                seen = self.contacts.setdefault(p, [])
                key = (row.get("question_id"), q)
                if key not in [(x[2], x[1]) for x in seen if len(x) > 2]:
                    seen.append((cycle, q, row.get("question_id")))

    # ---------- read ----------
    def met_since(self, cid, cycle):
        return sorted({q for c, q, _ in self.contacts.get(cid, []) if c is not None and cycle is not None and c > cycle})

    def view(self, cid, me=None):
        """The card as the prompt shows it. Everything stays stored; long lists are summarised."""
        c = self.cards.get(cid)
        if not c:
            return None
        holdings = {}
        for kind, h in c["holdings"].items():
            item = dict(h)
            if h["grade"] == "가능성":
                met = [x for x in self.met_since(cid, h["cycle"]) if x != me]
                if met:
                    item["met_since"] = met
            holdings[kind] = item
        trades = c["trades"]
        out = {"holdings": holdings or None,
               "trades_with_me": {"count": _count(trades, "status"), "last": trades[-3:]} if trades else None,
               "my_questions": {"count": _count(c["questions"], "outcome"),
                                "clues_from_them": sum(q.get("new_clues") or 0 for q in c["questions"]),
                                "last": c["questions"][-2:]} if c["questions"] else None,
               "asked_me": len(c["asked_me"]) or None,
               "said": c["said"] or None,
               "last_seen": c["last_seen"],
               "contradictions": c["contradictions"][-3:] or None}
        return {k: v for k, v in out.items() if v is not None}

    def seek(self, needed, here=(), me=None):
        """For each clue type I still need: who has shown evidence of holding it, best first."""
        rank = {"확인": 0, "가능성": 1}
        out = {}
        for kind in needed:
            rows = []
            for cid, c in self.cards.items():
                h = c["holdings"].get(kind)
                if cid == me or not h or h["state"] != "있음":
                    continue
                rows.append({"id": cid, "grade": h["grade"], "cycle": h["cycle"], "here": cid in here,
                             "last_seen": c["last_seen"]})
            rows.sort(key=lambda r: (rank[r["grade"]], not r["here"], -(r["cycle"] or 0)))
            if rows:
                out[kind] = rows[:4]
        return out

    def case_claims(self, me=None, limit=12):
        """[FIX-B45] What each character said about the case (a clue type it named), latest per topic.
        Bounded by characters x topics, so nothing is pushed out by newer chatter."""
        rows, seen = [], set()
        for cid, c in self.cards.items():
            if cid == me:
                continue
            for kind, s in (c.get("said") or {}).items():
                if kind not in TYPE_KO or (cid, s.get("text")) in seen:
                    continue
                seen.add((cid, s.get("text")))
                honest = sum(1 for t in c["trades"] if t.get("partner_honest") is True)
                broken = sum(1 for t in c["trades"] if t.get("partner_honest") is False)
                rows.append({"from": cid, "cycle": s.get("cycle"), "context": s.get("context"), "text": s.get("text"),
                             "grade": "가능성", "this_game": {"honest_trades": honest, "deceived_me": broken,
                                                            "contradictions": len(c["contradictions"])}})
        rows.sort(key=lambda r: -(r["cycle"] or 0))
        return rows[:limit]

    def notable(self, exclude=()):
        """Characters elsewhere whose card holds a contradiction (possible deceit)."""
        return {cid: c["contradictions"][-2:] for cid, c in self.cards.items()
                if c["contradictions"] and cid not in exclude} or None

    def snapshot(self):
        return {"cards": {str(k): v for k, v in self.cards.items()}}


def _count(rows, key):
    out = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


class Notes:
    """Notes the agent writes itself (textbook II ch.6 note-taking). Edited only by the agent."""

    def __init__(self):
        self.items, self.next_no, self.log = [], 1, []

    @property
    def full(self):
        return len(self.items) >= NOTE_CAP

    def apply(self, ops, cycle):
        """ops: [{"op": "add"|"edit"|"drop"|"pin"|"unpin", "no", "text"}]. Returns (applied, refused)."""
        applied, refused = [], []
        if not isinstance(ops, list):
            return applied, ([{"op": ops, "why": "memory는 배열이어야 한다"}] if ops else [])
        for op in ops[:NOTE_OPS_PER_TURN]:
            if not isinstance(op, dict):
                refused.append({"op": op, "why": "형식 오류"}); continue
            kind, no = str(op.get("op", "")).lower(), op.get("no")
            text = str(op.get("text") or "").strip()[:NOTE_LEN]
            item = next((x for x in self.items if x["no"] == no), None)
            if kind == "add":
                if not text:
                    refused.append({"op": "add", "why": "빈 메모"}); continue
                if self.full:
                    refused.append({"op": "add", "why": f"메모가 {NOTE_CAP}개로 가득 참: edit 또는 drop 먼저"}); continue
                self.items.append({"no": self.next_no, "text": text, "cycle": cycle, "updated": cycle, "pinned": False})
                applied.append({"op": "add", "no": self.next_no}); self.next_no += 1
            elif item is None:
                refused.append({"op": kind, "no": no, "why": "없는 번호"})
            elif kind == "edit" and text:
                item.update(text=text, updated=cycle); applied.append({"op": "edit", "no": no})
            elif kind == "drop":
                self.items.remove(item); applied.append({"op": "drop", "no": no})
            elif kind == "pin":
                if sum(x["pinned"] for x in self.items) >= NOTE_PINS and not item["pinned"]:
                    refused.append({"op": "pin", "no": no, "why": f"고정은 {NOTE_PINS}개까지"}); continue
                item["pinned"] = True; applied.append({"op": "pin", "no": no})
            elif kind == "unpin":
                item["pinned"] = False; applied.append({"op": "unpin", "no": no})
            else:
                refused.append({"op": kind, "no": no, "why": "알 수 없는 연산"})
        self.log.append({"cycle": cycle, "applied": applied, "refused": refused})
        return applied, refused

    def view(self):
        rows = sorted(self.items, key=lambda x: (not x["pinned"], -x["updated"], x["no"]))
        return {"notes": [{k: x[k] for k in ("no", "text", "cycle", "pinned")} for x in rows],
                "full": self.full}
