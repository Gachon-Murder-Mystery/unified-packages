"""Spec v1.3 data, truthful inference and mission predicates (no agent policy)."""
import json
import random
import re
from pathlib import Path

DATA = json.loads(Path(__file__).with_name('competitive_catalog.json').read_text(encoding='utf-8'))
MBTI = DATA['mbti']
TIERS = ['하', '중', '상']
# [FIX-A53] user decision 2026-11-12: every mission is worth 20; tiers are only the GM's pick slots
REWARDS = {'하': 20, '중': 20, '상': 20}
SHEETS = {c['id']: c for c in DATA['characters']}
MISSIONS = {m['id']: m for m in DATA['missions']}
INTERFERENCE = tuple(m['id'] for m in DATA['missions'] if m.get('category') == 'interference' and not m.get('retired'))  # [FIX-A53]
END_JUDGED = frozenset(m['id'] for m in DATA['missions'] if m.get('judged') == 'end')  # [FIX-A53] judged when the game ends


def tier(mission_id, character_id, mbti, n=None):
    m, c = MISSIONS[mission_id], SHEETS[character_id]
    base = m.get('quantity', {}).get(str(n)) if m.get('quantity') else m['base']
    if base not in TIERS:
        return None
    def matches(t):   # [FIX-A53] job and stat tokens only: a personality never changes a mission's difficulty
        if t in DATA['stat_tokens']:
            return c['stats'][DATA['stat_tokens'][t]] == 5
        return t == c['job']
    easy, hard = any(map(matches, m['easier'])), any(map(matches, m['harder']))
    return TIERS[max(0, min(2, TIERS.index(base)+(int(hard)-int(easy))))]


def min_cycles(mission):
    """[FIX-A7] Longest cycle count named in the condition ("40사이클 동안 ..." -> 40).
    Conservative for "within N cycles" wording, which can also finish earlier."""
    return max([int(x) for x in re.findall(r'(\d+)\s*사이클', mission['condition'])] or [0])


# [FIX-A10] Fewest cycles in which each mission can possibly be completed (one interaction
# action per cycle, a move every movement phase, trades cost no action). The text parse of
# min_cycles() missed counted actions (8 preparations need 16 cycles) and over-counted
# "within 15 cycles" wording, so every mission is listed explicitly.
MIN_CYCLES = {'O1': 2, 'O2': 41, 'O3': 6, 'O4': 1, 'O5': 1, 'O6': 2, 'O7': 40,
              'M01': 7, 'M02': 10, 'M03': 6, 'M04': 6, 'M05': 3, 'M06': 5, 'M07': 10, 'M08': 20,
              'M09': 10, 'M10': 5, 'M11': 8, 'M12': 1, 'M13': 1, 'M14': 1, 'M15': 1, 'M16': 40,
              'M17': 1, 'M18': 1, 'M19': 1, 'M20': 30, 'M21': 30, 'M22': 30, 'M23': 30, 'M24': 16,
              'M25': 20, 'M26': 40, 'M27': 40, 'M28': 5, 'M29': 7, 'M30': 4, 'M31': 3, 'M32': 6,
              'M33': 3, 'M34': 3, 'M35': 6, 'M36': 1, 'M37': 1, 'M38': 1, 'M39': 1, 'M40': 1,
              'M41': 1, 'M42': 21, 'M43': 1, 'M44': 4, 'M45': 10, 'M46': 2, 'M47': 3, 'M48': 1,
              'M49': 15, 'M50': 8, 'M51': 1,
              # [FIX-A21] a player can join one trade per cycle (it is the action)
              'M52': 1, 'M53': 2, 'M54': 2, 'M55': 2, 'M56': 4}
# [FIX-A53] v10 quantities (catalog rewritten by tools/build_v10_missions.py)
MIN_CYCLES.update({'O1': 3, 'O6': 2, 'M01': 8, 'M02': 14, 'M03': 12, 'M04': 12, 'M05': 4, 'M06': 12,
                   'M08': 30, 'M09': 30, 'M10': 9, 'M11': 7, 'M20': 30, 'M21': 30, 'M23': 30, 'M25': 25,
                   'M28': 6, 'M29': 8, 'M30': 5, 'M31': 3, 'M32': 10, 'M33': 6, 'M34': 5, 'M36': 4,
                   'M38': 4, 'M39': 4, 'M44': 6, 'M45': 15, 'M46': 3, 'M50': 12, 'M52': 2, 'M53': 3,
                   'M54': 3, 'M55': 3, 'M56': 8, 'I1': 1, 'I2': 1, 'I3': 9, 'I4': 2})
# Who must exist besides me: other characters (players or NPCs), other players, NPCs.
# Trade partners are any living character (NPCs trade too).
NEEDS = {'O4': {'others': 4}, 'O5': {'others': 1}, 'M07': {'others': 1}, 'M08': {'players': 1},
         'M12': {'others': 6}, 'M13': {'npcs': 3}, 'M14': {'players': 1}, 'M15': {'npcs': 1},
         'M17': {'others': 3}, 'M18': {'others': 2}, 'M19': {'others': 3}, 'M20': {'others': 1},
         'M28': {'others': 1}, 'M29': {'others': 7}, 'M35': {'npcs': 1}, 'M36': {'others': 1},
         'M37': {'others': 1}, 'M38': {'others': 3}, 'M39': {'others': 1}, 'M43': {'players': 3},
         'M44': {'players': 1}, 'M46': {'players': 1}, 'M47': {'npcs': 1},
         'M52': {'others': 1}, 'M53': {'others': 1}, 'M54': {'others': 1}, 'M56': {'others': 1},
         'O4': {'others': 4}, 'M12': {'others': 5}, 'M29': {'others': 8}, 'M38': {'others': 4}, 'M43': {'players': 4},
         'M19': {'others': 3}, 'I1': {'players': 1}, 'I2': {'others': 1}, 'I4': {'players': 1}}   # [FIX-A53]
BUNDLE_MISSIONS = ('M52', 'M53', 'M54')  # need bundled / unequal trades (interaction_order simultaneous)


def mission_feasible(mid, deadline, player_count, cid=None, targets=None, rules=None):
    """[FIX-A10] Can this mission be completed in a game with these settings at all?
    Necessary conditions (cycles, who exists), so a mission that passes may still be missed,
    but one that fails can never be achieved. Relationship targets are reachable: questions
    and fulfilled trades both raise them every cycle."""
    m = MISSIONS[mid]
    if m.get('retired'):   # [FIX-A53]
        return False, 'retired in v10: ' + m.get('retired_reason', '')
    need = MIN_CYCLES.get(mid, min_cycles(m))
    if deadline is not None and need > deadline:
        return False, f'needs at least {need} cycles, game has {deadline}'
    have = {'others': 11, 'players': player_count - 1, 'npcs': 12 - player_count}
    for who, n in NEEDS.get(mid, {}).items():
        if have[who] < n:
            return False, f'needs {n} {who}, game has {have[who]}'
    if mid == 'M48' and deadline is not None and have['others'] * deadline < 6:
        return False, 'not enough possible askers'
    if mid in BUNDLE_MISSIONS and (rules or {}).get('interaction_order') != 'simultaneous':
        return False, 'needs bundled trades (interaction_order simultaneous)'  # [FIX-A21]
    return True, ''


def candidates(seed, aid, cid, mbti, previous=(), player_count=3, deadline=None, rules=None):
    result = {}
    for t in TIERS:
        ids = []
        for mid, m in MISSIONS.items():
            if mid in previous or mid in INTERFERENCE:   # [FIX-A53] interference missions are assigned apart
                continue
            if not mission_feasible(mid, deadline, player_count, cid, rules=rules)[0]:
                continue  # [FIX-A7, FIX-A10] never offer a mission this game cannot satisfy
            quantities = [int(x) for x in m['quantity']] if m.get('quantity') else [None]
            if m.get('quantity'):
                eligible = sorted({tier(mid, cid, mbti, n) for n in quantities})
                assigned_slot = random.Random(f'quantity-slot:{seed}:{aid}:{cid}:{mid}').choice(eligible)
                if t != assigned_slot: continue
            if any(tier(mid, cid, mbti, n) == t for n in quantities):
                ids.append(mid)
        rng = random.Random(f'missions:{seed}:{aid}:{cid}:{t}:{player_count}')
        result[t] = rng.sample(sorted(ids), min(3, len(ids)))
    return result


def infer(information):
    """Close only accessible facts. No truth lookup; supports are clue ID sets."""
    excluded = {k: {} for k in ('suspect', 'weapon', 'place')}
    positions = {'suspect': {}, 'weapon': {}}
    companion = []
    pairs = []  # [FIX-A18]
    for item in information:
        cid = item['id']
        if item.get('kind') == 'definite':
            excluded[item['category']][item['candidate']] = {cid}
        elif item.get('kind') == 'ambiguous' and isinstance(item.get('pair'), dict) and len(item['pair']) == 2:
            (c1, v1), (c2, v2) = sorted(item['pair'].items())
            pairs.append(((c1, v1), (c2, v2), {cid}))
        elif item.get('kind') == 'indirect':
            if item['type'] == 'companion':
                companion.append((item['character'], item['other'], {cid}))
            else:
                cat, key = ('suspect', 'character') if item['type'] == 'character_room' else ('weapon', 'weapon')
                positions[cat][item[key]] = (item['room'], {cid})
    changed = True
    while changed:
        changed = False
        for a, b, support in companion:
            for x, y in ((a, b), (b, a)):
                if x not in positions['suspect'] and y in positions['suspect']:
                    room, basis = positions['suspect'][y]
                    positions['suspect'][x] = (room, support | basis)
                    changed = True
        scene = [i for i in range(8) if i not in excluded['place']]
        for cat in ('suspect', 'weapon'):
            for i, (room, support) in positions[cat].items():
                basis = excluded['place'].get(room)
                if basis is None and len(scene) == 1 and room != scene[0]:
                    basis = set().union(*excluded['place'].values())
                if basis is not None and i not in excluded[cat]:
                    excluded[cat][i] = basis | support
                    changed = True
            remaining = [i for i in range(12 if cat == 'suspect' else 8) if i not in excluded[cat]]
            if len(remaining) == 1 and remaining[0] in positions[cat]:
                room, support = positions[cat][remaining[0]]
                support = support | set().union(*excluded[cat].values())
                for i in range(8):
                    if i != room and i not in excluded['place']:
                        excluded['place'][i] = support
                        changed = True
            # [FIX-A5] Reverse inference (generalises the rule above). The culprit/weapon is
            # one of the candidates still open and was in the scene at midnight, so when every
            # open candidate's midnight room is known, the scene is one of those rooms.
            # Mirrored rule-for-rule in Agent_Reasoning/schema.py (H2 relies on identity).
            if remaining and all(i in positions[cat] for i in remaining):
                rooms = {positions[cat][i][0] for i in remaining}
                support = set().union(*(positions[cat][i][1] for i in remaining), *excluded[cat].values())
                for i in range(8):
                    if i not in rooms and i not in excluded['place']:
                        excluded['place'][i] = support
                        changed = True
        # [FIX-A18] Pair exclusions ("X together with W is not the answer"): once the other
        # side's category is down to W, X is out (and the reverse). Mirrored in schema.py.
        for (c1, v1), (c2, v2), support in pairs:
            for (cx, vx), (cy, vy) in (((c1, v1), (c2, v2)), ((c2, v2), (c1, v1))):
                left = [i for i in range(12 if cy == 'suspect' else 8) if i not in excluded[cy]]
                if left == [vy] and vx not in excluded[cx]:
                    excluded[cx][vx] = support | set().union(*excluded[cy].values())
                    changed = True
    return {cat: {str(i): sorted(support) for i, support in values.items()} for cat, values in excluded.items()}


def narrative_options(clue):
    """Closed, truth-preserving grammar. Unverifiable arbitrary prose is rejected."""
    if clue.get('kind') != 'indirect':
        return []
    if clue['type'] == 'character_room':
        name, room = SHEETS[clue['character']]['name'], DATA['places'][clue['room']]
        return [f'자정에 {room}에 누군가 있었어요.', f'자정쯤 {room}에 누가 있었던 건 확실해요.', f'{name}은(는) 자정에 어느 칸에 있었던 것 같아요.',
                f'{name}은(는) 자정에 {room}에 있었던 것 같아요.']
    if clue['type'] == 'companion':
        a, b = SHEETS[clue['character']]['name'], SHEETS[clue['other']]['name']
        return [f'{a}은(는) 자정에 혼자가 아니었어요.', f'{a} 씨는 그날 밤 혼자가 아니었어요.', f'{a}은(는) 자정에 {b}와 함께 있었던 것 같아요.']
    return [f'어떤 흉기가 자정에 {DATA["places"][clue["room"]]}에 있었던 것 같아요.',
            f'그 시간 {DATA["places"][clue["room"]]} 어딘가에 흉기가 하나 있었다고 들었어요.',
            f'{clue["weapon_name"]}은(는) 자정에 어느 칸에 있었어요.']


def build_case(seed):
    rng = random.Random(f'case:{seed}')
    truth = {'suspect': rng.randrange(12), 'weapon': rng.randrange(8), 'place': rng.randrange(8)}
    chars, weapons = [rng.randrange(8) for _ in range(12)], [rng.randrange(8) for _ in range(8)]
    chars[truth['suspect']] = weapons[truth['weapon']] = truth['place']
    clues = []
    def add(data):
        i = len(clues)
        # Opaque to recipients: no category/candidate index in the ID.
        import hashlib
        cid = 'clue:'+hashlib.sha256(f'case-clue:{seed}:{i}'.encode()).hexdigest()[:24]
        clues.append({'id': cid, 'source_room': (i*3+2)%8, **data})
    for cat, size in (('suspect', 12), ('weapon', 8), ('place', 8)):
        for i in range(size):
            if i != truth[cat]:
                add({'kind': 'definite', 'category': cat, 'candidate': i, 'excluded_id': i,
                     'text': f'{cat} 후보 {i}은(는) 정답이 아니다.'})
    for i, room in enumerate(chars):
        add({'kind': 'indirect', 'type': 'character_room', 'character': i, 'room': room,
             'text': f'{SHEETS[i]["name"]}은(는) 자정에 {DATA["places"][room]}에 있었다.'})
    from engine import WEAPONS
    for i, room in enumerate(weapons):
        add({'kind': 'indirect', 'type': 'weapon_room', 'weapon': i, 'weapon_name': WEAPONS[i], 'room': room,
             'text': f'{WEAPONS[i]}은(는) 자정에 {DATA["places"][room]}에 있었다.'})
    for room in range(8):
        people = [i for i, r in enumerate(chars) if r == room]
        for a, b in zip(people, people[1:]):
            add({'kind': 'indirect', 'type': 'companion', 'character': a, 'other': b,
                 'text': f'{SHEETS[a]["name"]}은(는) 자정에 {SHEETS[b]["name"]}와 같이 있었다.'})
    closure = infer(clues)
    assert all(len(closure[k]) == size-1 and str(truth[k]) not in closure[k]
               for k, size in (('suspect', 12), ('weapon', 8), ('place', 8)))
    return truth, {'character_rooms': chars, 'weapon_rooms': weapons}, clues


def _n_in_room(v, room):
    return sum(x['rooms'][str(v['cid'])] == room for x in v['cycles'])


def _got(v, t):
    other = t['recipient'] if t['proposer'] == v['aid'] else t['proposer']
    return len(t['offers'].get(other, [])) if t['fulfilled'].get(other) else 0


# [FIX-A58] count missions (monotone counts with a fixed need, the same numbers as their v10 predicates in
# mission_complete). Only these can be named by I1: "got halfway and failed" needs a count. test_v10 checks that
# count >= need agrees with mission_complete for every one of them.
COUNTS = {
    'O1': lambda v: (sum(x['room'] == 5 and x['successes'] > 0 for x in v['search']), 3),
    'M01': lambda v: (len({x['room'] for x in v['acts'] if x['kind'] in ('question', 'explore')}), 8),
    'M02': lambda v: (sum(x['room'] == 2 and x['kind'] in ('question', 'explore') for x in v['acts']), 14),
    'M03': lambda v: (_n_in_room(v, 3), 12),
    'M04': lambda v: (_n_in_room(v, 4), 12),
    'M05': lambda v: (sum(x['room'] == 6 and x['successes'] > 0 for x in v['search']), 4),
    'M06': lambda v: (_n_in_room(v, 1), 12),
    'M10': lambda v: (sum(x['kind'] in ('question', 'explore') and v['crowd'](x['cycle'], 4) for x in v['acts']), 9),
    'M11': lambda v: (sum(x['kind'] in ('question', 'explore') and v['crowd'](x['cycle'], 1, exact=True) for x in v['acts']), 7),
    'M28': lambda v: (max([sum(x['target'] == y['target'] for x in v['qs']) for y in v['qs']] or [0]), 6),
    'M29': lambda v: (len({x['target'] for x in v['success'] if x['kind'] == 'question'}), 8),
    'M30': lambda v: (sum(x['approach'] == 'interrogate' for x in v['success']), 5),
    'M31': lambda v: (sum(x['approach'] == 'threaten' for x in v['success']), 3),
    'M32': lambda v: (sum(x['target'] is None and x['successes'] > 0 for x in v['search']), 10),
    'M33': lambda v: (len({x['room'] for x in v['search'] if x['successes'] > 0}), 6),
    'M34': lambda v: (sum(x['approach'] == 'question' and x['successes'] >= 2 for x in v['qs']), 5),
    'M36': lambda v: (sum(all(x['fulfilled'].values()) for x in v['trades']), 7),
    'M38': lambda v: (len({x['recipient'] if x['proposer'] == v['aid'] else x['proposer'] for x in v['agreements']}), 4),
    'M39': lambda v: (sum(x.get('first_sender') not in (None, v['aid']) for x in v['trades']), 4),
    'M41': lambda v: (len(v['indirect']), 8),
    'M43': lambda v: (len({x['recipient'] for x in v['g']['disclosures'] if x['sender'] == v['aid'] and x['kind'] == 'response_definite'}), 4),
    'M44': lambda v: (sum(x['target'] == v['target_cid'] for x in v['qs']), 6),
    'M45': lambda v: (_n_in_room(v, 5), 15),
    'M46': lambda v: (sum(x['target'] == v['target_cid'] and x['approach'] in ('interrogate', 'threaten') and x['successes'] > 0
                          for x in v['qs']), 3),
    'M48': lambda v: (len(v['received']()), 10),
    'M50': lambda v: (_n_in_room(v, 7), 12),
    'M51': lambda v: (len(v['definite']), 12),
    'M53': lambda v: (sum(_got(v, t) for t in v['trades']), 9),
    'M55': lambda v: (v['p'].get('multi_finds', 0), 3),
}


def mission_complete(g, aid, mission, final=False, progress=False):
    """All mission predicates. Only engine facts/counters, never case truth.
    [FIX-A53] missions judged at the end ('judged': 'end') count only when final=True (finish)."""
    if mission['mission_id'] in END_JUDGED and not final and not progress:
        return False
    p = g['players'][aid]
    cid = p['character_id']
    c = g['characters'][str(cid)]
    mid, targets = mission['mission_id'], mission.get('targets', {})
    acts = [x for x in g['activity'] if x['actor'] == cid]
    cycles = [x for x in g['cycle_history'] if str(cid) in x['alive']]
    current = g['cycle_id']
    qs = [x for x in acts if x['kind'] == 'question']
    search = [x for x in acts if x['kind'] == 'explore']
    success = [x for x in acts if x['successes'] > 0]
    agreements = [x for x in g['trades'].values() if aid in (x['proposer'], x['recipient']) and x['accepted']]
    trades = [x for x in agreements if x['status'] == 'settled']
    incoming = [v[str(cid)] for owner, v in g['relationships'].items() if int(owner) != cid]
    npc_incoming = [g['relationships'][str(i)][str(cid)] for i in range(12)
                    if i != cid and g['characters'][str(i)]['mode'] == 'npc']
    target_cid = targets.get('character')
    if 'player' in targets and targets['player'] in g['players']:
        target_cid = g['players'][targets['player']]['character_id']
    relation = g['relationships'].get(str(target_cid), {}).get(str(cid), -1)
    def at(room): return sum(x['rooms'][str(cid)] == room for x in cycles)
    def streak(room, extra=lambda x: True):
        n = best = 0
        for x in cycles:
            n = n+1 if x['rooms'][str(cid)] == room and extra(x) else 0
            best = max(best, n)
        return best
    def received(frame=None):
        return [x for x in g['activity'] if x.get('target') == cid and x['kind'] == 'question'
                and (frame is None or x['cycle'] == frame)]
    def duration(n, predicate):
        return len(cycles) >= n and all(predicate(x) for x in acts if x['cycle'] <= n)
    def crowd(cycle, n, exact=False):   # [FIX-A53] characters (me included) in my room that cycle
        row = next((x for x in cycles if x['cycle'] == cycle), None)
        if row is None: return False
        here = sum(r == row['rooms'][str(cid)] for o, r in row['rooms'].items() if o in row['alive'])
        return here == n if exact else here >= n
    start = set(p.get('start_clues', []))  # [FIX-A19] dealt clues are not "acquired"
    definite = [x for x in c['information'].values() if x.get('kind') == 'definite' and x['id'] not in start]
    indirect = [x for x in c['information'].values() if x.get('kind') in ('indirect', 'narrated') and x['id'] not in start]
    # [FIX-A53] v10 predicates (catalog rewritten by tools/build_v10_missions.py); old ids keep their old reading below
    deadline = (g.get('rules') or {}).get('deadline_cycles') or 30
    def window(n): return [x for x in cycles if x['cycle'] > deadline - n]
    if progress:   # [FIX-A58] (count so far, count needed) for the count missions I1 may name; None otherwise
        fn = COUNTS.get(mid)
        return fn(locals()) if fn else None
    if mid == 'O1': return sum(x['room'] == 5 and x['successes'] > 0 for x in search) >= 3
    if mid == 'O4': return sum(x >= 65 for x in incoming) >= 4
    if mid == 'O5': return relation >= 80
    if mid == 'O6': return len(targets.get('clue_ids', [])) == 2 and set(targets['clue_ids']) <= set(c['information'])
    if mid == 'M01': return len({x['room'] for x in acts if x['kind'] in ('question', 'explore')}) == 8
    if mid == 'M02': return sum(x['room'] == 2 and x['kind'] in ('question', 'explore') for x in acts) >= 14
    if mid == 'M03': return at(3) >= 12
    if mid == 'M04': return at(4) >= 12
    if mid == 'M05': return sum(x['room'] == 6 and x['successes'] > 0 for x in search) >= 4
    if mid == 'M06': return at(1) >= 12
    if mid == 'M08':
        last = window(10)
        return len(last) == 10 and all(x['rooms'][str(cid)] != x['rooms'].get(str(target_cid)) for x in last)
    if mid == 'M09':
        last = window(8)
        return len(last) == 8 and all(x['rooms'][str(cid)] == SHEETS[cid]['start_room'] for x in last)
    if mid == 'M10': return sum(x['kind'] in ('question', 'explore') and crowd(x['cycle'], 4) for x in acts) >= 9
    if mid == 'M11': return sum(x['kind'] in ('question', 'explore') and crowd(x['cycle'], 1, exact=True) for x in acts) >= 7
    if mid == 'M12': return sum(x >= 60 for x in incoming) >= 5
    if mid == 'M19': return sum(x >= 75 for x in incoming) >= 3
    if mid == 'M20': return duration(deadline, lambda x: x['approach'] != 'threaten') and len(trades) >= 3
    if mid == 'M21': return duration(deadline, lambda x: x['kind'] != 'explore')
    if mid == 'M23': return duration(deadline, lambda x: x['kind'] != 'prepare')
    if mid == 'M25':
        n = 0
        for i, x in enumerate(cycles):
            moved = x['rooms'][str(cid)] != (SHEETS[cid]['start_room'] if i == 0 else cycles[i-1]['rooms'][str(cid)])
            n = n+1 if moved else 0
            if n >= 25: return True
        return False
    if mid == 'M28': return max([sum(x['target'] == y['target'] for x in qs) for y in qs] or [0]) >= 6
    if mid == 'M29': return len({x['target'] for x in success if x['kind'] == 'question'}) >= 8
    if mid == 'M30': return sum(x['approach'] == 'interrogate' for x in success) >= 5
    if mid == 'M32': return sum(x['target'] is None and x['successes'] > 0 for x in search) >= 10
    if mid == 'M33': return len({x['room'] for x in search if x['successes'] > 0}) >= 6
    if mid == 'M34': return sum(x['approach'] == 'question' and x['successes'] >= 2 for x in qs) >= 5
    if mid == 'M36': return sum(all(x['fulfilled'].values()) for x in trades) >= 7
    if mid == 'M38': return len({x['recipient'] if x['proposer'] == aid else x['proposer'] for x in agreements}) >= 4
    if mid == 'M39': return sum(x.get('first_sender') not in (None, aid) for x in trades) >= 4
    if mid == 'M40': return all(sum(x['category'] == cat for x in definite) >= 3 for cat in ('suspect', 'weapon', 'place'))
    if mid == 'M41': return len(indirect) >= 8
    if mid == 'M43': return len({x['recipient'] for x in g['disclosures'] if x['sender'] == aid and x['kind'] == 'response_definite'}) >= 4
    if mid == 'M44': return sum(x['target'] == target_cid for x in qs) >= 6
    if mid == 'M45': return at(5) >= 15
    if mid == 'M46': return sum(x['target'] == target_cid and x['approach'] in ('interrogate', 'threaten') and x['successes'] > 0 for x in qs) >= 3
    if mid == 'M48': return len(received()) >= 10
    if mid == 'M50': return at(7) >= 12
    if mid == 'M51': return len(definite) >= 12
    if mid == 'I1':   # [FIX-A58] the target must have got at least halfway, so a block is something that happened
        target = g['players'].get(targets.get('player'))
        hit = [m for m in (target or {}).get('missions', []) if m['mission_id'] == targets.get('mission_id')]
        if not hit or hit[0]['achieved']:
            return False
        got = mission_complete(g, targets['player'], hit[0], progress=True)
        return bool(got) and got[0] >= -(-got[1] // 2)
    if mid == 'I2':
        broken = [x for x in trades if not x['fulfilled'].get(aid) and any(v for a, v in x['fulfilled'].items() if a != aid)]
        victims = {(x['recipient'] if x['proposer'] == aid else x['proposer']) for x in broken}
        def to_me(v):
            vc = g['players'][v]['character_id'] if v in g['players'] else int(str(v).split(':')[1])
            return g['relationships'][str(vc)][str(cid)]
        return bool(victims) and all(to_me(v) >= 50 for v in victims)
    if mid == 'I3':
        acquired = [x for x in p['acquired'] if x['room'] == targets.get('place')]
        return any(current - x['cycle'] >= 8 and not any(x['id'] in other['information'] for i, other in g['characters'].items()
                                                          if int(i) != cid and other['controller']) for x in acquired)
    if mid == 'I4':
        hits = sum(x['target'] == target_cid and x['approach'] in ('interrogate', 'threaten') and x['successes'] > 0 for x in qs)
        return hits >= 2 and relation >= 50
    if mid == 'O1': return sum(x['room'] == 5 and x['successes'] and x['alone_players'] for x in search) >= 2
    if mid == 'O2':
        first = p.get('first_definite')
        return bool(first and current-first['cycle'] >= 40 and not any(
            x['clue_id'] == first['id'] and x['sender'] == aid for x in g['disclosures']))
    if mid == 'O3':  # [FIX-A11] the successful search must fall inside the 6-cycle stay ("그동안")
        hits, n, hit = {x['cycle'] for x in search if x['room'] == 7 and x['successes']}, 0, False
        for x in cycles:
            if x['rooms'][str(cid)] == 7 and all(x['rooms'][o] != 7 for o in x['alive'] if o != str(cid)):
                n, hit = n+1, hit or x['cycle'] in hits
                if n >= 6 and hit: return True
            else: n, hit = 0, False
        return False
    if mid == 'O4': return sum(x >= 65 for x in incoming) >= 4
    if mid == 'O5': return relation >= 85
    if mid == 'O6': return set(targets.get('clue_ids', [])) <= set(c['information']) and len(targets.get('clue_ids', [])) == 3
    if mid == 'O7': return duration(40, lambda x: x['approach'] not in ('interrogate', 'threaten'))
    if mid == 'M01': return len(p['visited']) == 8
    if mid == 'M02': return at(2) >= 10
    if mid == 'M03': return streak(3) >= 6
    if mid == 'M04': return streak(4, lambda x: all(x['rooms'][other] != 4 for other in x['alive'] if other != str(cid))) >= 6
    if mid == 'M05': return sum(x['room'] == 6 and x['successes'] > 0 for x in search) >= 3
    if mid == 'M06': return streak(1, lambda x: not received(x['cycle'])) >= 5
    if mid == 'M07': return sum(x['rooms'][str(cid)] == x['rooms'].get(str(target_cid)) for x in cycles) >= 10
    if mid == 'M08': return len(cycles) >= 20 and all(x['rooms'][str(cid)] != x['rooms'].get(str(target_cid)) for x in cycles[:20])
    if mid == 'M09': return len(cycles) >= 10 and all(x['rooms'][str(cid)] == SHEETS[cid]['start_room'] for x in cycles[:10])
    if mid == 'M10': return sum(sum(r == x['rooms'][str(cid)] for r in x['rooms'].values()) >= 4 for x in cycles) >= 5
    if mid == 'M11': return sum(sum(r == x['rooms'][str(cid)] for r in x['rooms'].values()) == 1 for x in cycles) >= 8
    if mid == 'M12': return sum(x >= 60 for x in incoming) >= 6
    if mid == 'M13': return sum(x >= 70 for x in npc_incoming) >= 3
    if mid == 'M14': return relation >= 70
    if mid == 'M15': return relation >= 80
    if mid == 'M16': return len(cycles) >= 40 and not p.get('relationship_below_30', False)
    if mid == 'M17': return sum(x <= 30 for x in incoming) >= 3
    if mid == 'M18': return max(incoming) >= 80 and min(incoming) <= 20
    if mid == 'M19': return current <= 15 and sum(x >= 60 for x in incoming) >= 3
    if mid == 'M20': return duration(30, lambda x: x['approach'] != 'threaten') and len(agreements) >= 1
    if mid == 'M21': return duration(30, lambda x: x['kind'] != 'explore')
    if mid == 'M22': return duration(30, lambda x: x['kind'] != 'question')
    if mid == 'M23': return duration(30, lambda x: x['kind'] != 'prepare')
    if mid == 'M24': return sum(x['prepared'] and x['kind'] in ('question', 'explore') for x in acts) >= 8
    if mid == 'M25':  # [FIX-A11] any 20 consecutive cycles, not only the first 20
        n = 0
        for i, x in enumerate(cycles):
            moved = x['rooms'][str(cid)] != (SHEETS[cid]['start_room'] if i == 0 else cycles[i-1]['rooms'][str(cid)])
            n = n+1 if moved else 0
            if n >= 20: return True
        return False
    if mid == 'M26': return len(cycles) >= 40 and not any(x['sender'] == aid and x['kind'] == 'possession' for x in g['disclosures'] if x['cycle'] <= 40)
    if mid == 'M27': return len(cycles) >= 40 and not any(x['proposer'] == aid or (x['recipient'] == aid and x['accepted']) for x in g['trades'].values() if x['cycle'] <= 40)
    if mid == 'M28': return max([sum(x['target'] == y['target'] for x in qs) for y in qs] or [0]) >= 5
    if mid == 'M29': return len({x['target'] for x in success if x['kind'] == 'question'}) >= 7
    if mid == 'M30': return sum(x['approach'] == 'interrogate' for x in success) >= 4
    if mid == 'M31': return sum(x['approach'] == 'threaten' for x in success) >= 3
    if mid == 'M32': return sum(x['target'] is None and x['successes'] > 0 for x in search) >= 6
    if mid == 'M33': return len({x['room'] for x in search if x['successes'] > 0}) >= 3
    if mid == 'M34': return sum(x['approach'] == 'question' and x['successes'] >= 2 for x in qs) >= 3
    if mid == 'M35': return sum(x['approach'] in ('question', 'interrogate') and x['target_mode'] == 'npc' and x['successes'] > 0 for x in qs) >= 6
    if mid == 'M36': return sum(all(x['fulfilled'].values()) for x in trades) >= 3
    if mid == 'M37': return sum(not x['fulfilled'][aid] and any(v for a, v in x['fulfilled'].items() if a != aid) for x in trades) >= 2
    if mid == 'M38': return len({x['recipient'] if x['proposer'] == aid else x['proposer'] for x in agreements}) >= 3
    if mid == 'M39': return sum(x.get('first_sender') not in (None, aid) for x in trades) >= 3
    if mid == 'M40': return all(sum(x['category'] == cat for x in definite) >= 2 for cat in ('suspect', 'weapon', 'place'))
    if mid == 'M41': return len(indirect) >= 4
    if mid == 'M42':
        acquired = [x for x in p['acquired'] if x['room'] == targets.get('place')]
        return any(current-x['cycle'] >= 20 and not any(x['id'] in other['information'] for i, other in g['characters'].items() if int(i) != cid and other['controller']) for x in acquired)
    if mid == 'M43': return len({x['recipient'] for x in g['disclosures'] if x['sender'] == aid and x['kind'] == 'response_definite'}) >= 3
    if mid == 'M44': return sum(x['target'] == target_cid for x in qs) >= 4
    if mid == 'M45': return at(5) >= 10
    if mid == 'M46': return sum(x['target'] == target_cid and x['approach'] in ('interrogate', 'threaten') and x['successes'] > 0 for x in qs) >= 2
    if mid == 'M47': return sum(x['target'] == target_cid and x['approach'] == 'question' and x['successes'] > 0 for x in qs) >= 3
    if mid == 'M48': return len(received()) >= 6
    if mid == 'M49': return len(cycles) >= 15 and not any(x['cycle'] <= 15 for x in received())
    if mid == 'M50': return at(7) >= 8
    if mid == 'M51': return len(definite) >= mission['params']['N']
    # [FIX-A21] trade missions read settled trades (sealed decisions, A17) and bundles (A20)
    def got(t): return len(t['offers'].get(other_side(t), [])) if t['fulfilled'].get(other_side(t)) else 0
    def other_side(t): return t['recipient'] if t['proposer'] == aid else t['proposer']
    if mid == 'M52': return sum(got(t) >= 3 for t in trades) >= 2   # [FIX-A53] twice
    if mid == 'M53': return sum(got(t) for t in trades) >= 9         # [FIX-A53]
    if mid == 'M53': return sum(got(t) for t in trades) >= 5
    if mid == 'M54': return sum(bool(t['fulfilled'].get(aid)) and len(t['offers'].get(aid, [])) > len(t['offers'].get(other_side(t), []))
                               for t in trades) >= 3   # [FIX-A53]
    if mid == 'M55': return p.get('multi_finds', 0) >= 3   # [FIX-A53]
    if mid == 'M56': return (sum(bool(t['fulfilled'].get(aid)) for t in trades) >= 8 and   # [FIX-A53]
                             not any((t.get('withheld') or {}).get(aid) for t in trades))
    raise AssertionError(f'unimplemented mission {mid}')
