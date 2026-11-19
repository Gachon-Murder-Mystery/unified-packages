"""Level-design simulation: how many clues (and which kinds) can a player collect in 20-30 cycles?

No agents, no personality, no LLM. The real engine (Game-engine/) runs a full 12-player game; scripted
policies play every seat. Two scenarios (user decision 2026-11-08):

  A  upper bound  everyone cooperates: players in a room pair up and swap the clue types the other lacks
                  in honest bundled trades (up to 3 for 3); answers give a definite clue whenever allowed.
  B  solo         nobody cooperates: no trades; answers give only the forced amount in the weakest form
                  (a definite clue only at 3 successes). Everyone still searches, so rooms are contested.
  C  lower bound  = the bottom 10% of B (bad dice).

Common play (best legal move, not a personality): move to the room with the most clues still new to me
(spread out), search while the room holds something new for me, otherwise ask the occupant who holds the
most clues I lack with the easiest approach (B: prepare first, so 3 successes are possible).

Engine settings = the experiments: interaction_order simultaneous, clue_flow boosted, mbti_rules off.

Usage: python sim_clue_capacity.py --seeds 100 --workers 6 --out runs/sim-capacity
"""
import argparse
import collections
import itertools
import json
import os
import statistics as st
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'Game-engine'))

from integration import ExternalEngine, ContractError  # noqa: E402
from competitive import Rules, public_clue  # noqa: E402
from competitive_rules import MISSIONS, infer, tier  # noqa: E402

SNAPSHOTS = (10, 15, 20, 25, 30)
CATS = ('suspect', 'weapon', 'place')
SIZES = {'suspect': 12, 'weapon': 8, 'place': 8}
IND = ('character_room', 'weapon_room', 'companion')


class Sim:
    def __init__(self, seed, scenario, deadline, tmp):
        self.seed, self.scenario, self.deadline = seed, scenario, deadline
        self.now = 1000
        self.e = ExternalEngine(str(Path(tmp) / f'sim-{scenario}-{seed}.db'), lambda: self.now)
        self.op = {'operator_token': self.e.operator_token}
        self.pool = self.call('pool/bootstrap', self.op)
        rules = {'interaction_order': 'simultaneous', 'clue_flow': 'boosted', 'mbti_rules': False,
                 'deadline_cycles': deadline, 'question_timeout': 3600, 'trade_timeout': 3600}  # the clock is frozen: never expires
        new = self.call('games/create', self.op, game_id='sim', seed=seed, rules=rules)
        self.gm = new['controller'] | {'game_id': 'sim'}
        self.ids = [a['agent_id'] for a in self.pool['agents'] if a['pool_role'] == 'player'][:12]
        act = [{'agent_id': a, 'character_id': i} for i, a in enumerate(self.ids)]
        draw = self.call('gm/mission-candidates', self.gm, activate=act)['mission_candidates']
        for item in act:
            item['sub_missions'] = self.picks(item, draw[item['agent_id']])
        self.call('gm/command', self.gm, command_id='setup', operation='PLAN', map_version=0, activate=act,
                  alternatives=[{'id': 'a', 'summary': 's'}, {'id': 'b', 'summary': 't'}], selected_alternative='a', reason='sim')
        self.creds = self.call('games/credentials', self.op, game_id='sim')['agent_credentials']
        self.seq = 0
        self.prepared_for = {}
        g = self.state()
        self.start_ids = {aid: set(self.ch(g, aid)['information']) for aid in self.ids}
        self.dry_cycle = None

    # ---- plumbing
    def call(self, route, creds=None, **kw):
        return self.e.handle(route, (creds or {}) | kw)

    def pc(self, aid):
        return {'game_id': 'sim'} | self.creds[aid]

    def state(self):
        with self.e.transaction() as db:
            return self.e.load(db, 'games', 'sim', 'id')

    @staticmethod
    def ch(g, aid):
        return g['characters'][str(g['players'][aid]['character_id'])]

    def picks(self, item, draw):
        mbti = next(a['fixed_mbti'] for a in self.pool['agents'] if a['agent_id'] == item['agent_id'])
        out = []
        for t, ids in draw.items():
            mid = ids[0]; m = MISSIONS[mid]; targets, params = {}, {}
            if m.get('quantity'):
                params['N'] = next(int(n) for n in m['quantity'] if tier(mid, item['character_id'], mbti, int(n)) == t)
            if m['designate'] == 'player': targets['player'] = next(a for a in self.ids if a != item['agent_id'])
            if m['designate'] in ('npc', 'character'): targets['character'] = (item['character_id'] + 1) % 12
            if m['designate'] == 'place': targets['place'] = 5
            if m['designate'] == 'indirect_clues':
                targets['clue_ids'] = [x['id'] for x in self.state()['clues'] if x['kind'] == 'indirect'][:3]
            out.append({'mission_id': mid, 'tier': t, 'targets': targets, 'params': params})
        return out

    def act(self, aid, action, payload=None):
        s = self.call('agent/observe', self.pc(aid)); self.seq += 1
        if not s.get('remaining_actions'):
            return None
        r = {k: s[k] for k in ('game_id', 'cycle_id', 'phase', 'phase_epoch')}
        r.update(agent_id=aid, action_id=f'a{self.seq}', action=action, payload=payload or {})
        try:
            return self.call('agent/action', self.pc(aid), request=r)
        except ContractError:
            return None

    def advance(self):
        s = self.call('gm/observe', self.gm)
        self.call('gm/command', self.gm, command_id=f"adv:{s['phase_epoch']}", operation='ADVANCE', phase_epoch=s['phase_epoch'])

    # ---- knowledge helpers (best play may look at the true state: this measures what is POSSIBLE)
    def new_in_room(self, g, aid, room):
        mine, taken = set(self.ch(g, aid)['information']), set(g.get('room_taken', []))
        return [c for c in g['clues'] if c['source_room'] == room and c['id'] not in taken and c['id'] not in mine]

    @staticmethod
    def full(c):
        return {k: v for k, v in c['information'].items() if v.get('kind') in ('definite', 'indirect')}

    def lacking(self, g, giver, taker):
        """Clues the giver holds in full that would be NEW information for the taker: not held, and not
        already implied (a definite clue for a candidate the taker has excluded by inference adds nothing)."""
        have = set(self.ch(g, taker)['information'])
        items = [v for k, v in self.full(self.ch(g, giver)).items() if k not in have]
        rule = object.__new__(Rules)
        return [v for v in items if rule.type_matches(v, {'type': ttype(v), 'exclude': self.exclusions(g, taker, ttype(v))})]

    def exclusions(self, g, aid, t):
        held = list(self.ch(g, aid)['information'].values())
        if t in CATS: return sorted(known_sets(held)[t])[:SIZES[t] - 1]
        if t == 'character_room': return sorted({x['character'] for x in held if x.get('type') == 'character_room'})
        if t == 'weapon_room': return sorted({x['weapon'] for x in held if x.get('type') == 'weapon_room'})
        return []

    # ---- phases
    def movement(self):
        g = self.state()
        heading = collections.Counter()
        order = list(self.ids)
        for aid in order:
            best = max(range(8), key=lambda r: (len(self.new_in_room(g, aid, r)) / (1 + heading[r]), -r))
            if not self.new_in_room(g, aid, best):   # nothing left anywhere: gather where the most players are
                crowd = collections.Counter(self.ch(g, b)['room'] for b in self.ids)
                best = max(range(8), key=lambda r: (crowd[r], -r))
            heading[best] += 1
            self.act(aid, 'move', {'room': best})
        self.advance()

    def interaction(self):
        g = self.state()
        rooms = collections.defaultdict(list)
        for aid in self.ids:
            rooms[self.ch(g, aid)['room']].append(aid)
        plans, trades = {}, []
        for room, here in rooms.items():
            searchers = [a for a in here if self.new_in_room(g, a, room)]
            idle = [a for a in here if a not in searchers]
            for a in searchers:
                plans[a] = ('explore', {})
            if self.scenario == 'A':     # pair the idle players for honest bundled swaps, most to gain first
                gain = {}
                for a, b in itertools.combinations(idle, 2):
                    gain[(a, b)] = min(3, len(self.lacking(g, a, b))) + min(3, len(self.lacking(g, b, a)))
                used = set()
                for (a, b), v in sorted(gain.items(), key=lambda x: -x[1]):
                    if v == 0 or a in used or b in used:
                        continue
                    body = self.trade_body(g, a, b)
                    if body:
                        used |= {a, b}
                        trades.append((a, b, body))
                        plans[a] = plans[b] = ('trade', None)
            for a in idle:
                if a not in plans:
                    plans[a] = self.question_plan(g, a, here)
        # proposals first, partners accept at once (each partner answers one proposal)
        for a, b, body in trades:
            tid = f't{self.seq}'; self.seq += 1
            try:
                self.call('trades/propose', self.pc(a), trade_id=tid, message_id='p' + tid, **body)
            except ContractError:
                plans[a] = plans[b] = ('pass', {})
                continue
            g2 = self.state()
            want = g2['trades'][tid]['requested']
            mine = list(self.full(self.ch(g2, b)).values())
            have_a = set(self.ch(g2, a)['information'])
            pool = [x for x in mine if x['id'] not in have_a] + [x for x in mine if x['id'] in have_a]
            ids = object.__new__(Rules).assign_typed(pool, want, pick=True)
            try:
                if ids:
                    self.call('trades/respond', self.pc(b), trade_id=tid, decision='accept', offer_ids=ids, message_id='r' + tid)
                else:
                    self.call('trades/respond', self.pc(b), trade_id=tid, decision='refuse', message_id='r' + tid)
                    plans[b] = self.question_plan(self.state(), b, rooms[self.ch(g2, b)['room']])
            except ContractError:
                pass
        for aid in self.ids:
            kind, payload = plans.get(aid, ('pass', {}))
            if kind != 'trade':
                self.act(aid, kind, payload)
        self.answer_all()
        self.advance()      # room searches settle here

    def question_plan(self, g, aid, here):
        others = [b for b in here if b != aid and g['characters'][str(g['players'][b]['character_id'])]['mode'] == 'active']
        if not others:
            return ('prepare', {}) if not self.ch(g, aid)['prepared'] else ('pass', {})
        target = max(others, key=lambda b: (len(self.lacking(g, b, aid)), -g['players'][b]['character_id']))
        if not self.lacking(g, target, aid):
            return ('pass', {})
        if self.scenario == 'B' and not self.ch(g, aid)['prepared']:
            return ('prepare', {})        # only 3 successes force a definite clue out of an unwilling answerer
        obs = self.call('agent/observe', self.pc(aid))
        tcid = g['players'][target]['character_id']
        ths = (obs.get('thresholds') or {}).get('by_target', {}).get(str(tcid), {})
        approach = min(('question', 'persuade', 'interrogate', 'threaten'), key=lambda x: (ths.get(x, 7), x != 'question'))
        ex = known_sets(list(self.ch(g, aid)['information'].values()))
        hyp = {k: next(i for i in range(SIZES[k]) if i not in ex[k]) if len(ex[k]) < SIZES[k] else 0 for k in CATS}
        return ('question', {'target_id': tcid, 'approach': approach, 'hypothesis': hyp})

    def trade_body(self, g, a, b):
        give = self.lacking(g, a, b)     # what a holds that is new for b
        get = self.lacking(g, b, a)      # what b holds that is new for a
        if not give or not get:
            return None
        want = sorted(get, key=lambda x: x['kind'] != 'definite')[:3]
        offer = sorted(give, key=lambda x: x['kind'] != 'definite')[:3]
        return {'target_id': g['players'][b]['character_id'], 'offer_types': [ttype(x) for x in offer],
                'offer_ids': [x['id'] for x in offer],
                'requested': [{'type': ttype(x), 'exclude': self.exclusions(g, a, ttype(x))} for x in want]}

    def answer_all(self):
        for _ in range(4):
            pending = False
            for aid in self.ids:
                for q in self.call('questions/inbox', self.pc(aid))['questions']:
                    pending = True
                    self.answer(aid, q)
            if not pending:
                return

    def answer(self, aid, q):
        o, n, extra = q['response_options'], q['successes'], q.get('extra_vague', 0)
        g = self.state()
        asker_has = set(self.ch(g, q['asker_id'])['information'])
        defs = sorted(o['definite'], key=lambda c: c in asker_has)
        definite = [{'kind': 'definite', 'clue_id': c} for c in defs[:1]]
        vague = [{'kind': 'ambiguous', 'pair': dict(x['pair'])} for x in o['ambiguous']]
        vague += [{'kind': 'indirect', 'clue_id': x['clue_id'], 'text': x['narrative_options'][0]} for x in o['indirect']]
        if n <= 0: s = []
        elif n == 1: s = vague[:1 + extra] or definite
        elif n == 2:
            if self.scenario == 'A': s = (definite + vague[:extra]) if definite else vague[:2 + extra]
            else: s = vague[:2 + extra] if vague else definite
        else: s = definite + vague[:2 + extra]
        for attempt in (s, None):
            if attempt is None:   # the engine's tier rule rejected it: fall back to the smallest legal answer
                attempt = _required(q)
            try:
                self.call('questions/respond', self.pc(aid), question_id=q['question_id'], response_id=f'r{self.seq}', statements=attempt)
                self.seq += 1
                return
            except ContractError:
                self.seq += 1
        # leave it to the engine (should not happen)

    def deduction(self):
        for aid in self.ids:
            self.act(aid, 'pass')
        self.advance()

    # ---- measurement
    def snapshot(self, cycle):
        g = self.state()
        out = {}
        remaining_rooms = sum(len([c for c in g['clues'] if c['id'] not in set(g.get('room_taken', []))]) for _ in [0])
        for aid in self.ids:
            c = self.ch(g, aid)
            info = c['information']
            usable = [v for v in info.values() if v.get('kind') in ('definite', 'indirect')]
            pairs = g['players'][aid].get('ambiguous_statements', [])
            ex = infer(list(info.values()) + pairs)
            combos = (12 - len(ex['suspect'])) * (8 - len(ex['weapon'])) * (8 - len(ex['place']))
            src = collections.Counter(d['kind'] for d in g['disclosures'] if d.get('recipient') == aid and d.get('clue_id') in info)
            out[c['character_id']] = {
                'usable': len(usable),
                'definite': {k: sum(v.get('kind') == 'definite' and v.get('category') == k for v in usable) for k in CATS},
                'indirect': {k: sum(v.get('type') == k for v in usable) for k in IND},
                'narrated': sum(v.get('kind') == 'narrated' for v in info.values()),
                'pairs': len(pairs), 'combos': combos,
                'excluded': {k: len(ex[k]) for k in CATS},
                'source': {'start': len(self.start_ids[aid] & set(info)), **dict(src)},
            }
        return {'cycle': cycle, 'players': out, 'room_left': remaining_rooms}

    def run(self):
        snaps, left_by_cycle = [], []
        for cycle in range(1, self.deadline + 1):
            self.movement()
            self.interaction()
            g = self.state()
            left = len(g['clues']) - len(set(g.get('room_taken', [])))
            left_by_cycle.append(left)
            if self.dry_cycle is None and left == 0:
                self.dry_cycle = cycle
            if cycle in SNAPSHOTS:
                snaps.append(self.snapshot(cycle))
            self.deduction()
        g = self.state()
        with self.e.transaction() as db:
            qs = [json.loads(r[0]) for r in db.execute("SELECT state FROM competitive_records WHERE game_id='sim' AND kind='question'")]
        acts = collections.Counter(x['kind'] for x in g['activity'])
        game = {'trades': dict(collections.Counter(t['status'] for t in g['trades'].values())),
                'questions': len(qs), 'successes': dict(collections.Counter(q.get('successes') for q in qs)),
                'activity': dict(acts), 'room_left_by_cycle': left_by_cycle, 'clues_total': len(g['clues'])}
        return {'seed': self.seed, 'scenario': self.scenario, 'dry_cycle': self.dry_cycle, 'game': game, 'snapshots': snaps}


def ttype(x):
    return x['category'] if x.get('kind') == 'definite' else x.get('type')


def known_sets(infos):
    """infer() keys candidates as strings once the state went through JSON; trades need ints."""
    ex = infer(infos)
    return {k: {int(x) for x in ex[k]} for k in CATS}


def _required(q):
    o, n, extra = q['response_options'], q['successes'], q.get('extra_vague', 0)
    definite = [{'kind': 'definite', 'clue_id': c} for c in o['definite'][:1]]
    vague = [{'kind': 'ambiguous', 'pair': dict(x['pair'])} for x in o['ambiguous']]
    vague += [{'kind': 'indirect', 'clue_id': x['clue_id'], 'text': x['narrative_options'][0]} for x in o['indirect']]
    if n <= 0: return []
    if n == 1: return vague[:1 + extra] or definite
    if n == 2: return (definite + vague[:extra]) if definite else vague[:2 + extra]
    return definite + vague[:2 + extra]


def one(args):
    seed, scenario, deadline, tmp = args
    try:
        return Sim(seed, scenario, deadline, tmp).run()
    except Exception as exc:  # report, never hide
        import traceback
        return {'seed': seed, 'scenario': scenario, 'error': f'{type(exc).__name__}: {exc}', 'trace': traceback.format_exc()[-1500:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seeds', type=int, default=100)
    ap.add_argument('--first-seed', type=int, default=1)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument('--deadline', type=int, default=30)
    ap.add_argument('--scenarios', default='A,B')
    ap.add_argument('--out', type=Path, default=ROOT / 'runs' / 'sim-capacity')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='simcap-')
    jobs = [(s, sc, args.deadline, tmp) for sc in args.scenarios.split(',') for s in range(args.first_seed, args.first_seed + args.seeds)]
    with ProcessPoolExecutor(args.workers) as pool, open(args.out / 'results.jsonl', 'a', encoding='utf-8') as f:
        for n, r in enumerate(pool.map(one, jobs), 1):
            f.write(json.dumps(r, ensure_ascii=False) + '\n'); f.flush()
            if 'error' in r:
                print('ERROR', r['scenario'], r['seed'], r['error'], flush=True)
            if n % 10 == 0:
                print(f'{n}/{len(jobs)} done', flush=True)


if __name__ == '__main__':
    main()
