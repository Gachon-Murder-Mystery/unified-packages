"""Authoritative competitive rules v1.3. No normal GM/player decision policy.

Runs inside ExternalEngine's SQLite transaction. Only engine rules choose NPC
responses or mandatory timeout disclosure. Player/GM choices stay external.
"""
import copy
import hashlib
import json
import math
import random
import secrets
import uuid

from integration import VERSION, ContractError, require, integer, identifier, text, fingerprint, token_matches
from competitive_rules import DATA, MBTI, SHEETS, MISSIONS, TIERS, REWARDS, tier, candidates, build_case, infer, narrative_options, mission_complete, mission_feasible
from engine import BACKGROUNDS, PERSONALITIES

SPEC = 'competitive-v1.3'
LEGAL = {'movement': ['move', 'stay'], 'interaction': ['explore', 'question', 'prepare', 'pass'],
         'deduction': ['accuse', 'pass']}
RULES = {'gm_lease': 40, 'player_lease': 40, 'question_timeout': 30, 'trade_timeout': 30,
         'deadline_cycles': 120, 'min_players': 3, 'max_players': 12,
         'successor_scope': 'active_then_pool', 'relationship_effects': True,
         # [FIX-A1] Wall-clock budget known to the engine (0 = off). The driver used to
         # enforce it alone, so players were told 106+ cycles remained when the game ended.
         'time_limit_seconds': 0,
         # [FIX-A3] Score of a surviving non-winner when the game ends unsolved
         # (deadline/time_out). v1.3 was 0. [FIX-A24] user decision 2026-11-08: -10 for every
         # survivor (missions void too), so an unsolved game is worse for all than anyone winning.
         'unsolved_score': -10,
         # [FIX-A4] Sub-missions are bonus points. If the game ends unsolved (deadline/time_out)
         # survivors lose them; a player who died by a wrong accusation keeps them.
         # False restores v1.3 (missions always count).
         'void_missions_if_unsolved': True,
         # [FIX-A29 -> A30] MBTI corrections to rolls/scores/relationship gains (MBTI_RULES). OFF by default:
         # user decision 2026-11-08, abilities belong to the job only; MBTI only tilts the agent's choices.
         # Kept as an experimental switch for comparison runs.
         'mbti_rules': False,
         # [FIX-A34] user decision 2026-11-09: the relationship between pool agents (outside the game)
         # belongs to each agent, which scores its own impressions (starts at 50). An engine-kept value
         # would turn it into a stat. True restores FIX-A28 (engine carries in-game changes over).
         'engine_agent_relationships': False,
         # [FIX-A37] user decision 2026-11-09: a personality is not a game stat. The v1.3 owner multiplier on
         # in-game relationship changes (E x1.5, I x0.5) is off; True restores it.
         'ei_relationship_multiplier': False,
         # [FIX-A33] who drives the game: 'gm' = the GM requests actions, closes negotiation, declares
         # timeouts and advances; the engine only referees. 'engine' = the old automatic behaviour.
         'orchestration': 'gm',
         # [FIX-A9] Room clues are first come, first served: a clue taken from a room is gone
         # for everyone else (players still pass it on by answers and trades). 'shared' restores
         # v1.3, where every player could take its own copy. Every explore also reports how many
         # clues of that room the explorer can still get (free, no extra action).
         'room_clues': 'first_come',
         # [FIX-A17] 'simultaneous': in the interaction phase everyone submits an action or a
         # trade proposal at once; actions are held until every proposal is answered (accepting
         # forfeits your held action), then executed. Trade decisions are sealed (now /
         # after_partner / withhold) and settled together. Only a mechanical timeout cancels a
         # trade or fails a question, and it gives the players who spent their action a
         # replacement. 'immediate' keeps v1.3 (actions run at once, trades are free side deals).
         'interaction_order': 'simultaneous',  # default since 2026-11-07 (user decision)
         # [FIX-A19] 'boosted': everyone starts with 1 definite + 2 indirect clues (copies; rooms keep
         # them, so a search can turn up one you already hold), observe-skill jobs get one more clue per
         # successful search, and answers carry extra vague slots: round(10% of the answerer's clues)
         # plus one after half the deadline. 'v13' keeps the original flow.
         'clue_flow': 'v13',
         # [FIX-A38] user decision 2026-11-10: a player may PROPOSE (counters included) this many declared
         # trades per game; answering (accept/refuse) is unlimited. Jobs may add more (TRADE_PROPOSAL_BONUS).
         # Each player sees only its own count. 0 = unlimited (before A38).
         'trade_proposals_per_game': 6,
         # [FIX-A39] user decision 2026-11-10: winner takes all. When someone solves the case, surviving
         # non-winners score 0 and their sub-mission points do not count (a wrong accusation's dead keep
         # theirs, as before). False restores A24 (survivors of a solved game keep their missions).
         'void_missions_if_someone_wins': True,
         # [FIX-A40] user decision 2026-11-10: scores accumulate across games per pool agent. After a solved
         # game each first-ranked player may give any part of its own score of that game to other pool
         # players (its call; giving nothing is fine). Only the cumulative standings are announced to all.
         'post_game_sharing': True,
         # [FIX-A42] user decision 2026-11-10: talk rooms (담화). Rounds every opened room runs before the
         # interaction actions (a fixed share of the cycle, not seconds). 0 = no talk rooms.
         'talk_rounds': 3}
TRADE_PROPOSAL_BONUS = {'사업가': 3}   # [FIX-A38] by job name; the 사업가 skill (persuade -1) is unchanged
FULFILL = ('now', 'after_partner', 'withhold')
TRADE_REPLACEMENT = ['explore', 'question', 'prepare', 'pass']
# [FIX-A25] declared-type trades (user design 2026-11-08). A trade names TYPES, not clue IDs:
# definite clues by category, indirect clues by their three kinds. Requests may exclude numbers.
TRADE_TYPES = ('suspect', 'weapon', 'place', 'character_room', 'weapon_room', 'companion')
TYPE_RANGE = {'suspect': 12, 'weapon': 8, 'place': 8, 'character_room': 12, 'weapon_room': 8, 'companion': 12}
TALK_LIMIT = 200  # [FIX-A27] free talk attached to questions, answers and trades
WIN_SCORES = [100, 70, 50, 30]
WRONG_ACCUSE_SCORE = -20
# [FIX-A29] user decision 2026-11-08: each MBTI axis gets a real strength and a real cost in play.
# Numbers are first proposals, to be tuned from experiments (too weak / too strong).
MBTI_RULES = {
    # corrections only (user 2026-11-08): weights that tilt play, never prohibitions; summed per action, capped at +-2
    'cap': 2,
    'question_gain': {'E': 8, 'I': 3},          # target's relationship to the asker after a successful question/persuade (base 5)
    'I_published': 0.5,                          # an I's question pair is published with this chance (E: always)
    'N_original': 0.5,                           # each indirect clue an N receives in an answer arrives as the original with this chance
    'S_explore': -1, 'S_question_family': 1,     # S: searching easier, asking harder
    'N_win_bonus': 10, 'N_wrong': -25,           # N: correct +10, wrong accusation -25 (base -20)
    'T_persuade': -1, 'T_good_lost': 1,          # T: persuade easier; a good relationship (70+) no longer helps (cancels the base -1)
    'F_good': -2, 'F_bad': 2, 'F_pressure': 1,   # F: relationship modifier doubled; interrogate/threaten harder
    'trade_gain': {'J': 15, 'P': 5},             # partner's relationship after an honest trade (base 10): J builds trust fast, P slowly
    'J_broken': -30,                             # victim's relationship after a J deceives (base -20): a broken promise costs a J more
}


def catalog():
    from integration import catalog as legacy
    result = legacy()
    result.update(ruleset=SPEC, mode='competitive', characters=copy.deepcopy(DATA['characters']),
                  mbti=list(MBTI), missions=copy.deepcopy(DATA['missions']), sub_missions=copy.deepcopy(DATA['missions']),
                  default_rules=RULES.copy(), allowed_actions=copy.deepcopy(LEGAL), temporary_rules=[],
                  scoring={'ranks': [100, 70, 50, 30], 'dead': -20, 'survivor': 0, 'missions': REWARDS.copy()},
                  narrative_validation='closed-truth-preserving-grammar; arbitrary unverifiable prose rejected')
    for c in result['characters']:
        c.update(background=BACKGROUNDS[c['id']], voice=PERSONALITIES[c['id']])
    return result


def pool_key(pid): return 'competitive-pool:'+pid


class Rules:
    def __init__(self, engine): self.e = engine
    def tables(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS competitive_records(game_id TEXT, kind TEXT, key TEXT, state TEXT NOT NULL, PRIMARY KEY(game_id,kind,key))')
        db.execute('CREATE TABLE IF NOT EXISTS competitive_events(game_id TEXT, sequence INTEGER, state TEXT NOT NULL, PRIMARY KEY(game_id,sequence))')
    def record(self, db, g, kind, key):
        self.tables(db)
        row = db.execute('SELECT state FROM competitive_records WHERE game_id=? AND kind=? AND key=?', (g['id'],kind,key)).fetchone()
        return json.loads(row[0]) if row else None
    def persist(self, db, g):
        self.tables(db)
        for e in g['events']:
            db.execute('INSERT OR REPLACE INTO competitive_events VALUES (?,?,?)',(g['id'],e['sequence'],json.dumps(e,ensure_ascii=False)))
        g['events'] = []
        for field, kind in (('commands','command'),('trade_receipts','trade_message'),('receipts','action'),('questions','question')):
            for key, value in list(g[field].items()):
                pending = value['result']['execution_status'] in ('pending', 'held') if field == 'receipts' else value['status'] == 'pending' if field == 'questions' else False
                if not pending:
                    db.execute('INSERT OR REPLACE INTO competitive_records VALUES (?,?,?,?)', (g['id'],kind,key,json.dumps(value,ensure_ascii=False)))
                    del g[field][key]
        self.e.put(db,'games',g['id'],g,'id')
    def all_events(self, db, g, after=0):
        self.tables(db)
        return [json.loads(x[0]) for x in db.execute('SELECT state FROM competitive_events WHERE game_id=? AND sequence>? ORDER BY sequence',(g['id'],after))]+[x for x in g['events'] if x['sequence']>after]
    def pool(self, db, pid):
        row = db.execute('SELECT value FROM integration_config WHERE key=?', (pool_key(pid),)).fetchone()
        require(row is not None, 'not_found', 'bootstrap the competitive pool first')
        import json
        return json.loads(row[0])
    def save_pool(self, db, pool):
        import json
        db.execute('INSERT OR REPLACE INTO integration_config VALUES (?,?)',
                   (pool_key(pool['pool_id']), json.dumps(pool)))
    def bootstrap(self, db, d):
        self.e.operator(d)
        pid = identifier(d.get('pool_id', 'society'), 'pool_id')
        row = db.execute('SELECT value FROM integration_config WHERE key=?', (pool_key(pid),)).fetchone()
        if row:
            p = self.pool(db, pid)
            if 'model_version' in d:
                require(d['model_version'] == p['model_version'], 'conflict', 'pool model is immutable')
            return {'pool_id': pid, 'current_gm_id': p['current_gm_id'], 'agents': self.cards(db, p)}
        model = text(d.get('model_version', 'gpt-6-luna'), 'model_version', 100)
        gm = identifier(d.get('gm_agent_id', pid+':gm'), 'gm_agent_id')
        ids = [gm]+[pid+':'+uuid.uuid4().hex for _ in MBTI]
        for index, aid in enumerate(ids):
            a = self.e.register(db, d | {'agent_id': aid, 'model_version': model})
            a.update(pool_id=pid, fixed_mbti=None if aid == gm else MBTI[index-1],
                     pool_role='gm' if aid == gm else 'player', history=[], negotiation_reputation={'fulfilled': 0, 'broken': 0})
            self.e.put(db, 'agents', aid, a)
        p = {'pool_id': pid, 'agent_ids': ids, 'current_gm_id': gm, 'model_version': model,
             'random_secret': secrets.token_hex(32)}
        self.save_pool(db, p)
        return {'pool_id': pid, 'current_gm_id': gm, 'agents': self.cards(db, p)}
    def cards(self, db, pool):
        result = []
        for aid in pool['agent_ids']:
            a = self.e.load(db, 'agents', aid)
            l = self.e.load(db, 'agent_learning', aid)
            result.append({k: copy.deepcopy(a[k]) for k in ('agent_id', 'model_version', 'pool_status', 'busy_game_id',
                           'fixed_mbti', 'pool_role', 'history', 'negotiation_reputation', 'agent_relationships') if k in a} | {'learning_version': l['version']})
        return result
    def create(self, db, d):
        self.e.operator(d)
        pid = identifier(d.get('pool_id', 'society'), 'pool_id')
        pool = self.pool(db, pid)
        gid = identifier(d.get('game_id', str(uuid.uuid4())), 'game_id')
        require(not db.execute('SELECT 1 FROM games WHERE id=?', (gid,)).fetchone(), 'conflict', 'game_id exists')
        seed = integer(d.get('seed', 0), 'seed', 0, 2**32-1)
        provenance=d.get('provenance',{})
        require(isinstance(provenance,dict) and len(provenance)<=12 and all(isinstance(k,str) and isinstance(v,str) and len(k)<=80 and len(v)<=500 for k,v in provenance.items()),'invalid_input','bounded string provenance required')
        require(isinstance(d.get('rules', {}), dict), 'invalid_input', 'rules must be an object')
        rules = RULES | d.get('rules', {})
        require(set(rules) == set(RULES), 'invalid_input', 'unknown competitive rule')
        for k in ('gm_lease', 'player_lease', 'question_timeout', 'trade_timeout'):
            require(type(rules[k]) in (int, float) and math.isfinite(rules[k]) and 1 <= rules[k] <= 3600,
                    'invalid_input', 'invalid timeout')
        integer(rules['deadline_cycles'], 'deadline_cycles', 1, 1000)
        integer(rules['time_limit_seconds'], 'time_limit_seconds', 0, 7*86400)
        integer(rules['unsolved_score'], 'unsolved_score', WRONG_ACCUSE_SCORE, 0)
        require(type(rules['void_missions_if_unsolved']) is bool, 'invalid_input', 'void_missions_if_unsolved must be boolean')
        integer(rules['talk_rounds'], 'talk_rounds', 0, 10)   # [FIX-A42]
        for k in ('void_missions_if_someone_wins', 'post_game_sharing'):   # [FIX-A39/A40]
            require(type(rules[k]) is bool, 'invalid_input', f'{k} must be boolean')
        require(type(rules.get('mbti_rules', False)) is bool, 'invalid_input', 'mbti_rules must be boolean')
        require(type(rules.get('engine_agent_relationships', False)) is bool, 'invalid_input',
                'engine_agent_relationships must be boolean')
        require(type(rules.get('ei_relationship_multiplier', False)) is bool, 'invalid_input',
                'ei_relationship_multiplier must be boolean')
        require(rules.get('orchestration', 'gm') in ('gm', 'engine'), 'invalid_input', 'orchestration is gm or engine')
        require(rules['room_clues'] in ('first_come', 'shared'), 'invalid_input', 'room_clues is first_come or shared')
        require(rules['interaction_order'] in ('immediate', 'simultaneous'), 'invalid_input', 'interaction_order is immediate or simultaneous')
        require(rules['clue_flow'] in ('v13', 'boosted'), 'invalid_input', 'clue_flow is v13 or boosted')
        require(rules['min_players'] == 3 and rules['max_players'] == 12 and
                rules['successor_scope'] == 'active_then_pool' and type(rules['relationship_effects']) is bool,
                'invalid_input', 'v1.3 requires 3..12 players and active_then_pool succession')
        gm = pool['current_gm_id']
        self.e.reserve(db, gm, gid)
        truth, timeline, clues = build_case(seed)
        chars = {}
        for cid, sheet in SHEETS.items():
            chars[str(cid)] = copy.deepcopy(sheet) | {'character_id': cid, 'controller': None,
                'mode': 'npc', 'mbti': MBTI[cid], 'goal': None, 'room': sheet['start_room'],
                'prepared': False, 'information': {c['id']: public_clue(c) for c in clues if c['source_room'] == sheet['start_room']}}
        g = {'id': gid, 'kind': 'external', 'mode': 'competitive', 'ruleset': SPEC, 'schema_version': VERSION,
             'pool_id': pid, 'seed': seed, 'rules': rules, 'truth': truth, 'timeline': timeline, 'clues': clues,'provenance':copy.deepcopy(provenance),
             'roll_secret': hashlib.sha256(f'{pool["random_secret"]}:roll:{seed}'.encode()).hexdigest(),
             'roll_counts': {}, 'characters': chars, 'players': {}, 'assignments': [],
             'controller': None, 'term': 0, 'cycle_id': 1, 'phase': 'movement', 'phase_epoch': 1,
             'spent': {}, 'map_version': 0, 'plans': [], 'events': [], 'receipts': {}, 'commands': {},
             'questions': {}, 'question_pairs': [], 'recent_questions': [], 'trades': {}, 'trade_receipts': {}, 'room_taken': [],
             'transfer_count': 0, 'relationships': {str(i): {str(j): 50.0 for j in range(12) if i != j} for i in range(12)},
             'activity': [], 'cycle_history': [], 'disclosures': [], 'deductions': {}, 'announcements': {},
             'status': 'awaiting_setup', 'ended': False, 'resolution': None, 'started': False,
             'failovers': 0, 'technical_failure': False, 'created_at': self.e.clock(), 'finished_at': None,
             'observer_token': secrets.token_urlsafe(32), 'gm_report': None,
             'standings_at_start': self.standings(db, pid)}   # [FIX-A40] public cumulative scores
        self.e.controller(g, gm)
        self.e.event(g, 'gm_selected', {'controller_id': gm, 'term': 1, 'mode': 'fixed-incumbent'})
        self.persist(db, g)
        return self.e.credentials(g) | {'observer_token': g['observer_token'], 'state': self.gm_view(db, g)}
    def ch(self, g, p): return g['characters'][str(p['character_id'])]
    def player(self, g, d, read=False):
        aid = identifier(d.get('agent_id'), 'agent_id')
        p = g['players'].get(aid)
        require(p is not None and token_matches(d.get('token'), p['token']) and
                (p['mode'] == 'active' or (read and p['mode'] == 'corpse')), 'unauthorized', 'own current player credential required')
        if not read:
            require(not g['ended'] and g['status'] == 'running' and g['resolution'] is None, 'not_running', 'game is not accepting actions')
        require(read or self.e.clock() < p['lease_until'], 'expired', 'player lease expired')
        return p
    def mission_candidates(self, db, g, activation):
        pool = self.pool(db, g['pool_id'])
        result = {}
        for item in activation:
            aid, cid = item['agent_id'], integer(item['character_id'], 'character_id', 0, 11)
            require(aid in pool['agent_ids'], 'unauthorized', 'agent outside this pool')
            a = self.e.load(db, 'agents', aid)
            previous = [x['mission_id'] for x in a['history'][-1].get('sub_missions', [])] if a['history'] else []
            require(a['pool_role'] == 'player' and a['fixed_mbti'] in MBTI, 'invalid_input', 'GM cannot become a player')
            result[aid] = candidates(g['seed'], aid, cid, a['fixed_mbti'], previous, len(activation),
                                     deadline=g['rules']['deadline_cycles'], rules=g['rules'])  # [FIX-A7, A10, A21]
        return result
    def preview_characters(self, db, g, activation):
        chars = [{k: copy.deepcopy(v) for k, v in c.items() if k in ('character_id','name','job','job_id','stats','skill','mbti')}
                 | {'controller': None} for c in g['characters'].values()]
        used = set()
        for item in activation:
            a = self.e.load(db, 'agents', item['agent_id'])
            c = chars[item['character_id']]
            c.update(controller=item['agent_id'], mbti=a['fixed_mbti'])
            used.add(a['fixed_mbti'])
        remaining = [x for x in MBTI if x not in used]
        random.Random(f'npc-mbti:{g["seed"]}').shuffle(remaining)
        for c in chars:
            if c['controller'] is None: c['mbti'] = remaining.pop()
        return chars
    def plan(self, db, g, d):
        require(not g['started'] and g['status'] == 'awaiting_setup', 'conflict', 'Role assignment is initial-only')
        require('tasks' not in d, 'invalid_input', 'competitive GM assigns Roles, not action tasks')
        require(type(d.get('map_version')) is int and d['map_version'] == g['map_version'], 'conflict', 'map version changed')
        activation = d.get('activate')
        require(isinstance(activation, list) and 3 <= len(activation) <= 12 and all(isinstance(x, dict) for x in activation),
                'invalid_input', 'assign three to twelve Roles at once')
        require(len({x.get('agent_id') for x in activation}) == len(activation) and
                len({x.get('character_id') for x in activation}) == len(activation), 'invalid_input', 'duplicate agent or character')
        choices = self.mission_candidates(db, g, activation)
        alternatives = d.get('alternatives', [])
        require(isinstance(alternatives, list) and len(alternatives) >= 2 and
                all(isinstance(x, dict) and isinstance(x.get('id'), str) and isinstance(x.get('summary'), str) for x in alternatives) and
                len({x['id'] for x in alternatives}) == len(alternatives) and
                d.get('selected_alternative') in [x['id'] for x in alternatives], 'invalid_input', 'two alternatives and a selection required')
        reason = text(d.get('reason'), 'reason')
        require(bool(reason.strip()), 'invalid_input', 'selection reason required')
        assigned_chars = {x['character_id'] for x in activation}
        assigned_agents = {x['agent_id']: x['character_id'] for x in activation}
        used_mbti = set()
        for item in activation:
            require(set(item) == {'agent_id', 'character_id', 'sub_missions'}, 'invalid_input', 'Role fields are agent_id, character_id, sub_missions')
            aid, cid = item['agent_id'], item['character_id']
            a = self.e.load(db, 'agents', aid)
            self.e.reserve(db, aid, g['id'])
            # [FIX-A16] pool reputation carried into this game; settlements keep it current
            g.setdefault('reputation', {})[aid] = copy.deepcopy(a.get('negotiation_reputation', {'fulfilled': 0, 'broken': 0}))
            if g['rules'].get('engine_agent_relationships', False):   # [FIX-A34] off by default
                g.setdefault('agent_trust', {})[aid] = copy.deepcopy(a.get('agent_relationships', {}))  # [FIX-A28] as of game start
            mbti = a['fixed_mbti']
            used_mbti.add(mbti)
            missions = item['sub_missions']
            require(isinstance(missions, list) and len(missions) == 3 and all(isinstance(x, dict) for x in missions), 'invalid_input', 'three missions required')
            require({x.get('tier') for x in missions} == set(TIERS) and len({x.get('mission_id') for x in missions}) == 3,
                    'invalid_input', 'one distinct mission per tier')
            for mission in missions:
                self.validate_mission(g, aid, cid, mbti, mission, choices[aid], assigned_chars, assigned_agents)
            assignment = {'assignment_id': str(uuid.uuid4()), 'agent_id': aid, 'character_id': cid,
                          'role': 'player', 'job_id': cid, 'status': 'active'}
            g['assignments'].append(assignment)
            g['players'][aid] = assignment | {'mode': 'active', 'token': secrets.token_urlsafe(32),
                'lease_until': self.e.clock()+g['rules']['player_lease'], 'visited': [SHEETS[cid]['start_room']],
                'acquired': [], 'ambiguous_statements': [], 'first_definite': None, 'relationship_below_30': False,
                'missions': [copy.deepcopy(x) | {'achieved': False, 'achieved_cycle': None} for x in missions]}
            c = g['characters'][str(cid)]
            c.update(controller=aid, mode='active', mbti=mbti, goal={'victory': 'solve_first', 'sub_missions': copy.deepcopy(missions)}, information={})
        rng = random.Random(f'npc-mbti:{g["seed"]}')
        remaining = [x for x in MBTI if x not in used_mbti]
        rng.shuffle(remaining)
        for c in g['characters'].values():
            if c['controller'] is None:
                c['mbti'] = remaining.pop()
        if g['rules'].get('clue_flow') == 'boosted': self.deal_start_clues(g)  # [FIX-A19]
        g.update(started=True, status='running', map_version=g['map_version']+1)
        g['time_started_at'] = self.e.clock()  # [FIX-A1] origin of the wall-clock budget
        g['plans'].append({'command_id': d['command_id'], 'alternatives': copy.deepcopy(alternatives),
                           'selected_alternative': d['selected_alternative'], 'reason': reason,
                           'map_version': g['map_version'], 'term': g['term']})
        self.e.event(g, 'roles_assigned', {'map_version': g['map_version'], 'player_count': len(activation)})
        return {'map_version': g['map_version'], 'started': True}
    def validate_mission(self, g, aid, cid, mbti, m, choices, assigned_chars, assigned_agents):
        mid, t = m.get('mission_id'), m.get('tier')
        require(t in TIERS and mid in choices[t], 'invalid_input', 'mission not in engine candidate slot')
        params, targets = m.get('params', {}), m.get('targets', {})
        require(isinstance(params, dict) and isinstance(targets, dict), 'invalid_input', 'invalid mission parameters')
        require(tier(mid, cid, mbti, params.get('N')) == t, 'invalid_input', 'wrong effective difficulty')
        require(set(params) <= ({'N'} if MISSIONS[mid].get('quantity') else set()), 'invalid_input', 'unsupported mission parameter')
        kind = MISSIONS[mid]['designate']
        ok, why = mission_feasible(mid, g['rules']['deadline_cycles'], len(assigned_agents), cid, targets, g['rules'])  # [FIX-A10]
        require(ok, 'invalid_input', f'mission {mid} cannot be achieved in this game: {why}')
        if kind == 'player':
            require(targets.get('player') in assigned_agents and targets['player'] != aid, 'invalid_input', 'target another participant')
            require('character' not in targets or targets['character'] == assigned_agents[targets['player']], 'invalid_input', 'player/character target mismatch')
        elif kind in ('character', 'npc'):
            target = integer(targets.get('character'), 'target_character', 0, 11)
            require(target != cid and (kind != 'npc' or target not in assigned_chars), 'invalid_input', 'invalid character target')
        elif kind == 'place': integer(targets.get('place'), 'place', 0, 7)
        elif kind == 'indirect_clues':
            ids = targets.get('clue_ids', [])
            possible = {c['id'] for c in g['clues'] if c['kind'] == 'indirect'}
            require(isinstance(ids, list) and len(ids) == 3 and len(set(ids)) == 3 and all(x in possible for x in ids), 'invalid_input', 'three distinct indirect IDs required')
        elif kind is None: require(not targets, 'invalid_input', 'mission has no designated target')
        if kind == 'character' and 'player' in targets:
            require(targets['player'] in assigned_agents and assigned_agents[targets['player']] == targets['character'],
                    'invalid_input', 'designated player does not control that character')
        allowed = {'player', 'character', 'target_ei'} if kind in ('player','character') else {'character', 'target_ei'} if kind == 'npc' else {'clue_ids'} if kind == 'indirect_clues' else {'place'} if kind == 'place' else set()
        require(set(targets) <= allowed, 'invalid_input', 'unknown mission target field')
    def visible(self, g):
        return {'schema_version': VERSION, 'ruleset': SPEC, 'game_id': g['id'], 'mode': 'competitive',
            'cycle_id': g['cycle_id'], 'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'status': g['status'],
            'ended': g['ended'], 'resolution': g['resolution'], 'remaining_cycles': 0 if g['resolution']=='deadline' else max(0, g['rules']['deadline_cycles']-g['cycle_id']+1),
            'positions': [{'id': c['character_id'], 'character_id': c['character_id'], 'room': self.public_room(g, c),
                           'mode': c['mode'], 'name': c['name'], 'job': c['job']} for c in g['characters'].values()],
            'placements': copy.deepcopy(g.get('placements', [])),
            'question_pairs': [copy.deepcopy(x) for x in g['question_pairs'] if not x.get('hidden')],
            'announcements': [copy.deepcopy(x['data']) for x in g['announcements'].values()
                              if x['published'] and not x.get('private_to')],   # [FIX-A36] private notices stay private
            'time_budget': self.time_budget(g), 'scoring': self.scoring(g)}
    def action_request(self, g, c, allowed, thresholds, occupants):
        """[FIX-A32] What the GM asks of this player now: only legal actions, each with its legal targets."""
        living = [x for x in occupants if x['mode'] != 'corpse']
        corpses = [x for x in occupants if x['mode'] == 'corpse']
        opts = {}
        for a in allowed:
            if a == 'move': opts[a] = {'rooms': list(range(8))}
            elif a == 'explore':
                opts[a] = {'room': c['room'], 'threshold': thresholds.get('explore'),
                           'corpse_targets': [{'id': x['id'], 'threshold': thresholds.get('explore_corpse')} for x in corpses]}
            elif a == 'question':
                opts[a] = {'targets': [{'id': x['id'], 'name': x.get('name'),
                                        'thresholds': thresholds.get('by_target', {}).get(str(x['id']))} for x in living]}
            elif a == 'trade': opts[a] = {'targets': [x['id'] for x in living]}
            else: opts[a] = {}
        return {'issued_by': 'gm', 'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'cycle': g['cycle_id'],
                'my_room': c['room'], 'options': opts}
    def my_mbti_rules(self, g, c):
        """[FIX-A29] The engine effects of my own type, so the player can play to them."""
        if not g['rules'].get('mbti_rules', False): return {}
        m, r, out = c.get('mbti', ''), MBTI_RULES, {}
        if 'E' in m: out['E'] = {'question_success_gain': r['question_gain']['E'], 'question_pair_published': 1.0}
        if 'I' in m: out['I'] = {'question_success_gain': r['question_gain']['I'], 'question_pair_published': r['I_published']}
        if 'S' in m: out['S'] = {'explore_threshold': r['S_explore'], 'question_family_threshold': r['S_question_family']}
        if 'N' in m: out['N'] = {'indirect_from_answers_original_chance': r['N_original'], 'win_bonus': r['N_win_bonus'], 'wrong_accusation': r['N_wrong']}
        if 'T' in m: out['T'] = {'persuade_threshold': r['T_persuade'], 'relationship_modifier': '70 up 0 / 30 down +1'}
        if 'F' in m: out['F'] = {'relationship_modifier': '70 up %d / 30 down +%d' % (r['F_good'], r['F_bad']), 'interrogate_threaten_threshold': r['F_pressure']}
        if 'J' in m: out['J'] = {'honest_trade_gain': r['trade_gain']['J'], 'deception_loss': r['J_broken']}
        if 'P' in m: out['P'] = {'honest_trade_gain': r['trade_gain']['P'], 'counter_cost': 'none until it settles'}
        out['cap'] = 'MBTI threshold corrections are summed per action and kept within +-%d' % r['cap']
        return out
    def occupant(self, g, x, viewer=None):
        """[FIX-A16] A player-controlled character in my room comes with the pool agent behind it
        and that agent's trade record over all games (fulfilled/broken), so a trader met before
        can be recognised: the plan's "reputation stays and you may meet the same partner again"."""
        # [FIX-A26] user decision 2026-11-08: the engine's trade record stays hidden. Only the pool
        # agent behind the character is shown, so an agent can apply what IT remembers of that agent.
        aid = g['characters'][str(x['id'])].get('controller')
        if aid in g['players'] and x['mode'] == 'active':
            out = dict(x, agent_id=aid)
            if viewer and g['rules'].get('engine_agent_relationships', False):  # [FIX-A28/A34]
                out['my_agent_relationship'] = g.get('agent_trust', {}).get(viewer, {}).get(aid, 50.0)
            return out
        return x
    def allocate_explores(self, db, g):
        """[FIX-A14] Settle this phase's room searches. In each room the searcher with the higher
        chance to succeed (1 - ((threshold-1)/6)^dice: skills, preparation) takes first, as many
        clues as its own successes give; equal chances are ordered at random (engine seed).
        Every searcher then learns what the room still holds for it, after all takes."""
        pending, taken = g.pop('pending_explores', []), g.setdefault('room_taken', [])
        def chance(x): return 1-((x['threshold']-1)/6)**x['dice']
        boosted = g['rules'].get('clue_flow') == 'boosted'
        def open_for(aid, room):
            known = self.ch(g, g['players'][aid])['information']
            return [public_clue(y) for y in g['clues'] if y['source_room'] == room and y['id'] not in taken and
                    (boosted or y['id'] not in known or known[y['id']].get('kind') in ('possession', 'narrated'))]
        for room in sorted({x['room'] for x in pending}):
            group = [x for x in pending if x['room'] == room]
            group.sort(key=lambda x: (-chance(x), self.rng(g, f"explore-order:{g['cycle_id']}:{room}:{x['action_id']}").random()))
            for x in group:
                options = open_for(x['agent_id'], room)
                self.rng(g, x['action_id']+':loot').shuffle(options)
                count = 2 if x['successes'] == 3 else int(x['successes'] > 0)
                if count: count += x.get('bonus', 0)  # [FIX-A19] observe-skill jobs
                got = options[:count]
                new = [info for info in got if self.acquire(g, x['agent_id'], info, 'explore')]
                if len(new) >= 2:  # [FIX-A21] M55
                    g['players'][x['agent_id']]['multi_finds'] = g['players'][x['agent_id']].get('multi_finds', 0) + 1
                taken.extend(info['id'] for info in got)
                g['receipts'][x['action_id']]['result'].update(
                    acquired_information=new, duplicates=len(got) - len(new), outcome='acquired' if new else 'no_information',
                    cost_incurred=x['successes'] == 1, execution_status='completed')
            for x in group:
                result = g['receipts'][x['action_id']]['result']
                result['room_remaining'] = len(open_for(x['agent_id'], room))
                self.complete_action(db, g, result)
        self.award_missions(g)
    def deal_start_clues(self, g):
        """[FIX-A19] 1 definite + 2 indirect per player, no two players share one. Copies: the rooms
        keep their clues. Recorded in start_clues so 'acquire' missions count only what is found."""
        rng = self.rng(g, 'start-clues')
        definite = [x for x in g['clues'] if x['kind'] == 'definite']
        indirect = [x for x in g['clues'] if x['kind'] == 'indirect']
        rng.shuffle(definite); rng.shuffle(indirect)
        for n, aid in enumerate(sorted(g['players'], key=lambda a: g['players'][a]['character_id'])):
            c = self.ch(g, g['players'][aid])
            dealt = definite[n:n+1] + indirect[2*n:2*n+2]
            for x in dealt: c['information'][x['id']] = public_clue(x)
            g['players'][aid]['start_clues'] = [x['id'] for x in dealt]
    def search_bonus(self, g, c, target):
        """[FIX-A19] Observe-skill jobs find one more clue per successful search where their skill applies."""
        if g['rules'].get('clue_flow') != 'boosted': return 0
        cid, room, corpse = c['character_id'], c['room'], target is not None
        return int(cid == 0 or (cid == 6 and corpse) or (not corpse and ((cid == 7 and room == 5) or
                   (cid == 8 and room == 7) or (cid == 9 and room == 3))))
    def extra_vague(self, g, target):
        """[FIX-A19] Extra vague slots: round(10% of the answerer's full clues) + 1 after half the deadline."""
        if g['rules'].get('clue_flow') != 'boosted': return 0
        held = sum(x.get('kind') in ('definite', 'indirect') for x in target['information'].values())
        late = int(g['cycle_id'] > g['rules']['deadline_cycles'] / 2)
        return int(held / 10 + 0.5) + late
    def public_room(self, g, c):
        """[FIX-A13] During the movement phase a move is not public yet: everyone sees the last
        revealed placement (start rooms before the first one). Moves used to show up the moment
        they were submitted, so later movers saw earlier movers' new rooms."""
        if g['phase'] != 'movement' or not g.get('started'):
            return c['room']
        placements = g.get('placements') or []
        if placements:
            return placements[-1]['rooms'].get(str(c['character_id']), c['room'])
        return SHEETS[c['character_id']]['start_room']
    def time_budget(self, g):
        """[FIX-A1] What a player needs to know about the real end of the game.

        remaining_cycles only counts deadline_cycles; with a wall-clock limit the game
        may end far earlier. estimated_remaining_cycles is the smaller of the two, using
        the average duration of the cycles already finished (None before cycle 2)."""
        cycles_left = 0 if g['resolution'] == 'deadline' else max(0, g['rules']['deadline_cycles']-g['cycle_id']+1)
        limit, start = g['rules'].get('time_limit_seconds', 0), g.get('time_started_at')
        result = {'time_limit_seconds': limit or None, 'elapsed_seconds': None, 'remaining_seconds': None,
                  'average_cycle_seconds': None, 'estimated_remaining_cycles': cycles_left,
                  'binding_limit': 'deadline_cycles',
                  'on_expiry': 'no winner; every player loses'}
        if start is None:
            return result
        elapsed = max(0.0, self.e.clock()-start)
        result['elapsed_seconds'] = round(elapsed, 1)
        finished = g['cycle_id']-1
        average = elapsed/finished if finished > 0 else None
        result['average_cycle_seconds'] = round(average, 1) if average else None
        if limit:
            remaining = max(0.0, limit-elapsed)
            result['remaining_seconds'] = round(remaining, 1)
            if not average:
                # No finished cycle yet: do not echo deadline_cycles as if it were reachable.
                result.update(estimated_remaining_cycles=None,
                              binding_limit='time_limit_seconds (cycle estimate available from cycle 2)')
            else:
                # The current cycle counts as one if it can still finish in time.
                by_time = int(remaining // average) + (1 if remaining > 0 else 0)
                if by_time < cycles_left:
                    result.update(estimated_remaining_cycles=by_time, binding_limit='time_limit_seconds')
        return result
    def win_scores(self, g, c=None):
        bonus = MBTI_RULES['N_win_bonus'] if c is not None and 'N' in c.get('mbti', '') and g['rules'].get('mbti_rules', False) else 0
        return [x + bonus for x in WIN_SCORES]
    def wrong_score(self, g, c=None):
        return MBTI_RULES['N_wrong'] if c is not None and 'N' in c.get('mbti', '') and g['rules'].get('mbti_rules', False) else WRONG_ACCUSE_SCORE
    def scoring(self, g, c=None):
        """[FIX-A3] The scoring that finish() applies, so players can reason about it. [FIX-A29] per player (N)."""
        void = g['rules'].get('void_missions_if_unsolved', False)
        return {'win_by_rank': self.win_scores(g, c), 'wrong_accusation': self.wrong_score(g, c),
                'unsolved_survivor': g['rules'].get('unsolved_score', 0),
                'sub_mission_rewards': dict(REWARDS),
                'sub_missions_if_unsolved': 'void for survivors' if void else 'kept',
                'sub_missions_if_wrong_accusation': 'kept',
                'sub_missions_if_someone_wins': 'void for survivors' if g['rules'].get('void_missions_if_someone_wins', False) else 'kept',   # [FIX-A39]
                'post_game_sharing': bool(g['rules'].get('post_game_sharing', False))}
    def pair(self, q): return {'question_id': q['question_id'], 'asker_id': q['asker_character'], 'target_id': q['target_id']}
    def gm_view(self, db, g):
        pool = self.pool(db, g['pool_id'])
        c = g['controller']
        cards = self.cards(db, pool)
        chars = [{k: copy.deepcopy(c[k]) for k in ('character_id', 'name', 'job_id', 'stats', 'skill', 'room', 'mode', 'mbti', 'controller')}
                 for c in g['characters'].values()]
        result = self.visible(g) | {'control': {'controller_id': c['controller_id'], 'term': c['term']} if c else None,
            'rules': copy.deepcopy(g['rules']), 'pool': cards, 'characters': chars, 'actors': chars,
            'active_agents': [a for a, p in g['players'].items() if p['mode'] == 'active'],
            'spent': copy.deepcopy(g['spent']), 'event_sequence': g.get('event_count',0), 'map_version': g['map_version'],
            'plans': copy.deepcopy(g['plans']), 'failovers': g['failovers'],
            'agent_map': self.agent_map(g),
            'role_assignments': [copy.deepcopy(a) for a in g['assignments'] if a['role'] == 'player'],
            'indirect_clue_ids': [c['id'] for c in g['clues'] if c['kind'] == 'indirect'],
            'questions': [self.pair(q) | {'pending': q['status'] == 'pending', 'successes': q['successes'],
                         'approach': q['approach'],
                         'transferred_ids': q.get('transferred_ids', [])} for q in list(g['questions'].values())+g['recent_questions']],
            'pending_announcements': [{'kind': a['kind'], 'ref': ref} for ref, a in g['announcements'].items() if not a['published']],
            # [FIX-A33] facts the orchestrating GM acts on
            'orchestration': g['rules'].get('orchestration', 'engine'), 'stage': g.get('stage'),
            'needs_request': self.needs_request(g) if self.gm_led(g) else [],
            'ready_to_release': self.release_ready(g), 'ready_to_advance': self.advance_ready(g),
            'overdue': self.overdue(g),
            'talk': self.talk_facts(g),   # [FIX-A42]
            'gm_report': copy.deepcopy(g['gm_report']), 'metrics': self.metrics(g)}
        if not g['started']:
            # Candidates are character-dependent; draw only for proposed Roles to keep inputs small.
            result['mission_candidate_endpoint'] = 'gm/mission-candidates'
        return result
    @staticmethod
    def gm_led(g):
        return g['rules'].get('orchestration', 'engine') == 'gm'
    def proposal_quota(self, g, aid):
        """[FIX-A38] (per game, left) for this player's trade proposals, or None when unlimited."""
        limit = g['rules'].get('trade_proposals_per_game', 0)
        if not limit or aid not in g['players']: return None
        total = limit + TRADE_PROPOSAL_BONUS.get(self.ch(g, g['players'][aid]).get('job'), 0)
        return total, max(0, total - g.get('proposals_used', {}).get(aid, 0))
    def check_proposal(self, g, aid):
        q = self.proposal_quota(g, aid)
        require(q is None or q[1] > 0, 'conflict', 'no trade proposals left this game')
    def use_proposal(self, g, aid):
        if self.proposal_quota(g, aid) is not None:
            used = g.setdefault('proposals_used', {})
            used[aid] = used.get(aid, 0) + 1
    def player_view(self, g, p, raw=False):
        c = self.ch(g, p)
        info = list(copy.deepcopy(c['information']).values())
        from integration import catalog as old_catalog
        own = {k: copy.deepcopy(c[k]) for k in ('character_id', 'name', 'stats', 'skill', 'room', 'prepared', 'mbti', 'mode', 'goal')}
        own.update(id=p['agent_id'], assignment_id=p['assignment_id'], job={'id': c['job_id'], 'name': c['job']},
                   personality={'id': c['mbti'], 'name': c['mbti']}, personal_goal={'name': 'solve_first'})
        missions = [{k: copy.deepcopy(x[k]) for k in ('mission_id', 'tier', 'targets', 'params', 'achieved', 'achieved_cycle')}
                    for x in p['missions']]
        for m in missions: m['targets'].pop('target_ei', None)
        own['goal'] = {'victory': 'solve_first', 'sub_missions': copy.deepcopy(missions)}
        own['standings'] = copy.deepcopy(g.get('standings_at_start'))   # [FIX-A40] public cumulative scores
        quota = self.proposal_quota(g, p['agent_id'])   # [FIX-A38] my own count only
        if quota is not None: own['trade_proposals'] = {'per_game': quota[0], 'left': quota[1]}
        allowed = LEGAL[g['phase']] if not g['ended'] and g['resolution'] is None and g['status'] == 'running' and p['mode'] == 'active' and p['agent_id'] not in g['spent'] else []
        rep = self.replacement(g, p['agent_id'])  # [FIX-A17]
        if allowed and rep: allowed = rep['allowed']
        elif allowed and self.negotiating(g) and (quota is None or quota[1] > 0): allowed = list(allowed) + ['trade']   # [FIX-A38]
        own['talk'] = self.talk_view(g, p)   # [FIX-A42]
        if g['phase'] == 'interaction' and g.get('stage') == 'talk' and p['mode'] == 'active' and not g['ended']:
            allowed = ['talk'] if (own['talk'] or {}).get('my_turn') else []
        # [FIX-A2] Effective thresholds for every option open right now (skills applied).
        here = [x for x in g['characters'].values() if x['character_id'] != c['character_id'] and x['room'] == c['room']]
        thresholds = {'explore': self.threshold(g, c, 'explore', None),
                      'by_target': {str(t['character_id']): {a: self.threshold(g, c, a, t)
                                    for a in ('question', 'persuade', 'interrogate', 'threaten')}
                                    for t in here if t['mode'] != 'corpse'}}
        if any(t['mode'] == 'corpse' for t in here):
            thresholds['explore_corpse'] = self.threshold(g, c, 'explore', None, corpse=True)
        # [FIX-A32] the GM's action request for this phase: legal actions with their legal targets.
        # Visible positions only (moves stay hidden during the movement phase, FIX-A13).
        occupants = [self.occupant(g, x, p['agent_id']) for x in self.visible(g)['positions']
                     if x['id'] != c['character_id'] and x['room'] == c['room']]
        if allowed and g['phase'] == 'interaction' and not any(x['mode'] != 'corpse' for x in occupants):
            allowed = [a for a in allowed if a not in ('question', 'trade')]   # nobody here to ask or trade with
        request = self.action_request(g, c, allowed, thresholds, occupants) if allowed else None
        if self.gm_led(g) and not raw:  # [FIX-A33] a player acts only on the GM's open request
            req = g.get('requests', {}).get(p['agent_id'])
            if allowed and req and req['phase_epoch'] == g['phase_epoch'] and req['status'] == 'open':
                allowed = [a for a in allowed if a in req['options']]
                request = copy.deepcopy(req)
            else:
                allowed, request = [], None
        return self.visible(g) | {'self': own, 'thresholds': thresholds, 'information': info, 'shared_information': [],
            'relationships': copy.deepcopy(g['relationships'][str(c['character_id'])]), 'sub_missions': missions,
            'statements': copy.deepcopy(p['ambiguous_statements']),
            'interaction_stage': g.get('stage') if g['phase'] == 'interaction' else None, 'replacement': copy.deepcopy(rep),
            'candidates': old_catalog()['candidates'],
            'occupants': occupants, 'action_request': request,
            'question_pairs': [copy.deepcopy(x) for x in g['question_pairs']                      # [FIX-A29] I's pairs: only the two sides
                               if not x.get('hidden') or c['character_id'] in (x['asker_id'], x['target_id'])],
            'scoring': self.scoring(g, c), 'mbti_rules': self.my_mbti_rules(g, c),
            'allowed_actions': list(allowed), 'remaining_actions': int(bool(allowed)), 'lease_until': p['lease_until'],
            'final_feedback': copy.deepcopy(g.get('final_feedback')) if g['ended'] else None}
    def metrics(self, g):
        return {'questions': len(g['question_pairs']), 'transfers': g['transfer_count'],
            'indirect_links': sum(x.get('linked', False) for x in g['disclosures']),
            'fulfilled_trades': sum(x['status'] == 'settled' and all(x['fulfilled'].values()) for x in g['trades'].values()),
            'broken_trades': sum(x['status'] == 'settled' and not all(x['fulfilled'].values()) for x in g['trades'].values()),
            'relationship_updates': g.get('event_counts',{}).get('relationship_changed',0),
            'completed_missions': sum(x['achieved'] for p in g['players'].values() for x in p['missions']), 'gm_failovers': g['failovers']}

    def relationship_graph(self, g, owner=None):
        return {'directed': True, 'range': [0, 100], 'initial': 50, 'scope': 'game',
                'nodes': [{'id': c['character_id'], 'name': c['name']} for c in g['characters'].values()],
                'edges': [{'source': int(a), 'target': int(b), 'weight': weight}
                          for a, edges in g['relationships'].items() if owner is None or int(a) == owner
                          for b, weight in edges.items()]}
    def agent_map(self,g):
        nodes=[{'id':'character:'+str(c['character_id']),'type':'Character','mode':c['mode']} for c in g['characters'].values()]
        nodes += [{'id':'location:'+str(i),'type':'Location'} for i in range(8)]
        edges=[{'source':'character:'+str(c['character_id']),'relation':'locatedAt','target':'location:'+str(c['room'])} for c in g['characters'].values()]
        for a in g['assignments']:
            rid='role:'+a['assignment_id'];aid='agent:'+a['agent_id']
            if not any(n['id']==aid for n in nodes):nodes.append({'id':aid,'type':'Agent'})
            nodes.append({'id':rid,'type':'Role','role':a['role'],'status':a['status']})
            edges.append({'source':aid,'relation':'assignedTo','target':rid})
            if 'character_id' in a:edges.append({'source':rid,'relation':'represents','target':'character:'+str(a['character_id'])})
        return {'version':g['map_version'],'state_sequence':g.get('event_count',0),'nodes':nodes,'edges':edges}

    def rng(self, g, key): return random.Random(f'{g["roll_secret"]}:{key}')
    def threshold(self, g, c, approach, target, corpse=False):
        """[FIX-A2] Single source of truth for the dice threshold (skills, room, relation).
        roll() uses it, and player_view() shows the same numbers to the player."""
        stat = c['stats']['observe' if approach == 'explore' else 'speech' if approach in ('question', 'persuade') else 'pressure']
        threshold = 8-stat
        cid = c['character_id']
        if approach == 'explore':
            threshold -= 1 if cid == 0 else 2 if (cid == 6 and corpse) or (cid == 7 and c['room'] == 5) or (cid == 8 and c['room'] == 7) or (cid == 9 and c['room'] == 3) else 0
        elif approach == 'question':
            threshold -= int(cid == 1 or (cid == 10 and target['mode'] == 'npc') or
                             (cid == 11 and any(x['actor'] == cid and x.get('target') == target['character_id'] and x['approach'] == 'question' for x in g['activity'])))
        elif approach == 'persuade': threshold -= int(cid == 2)
        elif approach == 'interrogate': threshold -= int(cid == 4 or (cid == 10 and target['mode'] == 'npc'))
        elif approach == 'threaten': threshold -= int(cid == 5)
        if target and target['character_id'] == 3 and approach in ('interrogate', 'threaten'): threshold += 1
        mbti, on = c.get('mbti') or '', g['rules'].get('mbti_rules', False)
        relation = None
        if target and approach in ('question', 'persuade') and g['rules']['relationship_effects']:
            relation = g['relationships'][str(cid)][str(target['character_id'])]
            threshold += -1 if relation >= 70 else 1 if relation <= 30 else 0
        if on:  # [FIX-A29] MBTI corrections, summed per action and capped (a tilt, not a rule)
            mod = 0
            if 'S' in mbti: mod += MBTI_RULES['S_explore'] if approach == 'explore' else MBTI_RULES['S_question_family']
            if 'T' in mbti and approach == 'persuade': mod += MBTI_RULES['T_persuade']
            if 'T' in mbti and relation is not None and relation >= 70: mod += MBTI_RULES['T_good_lost']
            if 'F' in mbti and approach in ('interrogate', 'threaten'): mod += MBTI_RULES['F_pressure']
            if 'F' in mbti and relation is not None:  # F: doubles the base +-1
                mod += (MBTI_RULES['F_good'] + 1) if relation >= 70 else (MBTI_RULES['F_bad'] - 1) if relation <= 30 else 0
            threshold += max(-MBTI_RULES['cap'], min(MBTI_RULES['cap'], mod))
        return min(6, max(2, threshold))
    def roll(self, g, c, approach, target, key, corpse=False):
        threshold = self.threshold(g, c, approach, target, corpse)
        cid = c['character_id']
        prepared = c['prepared']
        c['prepared'] = False
        counter = g['roll_counts'].get(str(cid), 0)
        g['roll_counts'][str(cid)] = counter+1
        dice = [self.rng(g, f'roll:{cid}:{counter}:die:{i}').randint(1, 6) for i in range(3 if prepared else 2)]
        return {'dice': dice, 'threshold': threshold, 'successes': sum(x >= threshold for x in dice), 'prepared': prepared}
    def relation(self, g, owner, other, amount, reason):
        if owner == other: return
        c = g['characters'][str(owner)]
        delta = amount*((1.5 if c['mbti'][0] == 'E' else .5) if g['rules'].get('ei_relationship_multiplier', False) else 1)   # [FIX-A37]
        old = g['relationships'][str(owner)][str(other)]
        new = min(100.0, max(0.0, old+delta))
        g['relationships'][str(owner)][str(other)] = new
        aid = c['controller']
        self.e.event(g, 'relationship_changed', {'owner': owner, 'target': other, 'before': old, 'after': new, 'reason': reason},
                     'private', (aid,) if aid else ())
        for p in g['players'].values():
            if p['character_id'] == other and new <= 30: p['relationship_below_30'] = True
    def award_missions(self, g):
        for aid, p in g['players'].items():
            if p['mode'] != 'active': continue
            for m in p['missions']:
                if not m['achieved'] and mission_complete(g, aid, m):
                    m.update(achieved=True, achieved_cycle=g['cycle_id'])
                    self.e.event(g, 'mission_achieved', {'agent_id': aid, 'mission_id': m['mission_id'],
                                 'reward': REWARDS[m['tier']]}, 'private', (aid,))
    def acquire(self, g, aid, info, source, sender=None):
        p, c = g['players'][aid], self.ch(g, g['players'][aid])
        before = infer(c['information'].values())
        existing = c['information'].get(info['id'])
        if existing and existing.get('kind') in ('definite', 'indirect'):
            if sender is not None:g['disclosures'].append({'clue_id':info['id'],'sender':sender,'recipient':aid,'kind':source,'cycle':g['cycle_id'],'linked':False})
            return False
        if existing and existing.get('kind') == 'narrated' and info.get('kind') == 'narrated':
            versions = existing.get('narrations', [existing['text']])
            if info['text'] in versions:
                if sender is not None:g['disclosures'].append({'clue_id':info['id'],'sender':sender,'recipient':aid,'kind':source,'cycle':g['cycle_id'],'linked':False})
                return False
            info = copy.deepcopy(info) | {'narrations': versions+[info['text']]}
        c['information'][info['id']] = copy.deepcopy(info)
        original = next(x for x in g['clues'] if x['id'] == info['id'])
        p['acquired'].append({'id': info['id'], 'cycle': g['cycle_id'], 'room': original['source_room']})
        if info.get('kind') == 'definite' and p['first_definite'] is None:
            p['first_definite'] = {'id': info['id'], 'cycle': g['cycle_id']}
        # Counterfactual link computation stays internal; narrated text is not silently unmasked.
        counterfactual = list(c['information'].values())+[public_clue(original)]
        after = infer(counterfactual)
        linked = original['kind'] == 'indirect' and any(set(after[k])-set(before[k]) for k in before)
        g['disclosures'].append({'clue_id': info['id'], 'sender': sender, 'recipient': aid,
             'cycle': g['cycle_id'], 'kind': source, 'linked': bool(linked)})
        if original['kind'] == 'indirect':
            self.e.event(g,'indirect_link_measurement',{'clue_id':info['id'],'recipient':aid,'linked':bool(linked),
                         'prior_support':before if linked else None},'research')
        return True
    def action(self, db, g, d):
        p = self.player(g, d, read=True)
        require(p['mode'] == 'active', 'unauthorized', 'dead player cannot act')
        require(not (g['phase'] == 'interaction' and g.get('stage') == 'talk'), 'conflict', 'talk rooms are open; act after they close')   # [FIX-A42]
        r = d.get('request')
        require(isinstance(r, dict), 'invalid_input', 'action request must be an object')
        require(set(r) <= {'schema_version','game_id','agent_id','action_id','cycle_id','phase','phase_epoch','action','payload','task_id','generation'}, 'invalid_input', 'unknown action request field')
        require(r.get('schema_version', VERSION) == VERSION, 'unsupported_version', 'unknown action schema')
        require(r.get('agent_id') == p['agent_id'] and r.get('game_id') == g['id'], 'unauthorized', 'action identity mismatch')
        action_id = identifier(r.get('action_id'), 'action_id')
        fp = fingerprint(r)
        old = g['receipts'].get(action_id) or self.record(db,g,'action',action_id)
        if old:
            require(old['fingerprint'] == fp, 'conflict', 'action_id reused')
            return copy.deepcopy(old['result'])
        self.player(g, d)
        require(not r.get('task_id') and 'generation' not in r, 'invalid_input', 'competitive actions have no action task')
        require(r.get('phase') == g['phase'] and all(type(r.get(k)) is int and r[k] == g[k] for k in ('cycle_id', 'phase_epoch')),
                'conflict', 'stale phase')
        aid, cid = p['agent_id'], p['character_id']
        require(aid not in g['spent'], 'conflict', 'one action per phase')
        act, payload = r.get('action'), r.get('payload', {})
        rep = self.replacement(g, aid)  # [FIX-A17] a replacement action may be restricted
        require(act in (rep['allowed'] if rep else LEGAL[g['phase']]) and isinstance(payload, dict), 'invalid_input', 'illegal action')
        if self.gm_led(g):  # [FIX-A33] only an action the GM requested, on a target it offered
            req = g.get('requests', {}).get(aid)
            require(req is not None and req['phase_epoch'] == g['phase_epoch'] and req['status'] == 'open'
                    and act in req['options'], 'conflict', 'no open GM request for this action')
            if act == 'question':
                require(payload.get('target_id') in [x['id'] for x in req['options']['question']['targets']],
                        'invalid_input', 'question target is not in the GM request')
            req['status'] = 'answered'
        if rep: g['replacements'].pop(aid)
        if self.negotiating(g):  # [FIX-A17] held until every trade proposal is answered
            held = {k: r[k] for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
            held.update(schema_version=VERSION, ruleset=SPEC, execution_status='held', consumed=True,
                        outcome='awaiting_negotiation', acquired_information=[], roll=None)
            g['spent'][aid] = action_id
            g.setdefault('held', {})[aid] = {'request': copy.deepcopy(r), 'action_id': action_id, 'fp': fp}
            g['receipts'][action_id] = {'fingerprint': fp, 'result': held}
            p['lease_until'] = self.e.clock()+g['rules']['player_lease']
            self.maybe_release(db, g)
            return copy.deepcopy(g['receipts'][action_id]['result'])
        return self.execute(db, g, p, r, action_id, fp)
    def execute(self, db, g, p, r, action_id, fp):
        aid, cid = p['agent_id'], p['character_id']
        act, payload = r.get('action'), r.get('payload', {})
        c = self.ch(g, p)
        result = {k: r[k] for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
        result.update(schema_version=VERSION, ruleset=SPEC, execution_status='completed', consumed=True,
                      outcome='completed', acquired_information=[], roll=None, room=c['room'], actor_mode=p['mode'])
        target, approach, roll = None, None, None
        if act == 'move':
            require(set(payload) == {'room'}, 'invalid_input', 'move requires room')
            c['room'] = integer(payload['room'], 'room', 0, 7)
            p['visited'] = sorted(set(p['visited']) | {c['room']})
            result['room'] = c['room']
        elif act == 'prepare':
            require(not payload, 'invalid_input', 'prepare has no payload')
            c['prepared'] = True
        elif act == 'explore':
            require(set(payload) <= {'target_id'}, 'invalid_input', 'explore takes optional corpse target_id')
            target_id = payload.get('target_id')
            if target_id is not None:
                integer(target_id, 'corpse target', 0, 11)
                target = g['characters'][str(target_id)]
                require(target['mode'] == 'corpse' and target['room'] == c['room'], 'invalid_input', 'corpse must be in the same room')
            approach = 'explore'
            roll = self.roll(g, c, approach, target, action_id, corpse=target is not None)
            known = c['information']
            first_come = g['rules'].get('room_clues', 'shared') == 'first_come'  # [FIX-A9]
            taken = g.setdefault('room_taken', [])
            def new_to_me(x):
                return x['id'] not in known or known[x['id']].get('kind') in ('possession', 'narrated')
            boosted = g['rules'].get('clue_flow') == 'boosted'
            def obtainable(x):  # boosted: an untaken room clue can be one I already hold (a wasted find)
                return (boosted or new_to_me(x)) and not (first_come and x['id'] in taken)
            if first_come and target is None:
                # [FIX-A14] Room searches are settled together at the end of the interaction phase,
                # so who takes a contested clue no longer depends on whose request arrived first.
                result.update(execution_status='pending', outcome='awaiting_allocation')
                g.setdefault('pending_explores', []).append({
                    'action_id': action_id, 'agent_id': aid, 'room': c['room'], 'successes': roll['successes'],
                    'threshold': roll['threshold'], 'dice': len(roll['dice']),
                    'bonus': self.search_bonus(g, c, target)})
                source = []
            else:
                source = list(target['information'].values()) if target else [public_clue(x) for x in g['clues'] if x['source_room'] == c['room']]
            options = [x for x in source if (new_to_me(x) if target else obtainable(x))]
            self.rng(g, action_id+':loot').shuffle(options)
            count = 2 if roll['successes'] == 3 else int(roll['successes'] > 0)
            if count: count += self.search_bonus(g, c, target)  # [FIX-A19]
            result['acquired_information'] = options[:count]
            found = 0
            for info in result['acquired_information']:
                found += bool(self.acquire(g, aid, info, 'explore'))
                if first_come and not target and info['id'] not in taken: taken.append(info['id'])
            if found >= 2: p['multi_finds'] = p.get('multi_finds', 0) + 1  # [FIX-A21] M55
            if result['execution_status'] != 'pending':
                if not target:  # [FIX-A9] free count of what this room still holds for me
                    result['room_remaining'] = sum(obtainable(x) for x in g['clues'] if x['source_room'] == c['room'])
                result.update(outcome='acquired' if result['acquired_information'] else 'no_information',
                              cost_incurred=roll['successes'] == 1)
        elif act == 'question':
            require(set(payload) <= {'target_id', 'approach', 'hypothesis', 'message'}, 'invalid_input', 'question requires hypothesis/approach/target_id (+ optional message)')
            asker_message = self.talk(payload.get('message'))  # [FIX-A27]
            target_id = integer(payload.get('target_id'), 'target_id', 0, 11)
            target = g['characters'][str(target_id)]
            require(target_id != cid and target['room'] == c['room'] and target['mode'] != 'corpse', 'invalid_input', 'target must be a living character in the same room')
            approach = payload.get('approach', 'question')
            require(approach in ('question', 'interrogate', 'threaten', 'persuade'), 'invalid_input', 'unknown question approach')
            hypothesis = payload.get('hypothesis')
            require(isinstance(hypothesis, dict) and set(hypothesis) == {'suspect', 'weapon', 'place'}, 'invalid_input', 'three-candidate hypothesis required')
            for k, size in (('suspect', 12), ('weapon', 8), ('place', 8)): integer(hypothesis[k], k, 0, size-1)
            qid = 'question:'+str(uuid.uuid4())
            blocked = target.get('blocked', {}).get(str(cid), 0) >= g['cycle_id']
            roll = self.roll(g, c, approach, target, action_id)
            if blocked: roll['successes'] = 0
            q = {'question_id': qid, 'action_id': action_id, 'asker_id': aid, 'asker_character': cid,
                 'target_id': target_id, 'target_agent': target['controller'], 'hypothesis': copy.deepcopy(hypothesis),
                 'approach': approach, 'successes': roll['successes'], 'status': 'pending',
                 'deadline': self.e.clock()+g['rules']['question_timeout'], 'response_id': None,
                 'extra_vague': self.extra_vague(g, target) if roll['successes'] > 0 else 0,  # [FIX-A19]
                 'response_fingerprint': None, 'transferred_ids': [], 'asker_message': asker_message}
            g['questions'][qid] = q
            if asker_message: self.e.event(g, 'talk', {'from': aid, 'to': target['controller'], 'context': 'question', 'question_id': qid, 'text': asker_message}, 'supervisor')
            hidden = ('I' in c.get('mbti', '') and g['rules'].get('mbti_rules', False) and  # [FIX-A29] an I is published only sometimes
                      self.rng(g, qid+':published').random() >= MBTI_RULES['I_published'])
            g['question_pairs'].append(self.pair(q) | ({'hidden': True} if hidden else {}))
            result.update(execution_status='pending', outcome='awaiting_response', question_id=qid)
            if hidden: self.e.event(g, 'question_pair', self.pair(q), 'private', (aid, target['controller']) if target['controller'] else (aid,))
            else: self.e.event(g, 'question_pair', self.pair(q))
            if approach == 'interrogate': self.relation(g, target_id, cid, -5, 'interrogation')
            if approach == 'threaten': self.relation(g, target_id, cid, -10, 'threat')
            if roll['successes'] == 0:
                if approach in ('question', 'persuade'): self.relation(g, target_id, cid, -5, 'failed_question')
                if approach == 'interrogate': target.setdefault('blocked', {})[str(cid)] = g['cycle_id']+1
                if approach == 'threaten' and c['information']:
                    info = self.rng(g, action_id+':exposure').choice(list(c['information'].values()))
                    target['information'].setdefault(info['id'], possession(info))
                    g['disclosures'].append({'clue_id': info['id'], 'sender': aid, 'recipient': target['controller'], 'kind': 'possession', 'cycle': g['cycle_id']})
            elif approach in ('question', 'persuade'):
                gain = MBTI_RULES['question_gain'].get(c.get('mbti', ' ')[0], 5) if g['rules'].get('mbti_rules', False) else 5
                self.relation(g, target_id, cid, gain, 'successful_question')  # [FIX-A29] E +8, I +3
        elif act == 'accuse':
            require(set(payload) <= {'suspect', 'weapon', 'place', 'output_tokens', 'elapsed_seconds', 'confidence'}, 'invalid_input', 'unknown deduction field')
            guess = {k: integer(payload.get(k), k, 0, size-1) for k, size in (('suspect', 12), ('weapon', 8), ('place', 8))}
            tokens = integer(payload.get('output_tokens'), 'output_tokens', 0, 10000000)
            elapsed = payload.get('elapsed_seconds', 0)
            require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, 'invalid_input', 'invalid elapsed time')
            g['deductions'][aid] = {'guess': guess, 'output_tokens': tokens, 'elapsed_seconds': elapsed, 'action_id': action_id}
            self.e.event(g, 'deduction_submission', {'agent_id': aid, 'guess': guess, 'output_tokens': tokens,
                         'elapsed_seconds': elapsed}, 'private', (aid,))
            result.update(execution_status='pending', outcome='awaiting_adjudication')
        else:
            require(not payload, 'invalid_input', 'stay/pass has no payload')
        result['roll'] = roll
        g['spent'][aid] = action_id
        p['lease_until'] = self.e.clock()+g['rules']['player_lease']
        g['receipts'][action_id] = {'fingerprint': fp, 'result': result}
        if act != 'accuse':
            g['activity'].append({'actor': cid, 'agent_id': aid, 'kind': act, 'room': c['room'],
                'target': target['character_id'] if target else None, 'target_mode': target['mode'] if target else None,
                'approach': approach, 'successes': roll['successes'] if roll else 0, 'prepared': roll['prepared'] if roll else False,
                'cycle': g['cycle_id'], 'alone_players': not any(x['controller'] and x['mode'] == 'active' and x['character_id'] != cid and x['room'] == c['room'] for x in g['characters'].values())})
        if act == 'question':
            q = g['questions'][result['question_id']]
            if q['successes'] == 0 or target['mode'] == 'npc' or not target['information']:
                # [FIX-A15] record why the engine answered (diagnostics only)
                q['mandatory_reason'] = 'zero_successes' if q['successes'] == 0 else 'npc' if target['mode'] == 'npc' else 'no_information'
                self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
        elif act != 'accuse' and result['execution_status'] != 'pending': self.complete_action(db, g, result)
        self.award_missions(g)
        return copy.deepcopy(g['receipts'][action_id]['result'])
    def complete_action(self, db, g, result):
        aid = result['agent_id']
        result['held_information_ids']=list(self.ch(g,g['players'][aid])['information'])
        e = self.e.event(g, 'action_completed', {'result': copy.deepcopy(result)}, 'private', (aid,))
        self.e.experience(db, g, aid, e['event_id'], 'player', result['outcome'], result['outcome'] in ('acquired', 'winner'))
    def options(self, g, q):
        c = g['characters'][str(q['target_id'])]
        full = [x for x in c['information'].values() if x.get('kind') == 'definite']
        # [FIX-A18] Ambiguous statement = pair exclusion. Anchor: a candidate one of my definite
        # clues rules out. Partner: the asker's hypothesis candidate in another category. "Anchor
        # together with partner is not the answer" is true because of the anchor alone, and the
        # asker is not told which side is wrong. Same-category pairs are always true (one answer
        # per category), so they are never offered. Replaces "my clue is not about candidate X".
        ambiguous, seen = [], set()
        for x in [x for x in c['information'].values() if x.get('kind') == 'definite']:
            for cat, candidate in q['hypothesis'].items():
                if cat == x['category']:
                    continue
                pair = {x['category']: x['candidate'], cat: candidate}
                key = tuple(sorted(pair.items()))
                if key in seen:
                    continue
                seen.add(key)
                (c1, v1), (c2, v2) = key
                ambiguous.append({'kind': 'ambiguous', 'pair': dict(key),
                                  'text': f'{c1} 후보 {v1}이면서 {c2} 후보 {v2}인 조합은 정답이 아니다.'})
        indirect = []
        for clue in c['information'].values():
            if clue.get('kind') == 'indirect':
                indirect.append({'kind': 'indirect', 'clue_id': clue['id'], 'narrative_options': narrative_options(clue)})
            elif clue.get('kind') == 'narrated':
                indirect.append({'kind':'indirect','clue_id':clue['id'],'narrative_options':clue.get('narrations',[clue['text']])})
        return {'definite': [x['id'] for x in full], 'ambiguous': ambiguous, 'indirect': indirect}
    def default_response(self, g, q):
        opts = self.options(g, q)
        c = g['characters'][str(q['target_id'])]
        probability = .5
        if c['mbti'][2] == 'T': probability += {'persuade': .2, 'question': 0, 'interrogate': -.1, 'threaten': -.2}[q['approach']]
        else:
            r = g['relationships'][str(q['target_id'])][str(q['asker_character'])]
            probability += .2 if r >= 70 else -.2 if r <= 30 else 0
        definite = opts['definite'][:1]
        vague = [dict(x) for x in opts['ambiguous']]
        for x in opts['indirect']:
            vague.append({'kind': 'indirect', 'clue_id': x['clue_id'], 'text': x['narrative_options'][0]})
        n = q['successes']
        use_full = bool(definite) and (n == 3 or (n == 2 and self.rng(g, q['question_id']+':form').random() < probability))
        statements = ([{'kind': 'definite', 'clue_id': definite[0]}] if use_full else [])
        extra = q.get('extra_vague', 0)
        need = 0 if n == 0 else (1 if n == 1 else 0 if n == 2 and use_full else 2) + extra
        statements += vague[:need]
        if n == 2 and not use_full and len(vague) < 2 and definite:
            statements = [{'kind': 'definite', 'clue_id': definite[0]}] + vague[:extra]
        return statements
    def validate_response(self, g, q, statements):
        # [FIX-A19] extra vague slots can take an answer past three statements
        require(isinstance(statements, list) and len(statements) <= 3 + q.get('extra_vague', 0) and all(isinstance(x, dict) for x in statements), 'invalid_input', 'response is a statement list')
        opts = self.options(g, q)
        known = g['characters'][str(q['target_id'])]['information']
        seen, full, vague, safe = set(), 0, 0, []
        for s in statements:
            require(set(s) <= {'kind', 'clue_id', 'pair', 'text'}, 'invalid_input', 'unknown statement field')
            key = (s.get('kind'), s.get('clue_id'), json.dumps(s.get('pair'), sort_keys=True))
            require(key not in seen, 'invalid_input', 'duplicate statement')
            seen.add(key)
            if s.get('kind') == 'definite':
                require(s.get('clue_id') in opts['definite'], 'untruthful', 'definite clue must be owned and fully known')
                full += 1
                safe.append(public_clue(known[s['clue_id']]))
            elif s.get('kind') == 'ambiguous':
                match = next((x for x in opts['ambiguous'] if isinstance(s.get('pair'), dict) and x['pair'] == {k: s['pair'][k] for k in sorted(s['pair'])}), None)
                require(match is not None and s.get('text', match['text']) == match['text'], 'untruthful', 'ambiguous claim is not entailed by owned evidence')
                vague += 1
                safe.append(copy.deepcopy(match))
            elif s.get('kind') == 'indirect':
                clue = known.get(s.get('clue_id'), {})
                allowed = narrative_options(clue) if clue.get('kind') == 'indirect' else clue.get('narrations',[clue.get('text')]) if clue.get('kind') == 'narrated' else []
                require(s.get('text') in allowed and isinstance(s.get('text'),str), 'untruthful', 'narration is not in the truth-preserving grammar')
                vague += 1
                safe.append({'kind': 'narrated', 'id': clue['id'], 'text': s['text']})
            else: raise ContractError('invalid_input', 'unknown statement kind')
        n = q['successes']
        # If evidence makes a required form impossible, disclose all available allowed slots.
        # [FIX-A19] extra vague slots (boosted clue flow) on top of the success tier, capped by what exists
        extra, avail = q.get('extra_vague', 0), len(opts['ambiguous'])+len(opts['indirect'])
        max_vague = min(2 + extra, avail)
        if n == 0: valid = full == vague == 0
        elif n == 1: valid = (full == 0 and vague == min(1 + extra, avail)) or (avail == 0 and full == 1 and vague == 0)
        elif n == 2:
            valid = (full == 1 and vague == min(extra, avail) and bool(opts['definite'])) or (full == 0 and vague == max_vague and (max_vague > 0 or not opts['definite']))
        else: valid = full == int(bool(opts['definite'])) and vague == max_vague
        require(valid, 'disclosure_required', 'response does not meet success-tier disclosure requirement')
        return safe
    def complete_question(self, db, g, q, statements, mandatory=False, message=None):
        safe = self.validate_response(g, q, statements)
        target = g['characters'][str(q['target_id'])]
        npc = target['mode'] == 'npc'
        result = g['receipts'][q['action_id']]['result']
        acquired, descriptions = [], []
        for info in safe:
            if info['kind'] == 'ambiguous':
                descriptions.append(info)
                n = len(g['players'][q['asker_id']]['ambiguous_statements'])  # [FIX-A18] an ID, for belief support sets
                g['players'][q['asker_id']]['ambiguous_statements'].append(copy.deepcopy(info)|{'question_id':q['question_id'], 'id': f"pair:{q['question_id']}:{n}"})
                continue
            # NPC statements are full original indirect facts, player narration stays redacted.
            asker_n = ('N' in g['characters'][str(q['asker_character'])].get('mbti', '') and g['rules'].get('mbti_rules', False) and
                       self.rng(g, q['question_id']+':original:'+str(info.get('id'))).random() < MBTI_RULES['N_original'])
            if info['kind'] == 'narrated' and (npc or asker_n) and info['id'] in target['information']:
                info = public_clue(target['information'][info['id']])  # [FIX-A29] N reads the original
            self.acquire(g, q['asker_id'], info, 'response_definite' if info['kind'] == 'definite' else 'response_indirect', q['target_agent'])
            acquired.append(info)
            q['transferred_ids'].append(info['id'])
        q['status'] = 'completed'
        q['mandatory_fallback'] = mandatory
        g['transfer_count'] += int(bool(acquired))
        g['recent_questions'] = (g['recent_questions']+[copy.deepcopy(q)])[-12:]
        result.update(execution_status='completed', outcome='acquired' if acquired or descriptions else 'no_information',
                      acquired_information=acquired, statements=descriptions, answer_message=message)  # [FIX-A27]
        q['answer_message'] = message
        self.complete_action(db, g, result)
        self.e.event(g, 'question_supervision', self.pair(q) | {'successes': q['successes'], 'approach': q['approach'], 'transferred_ids': q['transferred_ids']}, 'supervisor')
        self.award_missions(g)
    def respond(self, db, g, d):
        p = self.player(g, d)
        q = g['questions'].get(d.get('question_id')) or self.record(db,g,'question',d.get('question_id'))
        require(q and q['target_agent'] == p['agent_id'], 'unauthorized', 'only the addressed player may respond')
        rid = identifier(d.get('response_id'), 'response_id')
        fp = fingerprint(d)
        if q['response_id'] is not None:
            require(q['response_id'] == rid and q['response_fingerprint'] == fp, 'conflict', 'response already committed')
            return {'question_id': q['question_id'], 'committed': True}
        require(q['status'] == 'pending' and self.e.clock() < q['deadline'], 'expired', 'response deadline passed')
        message = self.talk(d.get('message'))  # [FIX-A27]
        self.complete_question(db, g, q, d.get('statements'), message=message)
        if message: self.e.event(g, 'talk', {'from': p['agent_id'], 'to': q['asker_id'], 'context': 'answer', 'question_id': q['question_id'], 'text': message}, 'supervisor')
        q.update(response_id=rid, response_fingerprint=fp)
        p['lease_until'] = self.e.clock()+g['rules']['player_lease']
        return {'question_id': q['question_id'], 'committed': True}

    def adjudicate(self, db, g):
        correct = [(aid, x) for aid, x in g['deductions'].items() if x['guess'] == g['truth']]
        tokens = sorted(x['output_tokens'] for _, x in correct)
        verdicts = []
        for aid, choice in g['deductions'].items():
            p, won = g['players'][aid], choice['guess'] == g['truth']
            result = g['receipts'][choice['action_id']]['result']
            if won:
                rank = 1+sum(n < choice['output_tokens'] for n in tokens)
                p['rank'] = rank
                verdicts.append({'agent_id': aid, 'character_id': p['character_id'], 'winner': True,
                                 'rank': rank, 'guess': choice['guess']})
                result.update(execution_status='completed', outcome='winner', rank=rank)
            else:
                p['mode'] = 'corpse'
                for a in g['assignments']:
                    if a['assignment_id']==p['assignment_id']:a['status']='corpse'
                c = self.ch(g, p)
                c.update(mode='corpse', controller=None, goal=None)
                verdicts.append({'agent_id': aid, 'character_id': p['character_id'], 'winner': False, 'mode': 'corpse'})
                result.update(execution_status='completed', outcome='dead', actor_mode='corpse')
                self.e.event(g, 'actor_died', {'character_id': p['character_id']})
            self.complete_action(db, g, result)
        if verdicts:
            ref = 'deduction:'+str(g['phase_epoch'])
            g['announcements'][ref] = {'kind': 'deduction_result', 'data': {'ref': ref, 'kind': 'deduction_result',
                'cycle_id': g['cycle_id'], 'verdicts': verdicts}, 'published': False}
            self.e.event(g, 'deduction_ready', {'ref': ref, 'verdicts': verdicts}, 'supervisor')
        g['deductions'] = {}
        if correct: g['resolution'] = 'winner'
        elif not any(p['mode'] == 'active' for p in g['players'].values()): g['resolution'] = 'all_dead'

    def advance(self, db, g, d):
        require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'], 'conflict', 'stale phase epoch')
        active = [aid for aid, p in g['players'].items() if p['mode'] == 'active']
        self.maybe_release(db, g)  # [FIX-A17]; under GM orchestration a no-op (RELEASE first)
        require(active and all(aid in g['spent'] for aid in active) and
                not any(q['status'] == 'pending' for q in g['questions'].values()) and
                not any(t['status'] == 'proposed' for t in g['trades'].values()) and not self.negotiating(g),
                'pending', 'all actions, replies and trades must finish')
        g['replacements'] = {}
        if g['phase'] == 'deduction':
            self.adjudicate(db, g)
            alive = [str(p['character_id']) for p in g['players'].values() if p['mode'] == 'active']
            g['cycle_history'].append({'cycle': g['cycle_id'], 'rooms': {i: c['room'] for i, c in g['characters'].items()}, 'alive': alive})
            self.award_missions(g)
            if g['resolution'] is None and g['cycle_id'] >= g['rules']['deadline_cycles']:
                g['resolution'] = 'deadline'
            if g['resolution'] is not None:
                self.e.event(g, 'resolution_ready', {'resolution': g['resolution']}, 'supervisor')
                return self.visible(g)
            g['cycle_id'] += 1
            for c in g['characters'].values():
                if c['mode'] == 'corpse' and c['information']:
                    cid = self.rng(g, f'decay:{c["character_id"]}:{g["cycle_id"]}').choice(sorted(c['information']))
                    del c['information'][cid]
            remaining = g['rules']['deadline_cycles']-g['cycle_id']+1
            if remaining in (30, 10):
                ref = 'deadline:'+str(remaining)
                g['announcements'][ref] = {'kind': 'deadline_warning', 'data': {'ref': ref, 'kind': 'deadline_warning', 'remaining_cycles': remaining}, 'published': False}
            g['phase'] = 'movement'
        elif g['phase'] == 'movement':
            # The simultaneous movement boundary also moves NPCs, including staying.
            for c in g['characters'].values():
                if c['mode'] == 'npc': c['room'] = self.rng(g, f'npc-move:{g["cycle_id"]}:{c["character_id"]}').randrange(8)
            g['phase'] = 'interaction'
            g['stage'] = 'negotiation' if g['rules'].get('interaction_order') == 'simultaneous' else None  # [FIX-A17]
            self.open_talk(g)   # [FIX-A42] rooms with 2+ intending players talk first
            # [FIX-A13] Placement is public once the movement turn ends: every character's room,
            # recorded per cycle, so players can reason about who could have searched where.
            placement = {'cycle': g['cycle_id'], 'rooms': {cid: c['room'] for cid, c in g['characters'].items()}}
            g.setdefault('placements', []).append(placement)
            self.e.event(g, 'placement_revealed', copy.deepcopy(placement))
        else:
            self.allocate_explores(db, g)  # [FIX-A14]
            g['phase'] = 'deduction'
        g['phase_epoch'] += 1
        g['spent'] = {}
        self.e.event(g, 'phase_advanced', {'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'cycle_id': g['cycle_id']})
        return self.visible(g)
    def finish(self, db, g, reason=None):
        require(not g['ended'], 'conflict', 'already ended')
        reason = reason or g['resolution']
        require(reason in ('winner', 'all_dead', 'deadline', 'time_out', 'no_players', 'operator_stopped'), 'conflict', 'no official terminal resolution')
        for t in g['trades'].values():  # [FIX-A17] a proposal still open when the game ends
            if t['status'] == 'proposed' and g['rules'].get('interaction_order') == 'simultaneous':
                t.update(status='cancelled', cancel_reason='game_ended')
        self.settle_trades(db, g, force=True)
        feedback = {}
        pool = self.pool(db, g['pool_id'])
        # [FIX-A31] end-of-game standings: how far each player narrowed the answer (own clues + pair exclusions)
        def narrowed(pp):
            ex = infer(list(self.ch(g, pp)['information'].values()) + list(pp.get('ambiguous_statements', [])))
            return (12 - len(ex['suspect'])) * (8 - len(ex['weapon'])) * (8 - len(ex['place']))
        standings = {aid: narrowed(pp) for aid, pp in g['players'].items()}
        board_rows = []   # [FIX-A36]
        for aid, p in g['players'].items():
            rank = p.get('rank')
            unsolved_end = reason in ('deadline', 'time_out')
            unsolved = g['rules'].get('unsolved_score', 0) if unsolved_end else 0
            pc = self.ch(g, p)
            base = self.win_scores(g, pc)[min(rank, 4)-1] if rank else self.wrong_score(g, pc) if p['mode'] == 'corpse' else unsolved
            # [FIX-A4] survivors of an unsolved game forfeit their sub-mission bonus
            voided = unsolved_end and p['mode'] != 'corpse' and g['rules'].get('void_missions_if_unsolved', False)
            # [FIX-A39] winner takes all: surviving non-winners of a solved game forfeit theirs too
            voided = voided or (reason == 'winner' and not rank and p['mode'] != 'corpse'
                                and g['rules'].get('void_missions_if_someone_wins', False))
            score = base+(0 if voided else sum(REWARDS[m['tier']] for m in p['missions'] if m['achieved']))
            a = self.e.load(db, 'agents', aid)
            banked = sum(REWARDS[m['tier']] for m in p['missions'] if m['achieved'])
            rivals = [v for other, v in standings.items() if other != aid]
            record = {'game_id': g['id'], 'character_id': p['character_id'], 'job_id': p['character_id'],
                      # [FIX-A31] facts for the agent's own review of an unsolved game
                      'final_combinations': standings[aid],
                      'closest_rival_combinations': min(rivals) if rivals else None,
                      'score_if_someone_had_won': ((0 if g['rules'].get('void_missions_if_someone_wins', False) else banked)
                                                   if unsolved_end and p['mode'] != 'corpse' else None),
                      'missions_voided_points': (banked if voided else 0),
                      'final_score': score, 'participation_index': len(a['history'])+1,
                      'sub_missions': copy.deepcopy(p['missions']), 'rank': rank,
                      'survived': p['mode'] != 'corpse', 'outcome': 'loss' if reason == 'time_out' else 'win' if rank else 'loss', 'mbti': a['fixed_mbti'],
                      'missions_voided': bool(voided), 'resolution': reason}
            history_record = copy.deepcopy(record)
            history_record['sub_missions'] = [{k: m[k] for k in ('mission_id', 'tier', 'achieved', 'achieved_cycle')}
                                              for m in p['missions']]
            a['history'].append(history_record)
            if reason != 'operator_stopped':   # [FIX-A40] an aborted game is not played; the rollback undoes its history
                a['cumulative_score'] = a.get('cumulative_score', 0) + score
            # [FIX-A28] agent-to-agent relationship (trust) carries over: add this game's change
            # (game relationships start at 50) toward every other agent's character
            trust = a.setdefault('agent_relationships', {})
            for other, op in g['players'].items():
                if other == aid or not g['rules'].get('engine_agent_relationships', False): continue   # [FIX-A34]
                change = g['relationships'][str(p['character_id'])][str(op['character_id'])] - 50.0
                trust[other] = min(100.0, max(0.0, trust.get(other, 50.0) + change))
            self.e.put(db, 'agents', aid, a)
            feedback[aid] = {k: v for k, v in record.items() if k != 'mbti'}
            for m in feedback[aid]['sub_missions']: m['targets'].pop('target_ei', None)
            board_rows.append({'aid': aid, 'score': score, 'base': base,
                               'outcome': 'correct_accusation' if rank else 'wrong_accusation_dead' if p['mode'] == 'corpse'
                                          else 'unsolved_survivor' if unsolved_end else 'survivor_someone_else_won',
                               'missions': 0 if voided else banked, 'missions_voided': bool(voided),
                               'missions_achieved': sum(1 for m in p['missions'] if m['achieved'])})
        # [FIX-A36] one private result notice per player, written from the official scores; the GM publishes it
        board_rows.sort(key=lambda r: -r['score'])
        for i, r in enumerate(board_rows):
            r['rank'] = board_rows[i - 1]['rank'] if i and r['score'] == board_rows[i - 1]['score'] else i + 1
        for r in board_rows:   # shared scores are a shared rank: 'tied' players hold it (user, 11-09)
            r['tied'] = sum(1 for x in board_rows if x['score'] == r['score'])
        for r in board_rows:
            ref = 'scoreboard:' + r['aid']
            g['announcements'][ref] = {'kind': 'scoreboard', 'private_to': r['aid'], 'published': False, 'data': {
                'ref': ref, 'kind': 'scoreboard', 'players': len(board_rows), 'my_rank': r['rank'], 'my_tied': r['tied'],
                'my_score': r['score'],
                'note': 'from the GM to you only; other rows are anonymous (outcome and score composition only)',
                'rows': [{k: v for k, v in x.items() if k != 'aid'} | {'me': x['aid'] == r['aid']} for x in board_rows]}}
        g.update(ended=True, status='ended', resolution=reason, finished_at=self.e.clock(), final_feedback=feedback)
        # [FIX-A40] each first-ranked player may now share part of its score of this game
        g['sharing'] = {r['aid']: {'budget': r['score'], 'status': 'open'} for r in board_rows
                        if reason == 'winner' and g['rules'].get('post_game_sharing', False)
                        and g['players'][r['aid']].get('rank') == 1 and r['score'] > 0}
        for a in g['assignments']:
            if a['status']=='active':a['status']='completed'
        for aid in pool['agent_ids']:
            a = self.e.load(db, 'agents', aid)
            if a['busy_game_id'] == g['id']:
                a['busy_game_id'] = None
                if a['pool_status'] == 'active': a['pool_status'] = 'available'
                self.e.put(db, 'agents', aid, a)
        self.e.event(g, 'game_finished', {'reason': reason, 'feedback': feedback})
        self.publish_standings(db, g)   # [FIX-A40] at once when nobody may share
    # ---- [FIX-A40] cumulative score and post-game sharing ----------------
    def standings(self, db, pid):
        """Every pool player's cumulative score and completed games, highest first (public)."""
        rows = []
        for aid in self.pool(db, pid)['agent_ids']:
            a = self.e.load(db, 'agents', aid)
            if a.get('pool_role') != 'player': continue
            games = sum(1 for h in a.get('history', []) if h.get('resolution') != 'operator_stopped')
            rows.append({'agent_id': aid, 'cumulative_score': a.get('cumulative_score', 0), 'games': games})
        return sorted(rows, key=lambda r: (-r['cumulative_score'], r['agent_id']))
    def publish_standings(self, db, g):
        if any(s['status'] == 'open' for s in (g.get('sharing') or {}).values()) or g.get('standings_published'):
            return
        g['standings_published'] = self.standings(db, g['pool_id'])
        self.e.event(g, 'standings_published', {'standings': copy.deepcopy(g['standings_published'])})
    def share(self, db, member, d):
        """A first-ranked player of a finished game gives part of its own score of that game, once."""
        g = self.e.load(db, 'games', identifier(d.get('game_id'), 'game_id'), 'id')
        require(g.get('mode') == 'competitive' and g['ended'], 'not_running', 'sharing follows a finished game')
        aid = member['agent_id']
        s = (g.get('sharing') or {}).get(aid)
        require(s is not None, 'unauthorized', 'only a first-ranked player of this game may share')
        require(s['status'] == 'open', 'conflict', 'sharing for this game is closed')
        shares = d.get('shares', [])
        require(isinstance(shares, list) and len(shares) <= 15, 'invalid_input', 'shares: [{agent_id, points}]')
        pool_players = {r['agent_id'] for r in self.standings(db, g['pool_id'])}
        clean, total = [], 0
        for x in shares:
            require(isinstance(x, dict) and set(x) == {'agent_id', 'points'}, 'invalid_input', 'share: {agent_id, points}')
            rid, pts = x['agent_id'], x['points']
            require(rid in pool_players and rid != aid, 'invalid_input', 'a recipient is another pool player')
            require(type(pts) is int and pts > 0, 'invalid_input', 'points are positive integers')
            require(all(c['agent_id'] != rid for c in clean), 'invalid_input', 'one entry per recipient')
            clean.append({'agent_id': rid, 'points': pts})
            total += pts
        require(total <= s['budget'], 'invalid_input', f'at most your own score of this game ({s["budget"]})')
        giver = self.e.load(db, 'agents', aid)
        giver['cumulative_score'] = giver.get('cumulative_score', 0) - total
        giver.setdefault('shares_given', []).extend({'game_id': g['id'], 'to': c['agent_id'], 'points': c['points']} for c in clean)
        self.e.put(db, 'agents', aid, giver)
        for c in clean:
            r = self.e.load(db, 'agents', c['agent_id'])
            r['cumulative_score'] = r.get('cumulative_score', 0) + c['points']
            r.setdefault('shares_received', []).append({'game_id': g['id'], 'from': aid, 'points': c['points']})
            self.e.put(db, 'agents', c['agent_id'], r)
        s.update(status='done', shares=clean, total=total)
        self.e.event(g, 'score_shared', {'agent_id': aid, 'shares': clean, 'total': total}, 'supervisor')
        self.publish_standings(db, g)
        self.persist(db, g)
        return {'shared': total, 'shares': clean}
    def close_sharing(self, db, d):
        """Operator: whoever did not share by now gave nothing; then the standings are published."""
        self.e.operator(d)
        g = self.e.load(db, 'games', identifier(d.get('game_id'), 'game_id'), 'id')
        require(g.get('mode') == 'competitive' and g['ended'], 'not_running', 'sharing follows a finished game')
        for s in (g.get('sharing') or {}).values():
            if s['status'] == 'open': s.update(status='none', shares=[], total=0)
        self.publish_standings(db, g)
        self.persist(db, g)
        return {'sharing': copy.deepcopy(g.get('sharing') or {}), 'standings': copy.deepcopy(g.get('standings_published'))}
    def my_standing_notes(self, db, member):
        """What only I know: the shares I received and gave (the giver is the first-ranked player)."""
        return {'shares_received': copy.deepcopy(member.get('shares_received', [])),
                'shares_given': copy.deepcopy(member.get('shares_given', []))}
    def fail_player(self, db, g, aid, reason):
        p = g['players'][aid]
        if p['mode'] != 'active': return
        p.update(mode='npc', token='')
        for a in g['assignments']:
            if a['assignment_id']==p['assignment_id']:a['status']='npc'
        c = self.ch(g, p)
        c.update(mode='npc', controller=None, goal=None)
        g.get('held', {}).pop(aid, None)
        nf = g['rules'].get('interaction_order') == 'simultaneous'
        for t in g['trades'].values():  # [FIX-A17] a failed process is mechanical: cancel, no reputation
            if nf and t['status'] == 'proposed' and aid in (t['proposer'], t['recipient']):
                self.cancel_trade(g, t, 'player_failed')
        for q in g['questions'].values():
            if q['status'] == 'pending' and q['target_agent'] == aid and nf:
                q['mandatory_reason'] = 'player_failed'
                self.fail_question(db, g, q)
            elif q['status'] == 'pending' and q['target_agent'] == aid:
                self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
            elif q['status'] == 'pending' and q['asker_id'] == aid:
                q['status'] = 'cancelled'
                g['receipts'][q['action_id']]['result'].update(execution_status='completed', outcome='no_information')
        a = self.e.load(db, 'agents', aid)
        a['pool_status'] = 'fault'
        self.e.put(db, 'agents', aid, a)
        g['technical_failure'] = g['technical_failure'] or reason != 'promoted_to_gm'
        g['map_version'] += 1
        self.e.event(g, 'player_fault', {'agent_id': aid, 'character_id': p['character_id'], 'reason': reason})
        if g['resolution'] is None and not any(p['mode'] == 'active' for p in g['players'].values()): g['resolution'] = 'no_players'
    def failover(self, db, g, reason, chosen=None):
        old = g['controller']
        pool = self.pool(db, g['pool_id'])
        if old:
            a = self.e.load(db, 'agents', old['controller_id'])
            a['pool_status'] = 'fault'
            a['pool_role'] = 'retired_gm'
            self.e.put(db, 'agents', a['agent_id'], a)
            for assignment in g['assignments']:
                if assignment['role'] == 'gm' and assignment['term'] == old['term']: assignment['status'] = 'retired'
            self.e.event(g, 'gm_retired', {'controller_id': old['controller_id'], 'term': old['term'], 'reason': reason})
        g['controller'] = None
        g['failovers'] += 1
        g['technical_failure'] = True
        active = sorted(aid for aid, p in g['players'].items() if p['mode'] == 'active' and self.e.clock() < p['lease_until'])
        reserve = [a['agent_id'] for a in self.cards(db, pool) if a['pool_role'] == 'player' and a['pool_status'] == 'available']
        options = active or sorted(reserve)
        if chosen is not None:
            require(chosen in active or chosen in reserve, 'invalid_input', 'ineligible successor')
            options = [chosen]
        if not options:
            g['status'] = 'waiting_for_human'
            self.e.event(g, 'human_intervention_required', {'choices': ['FINISH'], 'reason': 'no_eligible_GM'})
            return
        aid = options[0]
        if aid in active: self.fail_player(db, g, aid, 'promoted_to_gm')
        else: self.e.reserve(db, aid, g['id'])
        a = self.e.load(db, 'agents', aid)
        a.update(pool_status='active', pool_role='gm')
        self.e.put(db, 'agents', aid, a)
        pool['current_gm_id'] = aid
        self.save_pool(db, pool)
        self.e.controller(g, aid)
        g['status'] = 'running' if g['started'] else 'awaiting_setup'
        self.e.event(g, 'gm_succeeded', {'controller_id': aid, 'term': g['term'], 'context_source': 'engine-only'})
    def gm_command(self, db, g, d):
        c = self.e.gm(g, d, allow_ended=True)
        cid = identifier(d.get('command_id'), 'command_id')
        fp = fingerprint(d)
        old = g['commands'].get(cid) or self.record(db,g,'command',cid)
        if old:
            require(old['fingerprint'] == fp, 'conflict', 'command_id reused')
            return copy.deepcopy(old['result'])
        op = d.get('operation')
        require(not g['ended'] or op == 'REPORT' or (op == 'ANNOUNCE' and d.get('kind') == 'scoreboard'),   # [FIX-A36]
                'not_running', 'game ended')
        if op == 'PLAN': result = self.plan(db, g, d)
        elif op == 'ADVANCE':
            require(g['resolution'] is None, 'not_running', 'terminal resolution already ready')
            result = self.advance(db, g, d)
        elif op == 'FINISH':
            self.finish(db, g)
            result = self.visible(g)
        elif op == 'REQUEST':  # [FIX-A33] the GM asks players to act: legal actions and targets only
            require(self.gm_led(g), 'conflict', 'the engine drives this game (orchestration=engine)')
            require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'], 'conflict', 'stale phase epoch')
            ids = d.get('agent_ids')
            require(isinstance(ids, list) and ids and all(x in g['players'] for x in ids), 'invalid_input', 'agent_ids: players of this game')
            issued = {}
            for aid in ids:
                if aid not in self.needs_request(g): continue
                req = self.player_view(g, g['players'][aid], raw=True).get('action_request')
                if not req: continue
                g['request_count'] = g.get('request_count', 0) + 1
                req = dict(req, request_id=f"req:{g['phase_epoch']}:{g['request_count']}", status='open',
                           replacement=bool(self.replacement(g, aid)))
                g.setdefault('requests', {})[aid] = req
                self.e.event(g, 'action_requested', {'request_id': req['request_id'], 'phase': req['phase'],
                             'options': sorted(req['options'])}, 'private', (aid,))
                issued[aid] = req['request_id']
            result = {'requested': issued}
        elif op == 'TALK_NEXT':  # [FIX-A42] the GM moves the talk rooms to the next round, or closes them
            require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'], 'conflict', 'stale phase epoch')
            require(g.get('stage') == 'talk', 'conflict', 'no talk stage open')
            facts = self.talk_facts(g)
            force = d.get('force', False)
            require(type(force) is bool, 'invalid_input', 'force is boolean')
            require(facts['ready'] or (force and facts['overdue']), 'pending', 'members of an open room have not spoken this round')
            result = self.talk_next(g)
        elif op == 'RELEASE':  # [FIX-A33] the GM closes the negotiation stage; held actions run
            require(self.gm_led(g), 'conflict', 'the engine drives this game (orchestration=engine)')
            require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'], 'conflict', 'stale phase epoch')
            require(self.release_ready(g), 'pending', 'actions or trade proposals are still open')
            self.release(db, g)
            result = {'stage': g['stage']}
        elif op == 'EXPIRE':  # [FIX-A33] the GM declares a mechanical timeout; the engine applies the rule
            require(self.gm_led(g), 'conflict', 'the engine drives this game (orchestration=engine)')
            kind, ref = d.get('kind'), d.get('ref')
            now = self.e.clock()
            if kind == 'question':
                q = g['questions'].get(ref)
                require(q is not None and q['status'] == 'pending' and now >= q['deadline'], 'conflict', 'question is not overdue')
                q['mandatory_reason'] = 'timeout'
                if g['rules'].get('interaction_order') == 'simultaneous': self.fail_question(db, g, q)
                else: self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
            elif kind == 'trade':
                t = g['trades'].get(ref)
                require(t is not None and t['status'] == 'proposed' and now >= t['deadline'], 'conflict', 'trade is not overdue')
                self.cancel_trade(g, t, 'timeout')
            else:
                raise ContractError('invalid_input', 'EXPIRE kind is question or trade')
            result = {'expired': ref, 'kind': kind}
        elif op == 'ANNOUNCE':
            ref = d.get('ref')
            a = g['announcements'].get(ref)
            require(a is not None and a['kind'] == d.get('kind'), 'invalid_input', 'reference official announcement data')
            require('data' not in d and 'text' not in d, 'invalid_input', 'GM cannot rewrite verdicts')
            if not a['published']:
                a['published'] = True
                if a.get('private_to'):   # [FIX-A36] the GM tells this player only
                    self.e.event(g, 'announcement', a['data'], 'private', (a['private_to'],))
                    if g.get('final_feedback') and a['private_to'] in g['final_feedback']:
                        g['final_feedback'][a['private_to']]['scoreboard'] = copy.deepcopy(a['data'])
                else:
                    self.e.event(g, 'announcement', a['data'])
            result = copy.deepcopy(a['data'])
        elif op == 'REPORT':
            require(g['ended'] and g['gm_report'] is None, 'conflict', 'one report after finalization')
            m = d.get('metrics')
            require(isinstance(m, dict) and set(m) in ({'completion', 'balance', 'exploration', 'record_completeness'},
                    {'completion', 'balance', 'exploration', 'record'}), 'invalid_input', 'C4 metrics required')
            m = {('record_completeness' if k == 'record' else k): v for k, v in m.items()}
            require(g['resolution'] in ('winner','all_dead','deadline','time_out') or m['completion'] == 0,
                    'invalid_input', 'non-legitimate ending cannot claim progress completion')
            for key, value in m.items():
                require((key == 'balance' and value is None) or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1), 'invalid_input', 'metric outside 0..1')
            evidence = d.get('evidence')
            require(isinstance(evidence, list) and bool(evidence) and all(x in {e['event_id'] for e in self.all_events(db,g)} for x in evidence), 'invalid_input', 'official evidence event IDs required')
            weights = {'completion': .35, 'balance': .25, 'exploration': .2, 'record_completeness': .2}
            denominator = sum(weights[k] for k in m if m[k] is not None)
            score = sum(weights[k]*v for k, v in m.items() if v is not None)/denominator
            g['gm_report'] = {'metrics': copy.deepcopy(m), 'evidence': evidence, 'score': score,
                'technical_failure': g['technical_failure'], 'success': score >= .7 and not g['technical_failure']}
            rel = d.get('reported_relationships')   # [FIX-A35] the GM's summary of what agents reported (record only)
            if rel is not None:
                require(isinstance(rel, dict) and len(json.dumps(rel)) <= 200000, 'invalid_input',
                        'reported_relationships must be a JSON object under 200 KB')
                g['gm_report']['reported_relationships'] = copy.deepcopy(rel)
            member=self.e.load(db,'agents',c['controller_id'])
            member.setdefault('gm_reports',[]).append({'game_id':g['id'],'score':score,'metrics':copy.deepcopy(m),
                'success':g['gm_report']['success'],'technical_failure':g['technical_failure']})
            self.e.put(db,'agents',c['controller_id'],member)
            event = self.e.event(g, 'gm_reported', {'score': score, 'success': g['gm_report']['success']}, 'supervisor')
            self.e.experience(db, g, c['controller_id'], event['event_id'], 'gm', 'performance_report', g['gm_report']['success'])
            result = g['gm_report']
        else: raise ContractError('invalid_input', 'unknown competitive GM operation')
        if not g['ended']: c['lease_until'] = self.e.clock()+g['rules']['gm_lease']
        g['commands'][cid] = {'fingerprint': fp, 'result': copy.deepcopy(result)}
        return result
    def operator(self, db, g, d):
        self.e.operator(d)
        require(not g['ended'], 'conflict', 'already ended')
        op = d.get('operation')
        if op == 'PAUSE':
            require(g['status'] != 'paused', 'conflict', 'already paused')
            g['paused_at'] = self.e.clock()
            g['status'] = 'paused'
        elif op == 'RESUME':
            require(g['status'] == 'paused' and g['controller'], 'conflict', 'no paused controller')
            delta = self.e.clock()-g.pop('paused_at')
            g['controller']['lease_until'] = self.e.clock()+g['rules']['gm_lease']
            for p in g['players'].values(): p['lease_until'] = self.e.clock()+g['rules']['player_lease']
            for x in list(g['questions'].values())+list(g['trades'].values()): x['deadline'] += delta
            if g.get('talk') and not g['talk'].get('closed'): g['talk']['round_started'] += delta   # [FIX-A42]
            g['status'] = 'running' if g['started'] else 'awaiting_setup'
        elif op == 'FAIL_PLAYER':
            require(d.get('agent_id') in g['players'], 'not_found', 'player not found')
            self.fail_player(db, g, d['agent_id'], 'injected_failure')
        elif op == 'FAIL_GM':
            require(g['controller'] and type(d.get('term')) is int and d['term'] == g['term'], 'stale_controller', 'current term required')
            self.failover(db, g, 'injected_failure')
        elif op == 'APPOINT_GM': self.failover(db, g, 'operator_appointment', d.get('agent_id'))
        elif op == 'TIMEOUT':
            # Prepare an official terminal result; the GM publishes and finishes it.
            if g['resolution'] is None:
                g['resolution'] = 'time_out'
                g['timeout_at'] = self.e.clock()
                ref = 'time_out'
                g['announcements'][ref] = {'kind':'time_out', 'published':False,
                    'data':{'ref':ref, 'kind':'time_out', 'resolution':'time_out',
                            'winners':[], 'losers':list(g['players']),
                            'text':'제한 시간이 종료되었습니다. 사건을 해결하지 못해 전원 패배합니다.'}}
                self.e.event(g, 'resolution_ready', {'resolution':'time_out'}, 'supervisor')
        elif op == 'FINISH': self.finish(db, g, g['resolution'] or 'operator_stopped')
        else: raise ContractError('invalid_input', 'unknown operator command')
        self.e.event(g, 'operator_action', {'operation': op})
        return self.e.credentials(g) | {'state': self.gm_view(db, g)}
    # [FIX-A43] the watchdog sweeps every 0.2 s and the runner renews leases every 2 s. A gap longer than the
    # shortest lease minus 5 s means the engine itself stood still (shorter stalls cannot expire a renewed lease).
    def sweep(self, db, g):
        now = self.e.clock()
        last, g['last_sweep'] = g.get('last_sweep'), now
        if g['ended'] or g['status'] == 'paused': return
        if last is not None and now - last >= min(g['rules']['player_lease'], g['rules']['gm_lease']) - 5:
            # [FIX-A43] 2026-11-11 v9-main2 seed 1: the whole machine stalled for minutes (a Codex call and the
            # heartbeat thread stood still with the engine). On waking, the wall clock had moved on and every
            # lease (12 players and the GM) expired at once. Time the engine could not serve is not the players'
            # fault: shift leases, deadlines and the talk round by the gap, as RESUME does after a pause.
            gap = now - last
            for p in g['players'].values():
                if p['mode'] == 'active': p['lease_until'] += gap
            if g['controller']: g['controller']['lease_until'] += gap
            for x in list(g['questions'].values()) + list(g['trades'].values()):
                if x.get('deadline') is not None: x['deadline'] += gap
            if g.get('talk') and not g['talk'].get('closed'): g['talk']['round_started'] += gap
            self.e.event(g, 'engine_stall', {'seconds': round(gap, 1)}, 'supervisor')
        if g['resolution'] is not None:
            if g['controller'] and self.e.clock() >= g['controller']['lease_until']: self.failover(db,g,'lease_expired')
            return
        for q in ([] if self.gm_led(g) else g['questions'].values()):   # [FIX-A33] GM: EXPIRE
            if q['status'] == 'pending' and self.e.clock() >= q['deadline']:
                q['mandatory_reason'] = 'timeout'  # [FIX-A15]
                if g['rules'].get('interaction_order') == 'simultaneous':
                    self.fail_question(db, g, q)  # [FIX-A17] mechanical: no stand-in answer, a new search
                else:
                    self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
        if g['rules'].get('interaction_order') == 'simultaneous' and not self.gm_led(g):
            for t in g['trades'].values():
                if t['status'] == 'proposed' and self.e.clock() >= t['deadline']:
                    self.cancel_trade(g, t, 'timeout')
            self.maybe_release(db, g)
        self.settle_trades(db, g)
        for aid, p in g['players'].items():
            if p['mode'] == 'active' and self.e.clock() >= p['lease_until']: self.fail_player(db, g, aid, 'lease_expired')
        if g['controller'] and self.e.clock() >= g['controller']['lease_until']: self.failover(db, g, 'lease_expired')

    def trade(self, db, g, d, operation):
        p = self.player(g, d)
        aid = p['agent_id']
        if operation == 'propose' and g['rules'].get('interaction_order') == 'simultaneous':
            return self.propose_declared(db, g, p, d)  # [FIX-A25]
        if operation == 'propose':
            require(g['phase'] == 'interaction', 'invalid_input', 'trades are proposed in the interaction phase')
            require(not any(t['proposer'] == aid and t['epoch'] == g['phase_epoch'] for t in g['trades'].values()), 'conflict', 'one proposal per phase')
            nf = g['rules'].get('interaction_order') == 'simultaneous'
            if nf:  # [FIX-A17] a proposal is this phase's action
                require(self.negotiating(g), 'conflict', 'proposals are made before actions run')
                require(aid not in g['spent'] and not self.replacement(g, aid), 'conflict', 'one action per phase')
                fulfill = d.get('fulfill', 'now')
                require(fulfill in FULFILL, 'invalid_input', 'fulfill is now, after_partner or withhold')
            other = d.get('recipient_agent_id')
            npc = None
            if other is None and type(d.get('target_id')) is int:
                target_cid = integer(d['target_id'], 'target_id', 0, 11)
                other = g['characters'][str(target_cid)]['controller']
            if other not in g['players']:
                target_cid = integer(d.get('target_id'), 'NPC target_id', 0, 11)
                npc = g['characters'][str(target_cid)]
                require(npc['mode'] == 'npc', 'invalid_input', 'target is not NPC')
                other = 'npc:'+str(target_cid)
            own = self.ch(g, p)
            target = npc or self.ch(g, g['players'][other])
            require(target['room'] == own['room'] and other != aid and target['mode'] != 'corpse', 'invalid_input', 'trade with a living peer in the same room')
            if nf:  # [FIX-A20] bundles: up to 3 owned clues for up to 3 requested items, unequal allowed
                offered, offer_category = self.bundle_offer(own, d), None
                requested = self.bundle_request(d)
                category = requested[0] if len(requested) == 1 and isinstance(requested[0], str) else None
                hypothesis = requested[0]['hypothesis'] if len(requested) == 1 and isinstance(requested[0], dict) else None
            else:
                requested = None
                offered, offer_category = self.offer(own,d)
                category = d.get('requested_category')
                hypothesis=d.get('requested_hypothesis')
                require(category in ('suspect', 'weapon', 'place', 'indirect') or isinstance(hypothesis,dict), 'invalid_input', 'request a category or hypothesis')
                if hypothesis is not None:
                    require(isinstance(hypothesis,dict) and set(hypothesis)=={'suspect','weapon','place'},'invalid_input','invalid trade hypothesis')
                    for k,limit in (('suspect',12),('weapon',8),('place',8)):integer(hypothesis[k],k,0,limit-1)
            tid = identifier(d.get('trade_id'), 'trade_id')
            require(tid not in g['trades'], 'conflict', 'trade ID already exists')
            accepted = self.rng(g, tid+':accept').random() < (.4 if npc and npc['mbti'][3] == 'J' else .7) if npc else False
            t = {'trade_id': tid, 'proposer': aid, 'recipient': other, 'target_character': target['character_id'],
                 'offers': {aid: offered}, 'offer_categories':{aid:offer_category}, 'requested_category': category,
                 'requested_hypothesis':hypothesis, 'requested': requested,
                 'status': 'accepted' if accepted else 'refused' if npc else 'proposed',
                 'accepted': accepted, 'fulfilled': {aid: False, other: False}, 'first_sender': None,
                 'deadline': self.e.clock()+g['rules']['trade_timeout'], 'cycle': g['cycle_id'], 'epoch': g['phase_epoch']}
            g['trades'][tid] = t
            g['disclosures'].append({'clue_id': offered[0] if offered else None, 'sender': aid, 'recipient': other, 'kind': 'possession', 'cycle': g['cycle_id']})
            if nf:
                t.update(decisions={aid: fulfill}, paid=[aid], decided_order=[aid] if fulfill == 'now' else [])
                self.pay_with_action(g, aid, tid, 'propose', target['character_id'])
                if npc:  # NPCs answer at once; J honours, P may not
                    if accepted and t['status'] == 'accepted':
                        matches = self.assign(list(target['information'].values()), requested, pick=True)
                        if matches:
                            t['offers'][other] = matches; t['offer_categories'][other] = category
                            honour = npc['mbti'][3] == 'J' or self.rng(g, tid+':honor').random() < .5
                            t['decisions'][other] = 'now' if honour else 'withhold'
                            self.resolve_trade(db, g, t)
                        else: t.update(accepted=False, status='refused')
                self.e.event(g, 'trade_proposed', {'trade_id': tid, 'proposer': aid, 'recipient': other}, 'private', (aid, other))
                self.maybe_release(db, g)
                return self.trade_view(g, t)
            if accepted:
                matches = [x['id'] for x in target['information'].values() if self.matches_trade(x,t)]
                t['offers'][other] = matches[:1]
                t['offer_categories'][other]=category
                if not matches: t.update(accepted=False, status='refused')
            self.e.event(g, 'trade_proposed', {'trade_id': tid, 'proposer': aid, 'recipient': other}, 'private', (aid, other))
            return self.trade_view(g, t)
        t = g['trades'].get(d.get('trade_id'))
        require(t is not None and aid in (t['proposer'], t['recipient']), 'unauthorized', 'trade participant required')
        require(self.e.clock() < t['deadline'] and t['status'] not in ('settled', 'refused'), 'expired', 'trade closed')
        if operation == 'respond' and t.get('form') == 'declared':
            return self.respond_declared(db, g, p, t, d)  # [FIX-A25]
        if operation == 'respond' and g['rules'].get('interaction_order') == 'simultaneous':
            return self.respond_trade(db, g, p, t, d)  # [FIX-A17]
        if operation == 'transfer' and g['rules'].get('interaction_order') == 'simultaneous':
            raise ContractError('conflict', 'trades settle from both sealed decisions; there is no separate transfer')
        if operation == 'respond':
            require(aid == t['recipient'] and t['status'] == 'proposed', 'conflict', 'recipient must answer proposal')
            decision = d.get('decision')
            require(decision in ('accept', 'refuse', 'counter'), 'invalid_input', 'unknown trade response')
            if decision == 'refuse': t['status'] = 'refused'
            else:
                own = self.ch(g, p)
                offers,offer_category=self.offer(own,d)
                offered_options=[own['information'][x] for x in offers] if offers else [x for x in own['information'].values() if x.get('category','indirect')==offer_category]
                require(any(self.matches_trade(x,t) for x in offered_options),'invalid_input','offer must match requested category/hypothesis')
                t['offers'][aid] = offers
                t['offer_categories'][aid]=offer_category
                t.update(status='proposed' if decision == 'counter' else 'accepted', accepted=decision == 'accept')
                if decision == 'counter':
                    original_ids=t['offers'][t['proposer']]
                    original = self.ch(g, g['players'][t['proposer']])['information'][original_ids[0]] if original_ids else {}
                    t['requested_category'] = original.get('category', t['offer_categories'][t['proposer']])
                    t['requested_hypothesis']=None
                    t['proposer'], t['recipient'] = t['recipient'], t['proposer']
            return self.trade_view(g, t)
        require(t['status'] == 'accepted', 'conflict', 'trade must be accepted')
        require(not t['fulfilled'][aid], 'conflict', 'already transferred')
        own = self.ch(g, p)
        recipient = t['recipient'] if aid == t['proposer'] else t['proposer']
        committed=t['offers'][aid]
        offered = d.get('information_id',committed[0] if committed else None)
        require(offered in own['information'], 'unauthorized', 'offered clue no longer owned')
        require((offered in committed if committed else own['information'][offered].get('category','indirect')==t['offer_categories'][aid]) and
                own['information'][offered].get('kind') in ('definite','indirect'), 'unauthorized','transfer must honor the promised ID/category')
        if aid==t['recipient']:require(self.matches_trade(own['information'][offered],t),'invalid_input','transfer does not match requested information')
        info = public_clue(own['information'][offered])
        if recipient not in g['players']:
            nc = g['characters'][recipient.split(':', 1)[1]]
            nc['information'][info['id']] = info
        else: self.acquire(g, recipient, info, 'trade', aid)
        t['fulfilled'][aid] = True
        t['first_sender'] = t['first_sender'] or aid
        if recipient not in g['players']:
            npc = g['characters'][recipient.split(':', 1)[1]]
            honor = npc['mbti'][3] == 'J' or self.rng(g, t['trade_id']+':honor').random() < .5
            if honor and t['offers'].get(recipient):
                info2 = npc['information'][t['offers'][recipient][0]]
                self.acquire(g, aid, info2, 'trade', recipient)
                t['fulfilled'][recipient] = True
        self.settle_trades(db, g)
        return self.trade_view(g, t)
    # ---- [FIX-A17] simultaneous helpers --------------------------------
    def negotiating(self, g):
        return (g['rules'].get('interaction_order') == 'simultaneous' and g['phase'] == 'interaction'
                and g.get('stage') == 'negotiation')
    def replacement(self, g, aid):
        rep = g.get('replacements', {}).get(aid)
        return rep if rep and rep.get('epoch') == g['phase_epoch'] else None
    def grant_replacement(self, g, aid, allowed, reason):
        p = g['players'].get(aid)
        if g['phase'] != 'interaction' or p is None or p['mode'] != 'active': return
        g['spent'].pop(aid, None)
        g.setdefault('replacements', {})[aid] = {'allowed': list(allowed), 'reason': reason, 'epoch': g['phase_epoch']}
        self.e.event(g, 'replacement_granted', {'agent_id': aid, 'allowed': list(allowed), 'reason': reason}, 'private', (aid,))
    def pay_with_action(self, g, aid, tid, role, target_cid):
        """Proposing or accepting a trade is this phase's action; an accepted proposal replaces the
        action the recipient had submitted (it is cancelled, not run)."""
        held = g.get('held', {}).pop(aid, None)
        if held:
            g['receipts'][held['action_id']]['result'].update(execution_status='completed', outcome='replaced_by_trade')
        req = g.get('requests', {}).get(aid)
        if req and req.get('status') == 'open':   # [FIX-A33] the trade used this phase's action
            req['status'] = 'traded'
        g['spent'][aid] = 'trade:'+tid
        p = g['players'][aid]; c = self.ch(g, p)
        g['activity'].append({'actor': p['character_id'], 'agent_id': aid, 'kind': 'trade', 'room': c['room'], 'target': target_cid,
            'target_mode': g['characters'][str(target_cid)]['mode'], 'approach': role, 'successes': 0, 'prepared': False,
            'cycle': g['cycle_id'], 'alone_players': False})
    def respond_trade(self, db, g, p, t, d):
        aid = p['agent_id']
        require(aid == t['recipient'] and t['status'] == 'proposed', 'conflict', 'recipient must answer proposal')
        decision = d.get('decision')
        require(decision in ('accept', 'refuse', 'counter'), 'invalid_input', 'unknown trade response')
        if decision == 'refuse':
            t['status'] = 'refused'
            self.e.event(g, 'trade_refused', {'trade_id': t['trade_id']}, 'private', (t['proposer'], t['recipient']))
            self.maybe_release(db, g)
            return self.trade_view(g, t)
        fulfill = d.get('fulfill', 'now')
        require(fulfill in FULFILL, 'invalid_input', 'fulfill is now, after_partner or withhold')
        if aid not in t['paid']:  # accepting costs this phase's action, unless already paid for this trade
            spent = g['spent'].get(aid)
            require(not (isinstance(spent, str) and spent.startswith('trade:')) and not self.replacement(g, aid),
                    'conflict', 'this phase\'s action is already used for another trade')
        own = self.ch(g, p)
        offers, offer_category = self.bundle_offer(own, d), None  # [FIX-A20]
        if decision == 'accept':
            require(len(offers) == len(t['requested']) and self.assign([own['information'][x] for x in offers], t['requested']),
                    'invalid_input', 'the offered clues must match every requested item, one each')
        else:
            new_request = self.bundle_request(d)   # a counter states what it wants in return
        if aid not in t['paid']:
            self.pay_with_action(g, aid, t['trade_id'], decision, g['players'][t['proposer']]['character_id'] if t['proposer'] in g['players'] else int(t['proposer'].split(':')[1]))
            t['paid'].append(aid)
        t['offers'][aid] = offers
        t['offer_categories'][aid] = offer_category
        if decision == 'counter':
            t['requested'] = new_request
            t['requested_category'] = new_request[0] if len(new_request) == 1 and isinstance(new_request[0], str) else None
            t['requested_hypothesis'] = None
            t['proposer'], t['recipient'] = t['recipient'], t['proposer']
            t.update(decisions={aid: fulfill}, decided_order=[aid] if fulfill == 'now' else [],
                     deadline=self.e.clock()+g['rules']['trade_timeout'])
            self.e.event(g, 'trade_countered', {'trade_id': t['trade_id']}, 'private', (t['proposer'], t['recipient']))
            return self.trade_view(g, t)
        t['decisions'][aid] = fulfill
        if fulfill == 'now': t['decided_order'].append(aid)
        t.update(status='accepted', accepted=True)
        self.resolve_trade(db, g, t)
        self.maybe_release(db, g)
        return self.trade_view(g, t)
    def resolve_trade(self, db, g, t):
        """Settle from the two sealed decisions. after_partner gives only if the partner gives now;
        both after_partner = a fizzled trade (actions spent, nothing moves, no reputation);
        withhold = broken (relationship -20, reputation)."""
        a, b = t['proposer'], t['recipient']
        d = t['decisions']
        def party(aid): return g['players'][aid]['character_id'] if aid in g['players'] else int(aid.split(':')[1])
        if d[a] == d[b] == 'after_partner':
            t.update(status='fizzled', fulfilled={a: False, b: False}, withheld={a: False, b: False})
            self.e.event(g, 'trade_fizzled', {'trade_id': t['trade_id']}, 'private', (a, b))
            return
        give = {x: d[x] == 'now' or (d[x] == 'after_partner' and d[b if x == a else a] == 'now') for x in (a, b)}
        t.update(fulfilled=give, withheld={x: d[x] == 'withhold' for x in (a, b)},
                 first_sender=(t['decided_order'] or [None])[0])
        for x in (a, b):
            other = b if x == a else a
            if give[x]:
                source = self.ch(g, g['players'][x])['information'] if x in g['players'] else \
                    g['characters'][x.split(':', 1)[1]]['information']
                for clue_id in t['offers'][x]:  # [FIX-A20] the whole bundle
                    if other in g['players'] and clue_id in source: self.acquire(g, other, public_clue(source[clue_id]), 'trade', x)
        t['status'] = 'settled'
        for x in (a, b):
            other = b if x == a else a
            if not give[x] and not t['withheld'][x]: continue   # conditional, partner withheld: neither
            self.relation(g, party(other), party(x), 10 if give[x] else -20, 'trade_fulfilled' if give[x] else 'trade_broken')
            if x in g['players']:
                rec = self.e.load(db, 'agents', x)
                rec['negotiation_reputation']['fulfilled' if give[x] else 'broken'] += 1
                self.e.put(db, 'agents', x, rec)
                g.setdefault('reputation', {})[x] = copy.deepcopy(rec['negotiation_reputation'])
            if other in g['players']:
                observer = self.e.load(db, 'agents', other)
                peer = observer.setdefault('peer_reputation', {}).setdefault(x, {'fulfilled': 0, 'broken': 0})
                peer['fulfilled' if give[x] else 'broken'] += 1
                self.e.put(db, 'agents', other, observer)
        self.e.event(g, 'trade_settled', {'trade_id': t['trade_id'], 'fulfilled': give}, 'private', (a, b))
        self.award_missions(g)
    def bundle_offer(self, own, d):
        """[FIX-A20] 1-3 clues I hold in full (definite or indirect), no repeats."""
        ids = d.get('offer_ids')
        require(isinstance(ids, list) and 1 <= len(ids) <= 3 and len(set(ids)) == len(ids) and
                all(isinstance(x, str) and own['information'].get(x, {}).get('kind') in ('definite', 'indirect') for x in ids),
                'unauthorized', 'offer 1-3 different clues you hold in full')
        return list(ids)
    def bundle_request(self, d):
        """[FIX-A20] 1-3 wanted items: 'definite' (any), 'suspect'|'weapon'|'place' (a definite clue of that
        category), 'indirect', or {'hypothesis': {...}}. requested_category / requested_hypothesis still work."""
        items = d.get('requested')
        if items is None:
            items = [d['requested_category']] if d.get('requested_category') else \
                [{'hypothesis': d['requested_hypothesis']}] if d.get('requested_hypothesis') else []
        require(isinstance(items, list) and 1 <= len(items) <= 3, 'invalid_input', 'request 1-3 items')
        for item in items:
            if isinstance(item, dict):
                h = item.get('hypothesis')
                require(set(item) == {'hypothesis'} and isinstance(h, dict) and set(h) == {'suspect', 'weapon', 'place'}, 'invalid_input', 'invalid trade hypothesis')
                for k, limit in (('suspect', 12), ('weapon', 8), ('place', 8)): integer(h[k], k, 0, limit-1)
            else:
                require(item in ('definite', 'suspect', 'weapon', 'place', 'indirect'), 'invalid_input', 'unknown requested item')
        return copy.deepcopy(items)
    @staticmethod
    def spec_matches(info, item):
        if info.get('kind') not in ('definite', 'indirect'): return False
        if isinstance(item, dict):
            h = item['hypothesis']
            if info['kind'] == 'definite': return h[info['category']] == info['candidate']
            return (info.get('character') == h['suspect'] or info.get('other') == h['suspect'] or
                    info.get('weapon') == h['weapon'] or info.get('room') == h['place'])
        if item == 'definite': return info['kind'] == 'definite'
        if item == 'indirect': return info['kind'] == 'indirect'
        return info['kind'] == 'definite' and info.get('category') == item
    def assign(self, infos, items, pick=False):
        """One distinct clue per requested item. With pick=True choose them from `infos` (NPCs);
        otherwise `infos` must be exactly the offered clues. Returns the clue IDs or None."""
        import itertools
        pool = infos if pick else infos[:len(items)]
        for combo in itertools.permutations(pool, len(items)):
            if all(self.spec_matches(x, item) for x, item in zip(combo, items)):
                return [x['id'] for x in combo]
        return None
    def cancel_trade(self, g, t, reason):
        """Mechanical failure: nothing moves, no relationship or reputation; whoever spent this phase's
        action on the trade gets a replacement action of its choice (not another trade)."""
        t.update(status='cancelled', cancel_reason=reason)
        if t['epoch'] == g['phase_epoch']:
            for aid in t.get('paid', []):
                if g['spent'].get(aid) == 'trade:'+t['trade_id']:
                    self.grant_replacement(g, aid, TRADE_REPLACEMENT, 'trade_'+reason)
        self.e.event(g, 'trade_cancelled', {'trade_id': t['trade_id'], 'reason': reason}, 'private',
                     tuple(x for x in (t['proposer'], t['recipient']) if x in g['players']))
    def fail_question(self, db, g, q):
        """Mechanical failure of an answer: the asker gets nothing and one replacement search."""
        q['status'] = 'failed'
        q['mandatory_fallback'] = True
        result = g['receipts'][q['action_id']]['result']
        result.update(execution_status='completed', outcome='question_failed', acquired_information=[], statements=[])
        self.complete_action(db, g, result)
        self.e.event(g, 'question_supervision', self.pair(q) | {'successes': q['successes'], 'approach': q['approach'],
                     'transferred_ids': [], 'failed': q.get('mandatory_reason')}, 'supervisor')
        if q.get('cycle_id', g['cycle_id']) == g['cycle_id']:
            self.grant_replacement(g, q['asker_id'], ['explore'], 'question_'+str(q.get('mandatory_reason')))
    def release_ready(self, g):
        active = [aid for aid, p in g['players'].items() if p['mode'] == 'active']
        return (self.negotiating(g) and bool(active) and all(aid in g['spent'] for aid in active)
                and not any(t['status'] == 'proposed' for t in g['trades'].values()))
    def advance_ready(self, g):
        active = [aid for aid, p in g['players'].items() if p['mode'] == 'active']
        return (g['status'] == 'running' and g['resolution'] is None and bool(active)
                and all(aid in g['spent'] for aid in active) and not self.negotiating(g)
                and not any(q['status'] == 'pending' for q in g['questions'].values())
                and not any(t['status'] == 'proposed' for t in g['trades'].values()))
    def needs_request(self, g):
        """[FIX-A33] Players the GM still has to ask this phase (incl. a replacement after a timeout)."""
        if g['status'] != 'running' or g['resolution'] is not None or not g['started']: return []
        if g.get('stage') == 'talk': return []   # [FIX-A42] actions come after the talk rooms close
        out = []
        for aid, p in g['players'].items():
            if p['mode'] != 'active' or (aid in g['spent'] and not self.replacement(g, aid)): continue
            req = g.get('requests', {}).get(aid)
            if req and req['phase_epoch'] == g['phase_epoch'] and req['status'] == 'open': continue
            out.append(aid)
        return sorted(out)
    def overdue(self, g):
        now = self.e.clock()
        return ([{'kind': 'question', 'ref': q['question_id']} for q in g['questions'].values()
                 if q['status'] == 'pending' and now >= q['deadline']] +
                [{'kind': 'trade', 'ref': t['trade_id']} for t in g['trades'].values()
                 if t['status'] == 'proposed' and now >= t['deadline']])
    # ---- [FIX-A42] talk rooms (담화) ----------------------------------------
    def open_talk(self, g):
        intent = g.pop('talk_intent', {}) or {}
        rounds = g['rules'].get('talk_rounds', 0)
        g['talk'] = None
        if not rounds or g['rules'].get('interaction_order') != 'simultaneous': return
        rooms = {}
        for aid, cycle in intent.items():
            p = g['players'].get(aid)
            if cycle == g['cycle_id'] and p and p['mode'] == 'active':
                rooms.setdefault(str(self.ch(g, p)['room']), []).append(aid)
        rooms = {r: sorted(m, key=lambda x: g['players'][x]['character_id']) for r, m in rooms.items() if len(m) >= 2}
        if not rooms: return
        g['talk'] = {'cycle': g['cycle_id'], 'round': 1, 'rounds': rounds, 'round_started': self.e.clock(), 'closed': False,
                     'rooms': {r: {'members': m, 'left': [], 'log': [], 'said': {}} for r, m in sorted(rooms.items())}}
        g['stage'] = 'talk'
        self.e.event(g, 'talk_opened', {'cycle': g['cycle_id'], 'rounds': rounds,
                     'rooms': {r: [g['players'][x]['character_id'] for x in m] for r, m in rooms.items()}}, 'supervisor')
    def room_open(self, g, room):
        return len([x for x in room['members'] if x not in room['left'] and g['players'][x]['mode'] == 'active']) >= 2
    def talk_facts(self, g):
        t = g.get('talk')
        if g.get('stage') != 'talk' or not t: return None
        waiting = [x for room in t['rooms'].values() if self.room_open(g, room) for x in room['members']
                   if x not in room['left'] and g['players'][x]['mode'] == 'active' and room['said'].get(x) != t['round']]
        return {'round': t['round'], 'rounds': t['rounds'], 'ready': not waiting, 'waiting': waiting,
                'overdue': self.e.clock() >= t['round_started'] + g['rules']['question_timeout']}
    def talk_next(self, g):
        t = g['talk']
        for room in t['rooms'].values():   # who did not speak by a forced close leaves (mechanical)
            if not self.room_open(g, room): continue
            for x in room['members']:
                if x not in room['left'] and room['said'].get(x) != t['round']:
                    room['left'].append(x)
                    self.e.event(g, 'talk_timeout', {'agent_id': x, 'round': t['round']}, 'supervisor')
        if t['round'] < t['rounds'] and any(self.room_open(g, r) for r in t['rooms'].values()):
            t.update(round=t['round'] + 1, round_started=self.e.clock())
            return {'talk_round': t['round']}
        t['closed'] = True
        g['stage'] = 'negotiation'
        self.e.event(g, 'talk_closed', {'cycle': g['cycle_id'], 'rounds_held': t['round']}, 'supervisor')
        return {'talk_closed': True, 'stage': g['stage']}
    def talk_view(self, g, p):
        """Members see the log; others in the room see only who is talking."""
        t = g.get('talk')
        if not t or t['cycle'] != g['cycle_id'] or g['phase'] != 'interaction': return None
        room = t['rooms'].get(str(self.ch(g, p)['room']))
        if room is None: return None
        cid = lambda x: g['players'][x]['character_id']
        if p['agent_id'] not in room['members']:
            return {'joined': False, 'talking': [cid(x) for x in room['members'] if x not in room['left']], 'closed': t['closed']}
        mine = p['agent_id']
        my_turn = (not t['closed'] and g.get('stage') == 'talk' and self.room_open(g, room)
                   and mine not in room['left'] and room['said'].get(mine) != t['round'])
        return {'joined': True, 'round': t['round'], 'rounds': t['rounds'], 'closed': t['closed'] or not self.room_open(g, room),
                'members': [cid(x) for x in room['members']], 'left': [cid(x) for x in room['left']],
                'log': copy.deepcopy(room['log']), 'my_turn': my_turn, 'must_speak': my_turn and t['round'] == 1}
    def talk_intent(self, g, p, d):
        require(not g['ended'] and p['mode'] == 'active' and g['phase'] == 'movement', 'conflict', 'talk intent is given in the movement phase')
        join = d.get('join')
        require(type(join) is bool, 'invalid_input', 'join is boolean')
        intents = g.setdefault('talk_intent', {})
        if join: intents[p['agent_id']] = g['cycle_id']
        else: intents.pop(p['agent_id'], None)
        return {'join': join, 'cycle': g['cycle_id']}
    def talk_say(self, g, p, d):
        view = self.talk_view(g, p)
        require(view is not None and view.get('joined') and view.get('my_turn'), 'conflict', 'not your turn in a talk room')
        t = g['talk']
        room = t['rooms'][str(self.ch(g, p)['room'])]
        leave = d.get('leave', False)
        require(type(leave) is bool, 'invalid_input', 'leave is boolean')
        message = self.talk(d.get('text'))
        require(message or (leave and t['round'] > 1), 'invalid_input', 'say something (in the first round you must speak)')
        c = self.ch(g, p)
        if message:
            room['log'].append({'round': t['round'], 'from_character': c['character_id'], 'text': message})
        room['said'][p['agent_id']] = t['round']
        if leave: room['left'].append(p['agent_id'])
        self.e.event(g, 'talk_said', {'agent_id': p['agent_id'], 'character_id': c['character_id'], 'job': c.get('job'),
                     'mbti': c.get('mbti'), 'room': c['room'], 'round': t['round'], 'text': message, 'leave': leave}, 'supervisor')
        if not self.gm_led(g) and self.talk_facts(g)['ready']:   # engine-orchestrated tests: move on at once
            self.talk_next(g)
        return {'said': bool(message), 'left': leave, 'round': t['round']}
    def maybe_release(self, db, g):
        """End the negotiation stage once everyone has submitted and no proposal is open; then run
        the held actions (by character, for determinism). [FIX-A33] Under GM orchestration the GM does
        this with RELEASE; the engine never moves on by itself."""
        if self.gm_led(g) or not self.release_ready(g): return
        self.release(db, g)
    def release(self, db, g):
        g['stage'] = 'execution'
        held = g.pop('held', {})
        for aid in sorted(held, key=lambda x: g['players'][x]['character_id']):
            h = held[aid]
            if g['players'][aid]['mode'] != 'active': continue
            try:
                self.execute(db, g, g['players'][aid], h['request'], h['action_id'], h['fp'])
            except ContractError as exc:  # became illegal while held (e.g. target left): spent, recorded
                g['receipts'][h['action_id']]['result'].update(execution_status='completed', outcome='invalid_at_execution',
                                                                error=str(exc)[:200])
        self.e.event(g, 'negotiation_closed', {'cycle_id': g['cycle_id']}, 'supervisor')
    def trade_view(self, g, t, viewer=None):
        if t.get('form') == 'declared':
            return self.declared_view(g, t, viewer)
        def character(aid):  # [FIX-A12] who the parties are, as characters the players can see
            return g['players'][aid]['character_id'] if aid in g['players'] else int(str(aid).split(':')[1])
        # [FIX-A17] sealed decisions stay hidden from both sides; the outcome shows after settlement
        return {k: copy.deepcopy(v) for k, v in t.items() if k not in ('offers', 'decisions', 'decided_order')} | {
            'proposer_character': character(t['proposer']), 'recipient_character': character(t['recipient']),
            'offers': {aid: [possession(self.offered_clue(g, cid)) for cid in ids] or [{'kind':'possession_category','category':t['offer_categories'][aid]}] for aid, ids in t['offers'].items()}}
    # ---- [FIX-A25] declared-type trades ---------------------------------------
    def talk(self, value):
        """[FIX-A27] Free talk: any content, true or false. Never verified, never a clue."""
        if value is None or value == '': return None
        require(isinstance(value, str) and len(value) <= TALK_LIMIT, 'invalid_input', f'message is text of at most {TALK_LIMIT} characters')
        return value
    def holder(self, g, x):
        return self.ch(g, g['players'][x]) if x in g['players'] else g['characters'][x.split(':', 1)[1]]
    def party(self, g, x):
        return self.holder(g, x)['character_id']
    @staticmethod
    def complete(c):
        return [x for x in c['information'].values() if x.get('kind') in ('definite', 'indirect')]
    @staticmethod
    def type_matches(info, item):
        """Does this clue fit {'type', 'exclude'}? Definite: that category, candidate not excluded.
        Indirect: that kind, its character (companion: neither character) / weapon not excluded."""
        kind, ex = item['type'], set(item.get('exclude') or [])
        if kind in ('suspect', 'weapon', 'place'):
            return info.get('kind') == 'definite' and info.get('category') == kind and info.get('candidate') not in ex
        if info.get('kind') != 'indirect' or info.get('type') != kind: return False
        if kind == 'character_room': return info.get('character') not in ex
        if kind == 'weapon_room': return info.get('weapon') not in ex
        return info.get('character') not in ex and info.get('other') not in ex
    def assign_typed(self, infos, items, pick=False):
        """One distinct clue per item. pick=True: choose from `infos` (possession check); otherwise
        `infos` are exactly the delivered clues. Returns the clue IDs or None."""
        import itertools
        pool = infos if pick else infos[:len(items)]
        if pick and len(pool) > 12:  # keep the search small: only clues that fit some item
            pool = [x for x in pool if any(self.type_matches(x, i) for i in items)]
        for combo in itertools.permutations(pool, len(items)):
            if all(self.type_matches(x, i) for x, i in zip(combo, items)):
                return [x['id'] for x in combo]
        return None
    def type_items(self, raw, declared=False):
        require(isinstance(raw, list) and 1 <= len(raw) <= 3, 'invalid_input', 'name 1-3 items')
        out = []
        for item in raw:
            if isinstance(item, str): item = {'type': item}
            allowed = {'type'} if declared else {'type', 'exclude'}
            require(isinstance(item, dict) and item.get('type') in TRADE_TYPES and set(item) <= allowed,
                    'invalid_input', 'item is {"type": %s%s}' % ('|'.join(TRADE_TYPES), '' if declared else ', "exclude": [numbers]'))
            ex = item.get('exclude') or []
            require(isinstance(ex, list) and len(set(ex)) == len(ex), 'invalid_input', 'exclude is a list of different numbers')
            for v in ex: integer(v, 'exclude', 0, TYPE_RANGE[item['type']]-1)
            out.append({'type': item['type'], 'exclude': sorted(ex)})
        return out
    @staticmethod
    def sealed_ids(own, ids, n):
        require(isinstance(ids, list) and len(ids) == n and len(set(ids)) == n and
                all(isinstance(x, str) and own['information'].get(x, {}).get('kind') in ('definite', 'indirect') for x in ids),
                'unauthorized', 'offer_ids: one clue you hold in full per item, no repeats')
        return list(ids)
    def propose_declared(self, db, g, p, d):
        """"I give these types, I want these types (with exclusions)". The engine checks that the
        proposer holds clues of the declared types; the clues actually handed over are sealed now
        and may differ (deception). Proposing is this phase's action."""
        aid = p['agent_id']
        require(g['phase'] == 'interaction', 'invalid_input', 'trades are proposed in the interaction phase')
        require(not any(t['proposer'] == aid and t['epoch'] == g['phase_epoch'] for t in g['trades'].values()), 'conflict', 'one proposal per phase')
        require(self.negotiating(g), 'conflict', 'proposals are made before actions run')
        require(aid not in g['spent'] and not self.replacement(g, aid), 'conflict', 'one action per phase')
        self.check_proposal(g, aid)   # [FIX-A38]
        target_cid = integer(d.get('target_id'), 'target_id', 0, 11)
        target, own = g['characters'][str(target_cid)], self.ch(g, p)
        require(target_cid != own['character_id'] and target['room'] == own['room'] and target['mode'] != 'corpse',
                'invalid_input', 'trade with a living peer in the same room')
        other, npc = target.get('controller'), None
        if other not in g['players']:
            require(target['mode'] == 'npc', 'invalid_input', 'target is not NPC')
            npc, other = target, 'npc:'+str(target_cid)
        if self.gm_led(g):  # [FIX-A33]
            req = g.get('requests', {}).get(aid)
            require(req is not None and req['phase_epoch'] == g['phase_epoch'] and req['status'] == 'open'
                    and 'trade' in req['options'] and target_cid in req['options']['trade']['targets'],
                    'conflict', 'no open GM request for this trade')
        declared = self.type_items(d.get('offer_types'), declared=True)
        requested = self.type_items(d.get('requested'))
        sealed = self.sealed_ids(own, d.get('offer_ids'), len(declared))
        require(self.assign_typed(self.complete(own), declared, pick=True) is not None,
                'unauthorized', 'you must hold a clue of every declared type')
        tid = identifier(d.get('trade_id'), 'trade_id')
        require(tid not in g['trades'], 'conflict', 'trade ID already exists')
        message = self.talk(d.get('message'))
        t = {'trade_id': tid, 'form': 'declared', 'proposer': aid, 'recipient': other, 'target_character': target_cid,
             'declared': {aid: declared}, 'requested': requested, 'sealed': {aid: sealed},
             'messages': [{'from_character': own['character_id'], 'stage': 'propose', 'text': message}] if message else [],
             'offers': {}, 'status': 'proposed', 'accepted': False, 'fulfilled': {aid: False, other: False},
             'withheld': {}, 'first_sender': None, 'paid': [aid],
             'deadline': self.e.clock()+g['rules']['trade_timeout'], 'cycle': g['cycle_id'], 'epoch': g['phase_epoch']}
        g['trades'][tid] = t
        self.use_proposal(g, aid)   # [FIX-A38]
        g['disclosures'].append({'clue_id': None, 'sender': aid, 'recipient': other, 'kind': 'possession',
                                 'cycle': g['cycle_id'], 'declared': [x['type'] for x in declared]})
        self.pay_with_action(g, aid, tid, 'propose', target_cid)
        if message: self.e.event(g, 'talk', {'from': aid, 'to': other, 'context': 'trade_propose', 'trade_id': tid, 'text': message}, 'supervisor')
        self.e.event(g, 'trade_proposed', {'trade_id': tid, 'proposer': aid, 'recipient': other}, 'private', (aid, other))
        if npc: self.npc_answer(db, g, t, npc)
        self.maybe_release(db, g)
        return self.trade_view(g, t, aid)
    def respond_declared(self, db, g, p, t, d):
        """accept: seal my clues for the request; the exchange happens at once, always.
        counter: a new declaration + request + sealed clues back to the proposer. refuse: closed."""
        aid = p['agent_id']
        require(aid == t['recipient'] and t['status'] == 'proposed', 'conflict', 'recipient must answer proposal')
        decision = d.get('decision')
        require(decision in ('accept', 'refuse', 'counter'), 'invalid_input', 'unknown trade response')
        if decision == 'counter': self.check_proposal(g, aid)   # [FIX-A38] a counter is a proposal
        message = self.talk(d.get('message'))
        own = self.ch(g, p)
        if decision == 'refuse':
            if message: t['messages'].append({'from_character': own['character_id'], 'stage': 'refuse', 'text': message})
            t['status'] = 'refused'
            self.e.event(g, 'trade_refused', {'trade_id': t['trade_id']}, 'private', (t['proposer'], t['recipient']))
            if message: self.e.event(g, 'talk', {'from': aid, 'to': t['proposer'], 'context': 'trade_refuse', 'trade_id': t['trade_id'], 'text': message}, 'supervisor')
            self.maybe_release(db, g)
            return self.trade_view(g, t, aid)
        if aid not in t['paid']:  # answering with accept/counter costs this phase's action
            spent = g['spent'].get(aid)
            require(not (isinstance(spent, str) and spent.startswith('trade:')) and not self.replacement(g, aid),
                    'conflict', 'this phase\'s action is already used for another trade')
        mine = self.complete(own)
        mbti_on = g['rules'].get('mbti_rules', False)
        if decision == 'accept':
            sealed = self.sealed_ids(own, d.get('offer_ids'), len(t['requested']))
            require(self.assign_typed(mine, t['requested'], pick=True) is not None,
                    'unauthorized', 'you must hold clues matching every requested item')
        else:
            declared = self.type_items(d.get('offer_types'), declared=True)
            new_request = self.type_items(d.get('requested'))
            sealed = self.sealed_ids(own, d.get('offer_ids'), len(declared))
            require(self.assign_typed(mine, declared, pick=True) is not None,
                    'unauthorized', 'you must hold a clue of every declared type')
        free_counter = decision == 'counter' and mbti_on and 'P' in own.get('mbti', '')  # [FIX-A29] P: countering is free
        if aid not in t['paid'] and not free_counter:
            self.pay_with_action(g, aid, t['trade_id'], decision, self.party(g, t['proposer']))
            t['paid'].append(aid)
        if free_counter and aid not in t['paid']: t.setdefault('owed', []).append(aid)
        if message:
            t['messages'].append({'from_character': own['character_id'], 'stage': decision, 'text': message})
            self.e.event(g, 'talk', {'from': aid, 'to': t['proposer'], 'context': 'trade_'+decision, 'trade_id': t['trade_id'], 'text': message}, 'supervisor')
        if decision == 'counter':
            self.use_proposal(g, aid)   # [FIX-A38]
            t['proposer'], t['recipient'] = aid, t['proposer']
            t.update(declared={aid: declared}, requested=new_request, sealed={aid: sealed},
                     deadline=self.e.clock()+g['rules']['trade_timeout'])
            g['disclosures'].append({'clue_id': None, 'sender': aid, 'recipient': t['recipient'], 'kind': 'possession',
                                     'cycle': g['cycle_id'], 'declared': [x['type'] for x in declared]})
            self.e.event(g, 'trade_countered', {'trade_id': t['trade_id']}, 'private', (t['proposer'], t['recipient']))
            return self.trade_view(g, t, aid)
        t['sealed'][aid] = sealed
        t['accepted'] = True
        for x in t.pop('owed', []):  # [FIX-A29] a P's free counter costs its action once it settles
            if x not in t['paid'] and x in g['players'] and not (isinstance(g['spent'].get(x), str) and g['spent'][x].startswith('trade:')):
                self.pay_with_action(g, x, t['trade_id'], 'counter', self.party(g, aid))
                t['paid'].append(x)
        self.settle_declared(db, g, t)
        self.maybe_release(db, g)
        return self.trade_view(g, t, aid)
    def npc_answer(self, db, g, t, npc):
        """NPCs answer at once: J always hands over what was asked, P half the time hands over
        something else (if it holds anything else)."""
        tid, other = t['trade_id'], t['recipient']
        accept = self.rng(g, tid+':accept').random() < (.4 if npc['mbti'][3] == 'J' else .7)
        mine = self.complete(npc)
        honest = self.assign_typed(mine, t['requested'], pick=True)
        if not accept or honest is None:
            t['status'] = 'refused'
            return
        ids = honest
        if npc['mbti'][3] != 'J' and self.rng(g, tid+':honor').random() >= .5:
            rest = [x for x in mine if x['id'] not in honest]
            if len(rest) >= len(honest) and self.assign_typed(rest[:len(honest)], t['requested']) is None:
                ids = [x['id'] for x in rest[:len(honest)]]
        t['sealed'][other] = ids
        t['accepted'] = True
        self.settle_declared(db, g, t)
    def settle_declared(self, db, g, t):
        """Both sealed sets move at once. A side is honest when what it handed over fits what it
        declared (proposer) or what was requested of it (recipient). Both honest: relationship +10
        each way. A deceiver: the deceived side's relationship to it -20 and a broken mark in the
        engine's hidden record; the honest side of a deception: no change."""
        a, b = t['proposer'], t['recipient']
        specs = {a: t['declared'][a], b: t['requested']}
        honest = {}
        for x in (a, b):
            other = b if x == a else a
            source = self.holder(g, x)['information']
            given = [source[c] for c in t['sealed'][x] if c in source]
            honest[x] = len(given) == len(specs[x]) and self.assign_typed(given, specs[x]) is not None
            for info in given:
                if other in g['players']: self.acquire(g, other, public_clue(info), 'trade', x)
                else: self.holder(g, other)['information'].setdefault(info['id'], public_clue(info))
        t.update(status='settled', offers=copy.deepcopy(t['sealed']), fulfilled=dict(honest),
                 withheld={x: not honest[x] for x in (a, b)}, first_sender=a if honest[a] else None)
        for x in (a, b):
            other = b if x == a else a
            if honest[x] and not all(honest.values()): continue
            xm = self.holder(g, x).get('mbti', '') if g['rules'].get('mbti_rules', False) else ''
            gain = MBTI_RULES['trade_gain'].get('J' if 'J' in xm else 'P' if 'P' in xm else '', 10)   # [FIX-A29]
            loss = MBTI_RULES['J_broken'] if 'J' in xm else -20
            self.relation(g, self.party(g, other), self.party(g, x), gain if honest[x] else loss,
                          'trade_fulfilled' if honest[x] else 'trade_broken')
            if x in g['players']:  # [FIX-A26] research record only; players never see it
                rec = self.e.load(db, 'agents', x)
                rec['negotiation_reputation']['fulfilled' if honest[x] else 'broken'] += 1
                self.e.put(db, 'agents', x, rec)
                g.setdefault('reputation', {})[x] = copy.deepcopy(rec['negotiation_reputation'])
        self.e.event(g, 'trade_settled', {'trade_id': t['trade_id']}, 'private', tuple(x for x in (a, b) if x in g['players']))
        self.e.event(g, 'trade_judged', {'trade_id': t['trade_id'], 'honest': honest}, 'supervisor')
        self.award_missions(g)
    def declared_view(self, g, t, viewer):
        """What a party sees: the declaration, the request, the talk, and after settlement which
        clue IDs it received and gave. Never the other side's sealed clues before settlement, never
        whether anyone was honest (A25), and the engine keeps no history for players (A26)."""
        view = {k: copy.deepcopy(t[k]) for k in ('trade_id', 'proposer', 'recipient', 'status', 'requested',
                                                  'messages', 'deadline', 'cycle', 'accepted', 'cancel_reason') if k in t}
        view.update(form='declared', proposer_character=self.party(g, t['proposer']),
                    recipient_character=self.party(g, t['recipient']),
                    offer_types=copy.deepcopy(t['declared'].get(t['proposer'])))
        if viewer in t.get('sealed', {}): view['my_sealed_offer'] = list(t['sealed'][viewer])
        if t['status'] == 'settled' and viewer in (t['proposer'], t['recipient']):
            other = t['recipient'] if viewer == t['proposer'] else t['proposer']
            view.update(received_ids=list(t['offers'].get(other, [])), gave_ids=list(t['offers'].get(viewer, [])))
        return view
    def offered_clue(self, g, cid):
        """[FIX-A23] An offered clue for the trade view. A corpse loses one clue per cycle, so the
        dead proposer of an old trade may no longer hold it; the scenario clue list still names it
        (start clues are copies of the same IDs). Before this, the lookup raised StopIteration and
        the partner's trades/inbox answered 500 for the rest of the game."""
        held = next((c['information'][cid] for c in g['characters'].values() if cid in c['information']), None)
        return held or next((x for x in g['clues'] if x['id'] == cid), {'id': cid, 'category': 'indirect'})
    def offer(self,own,d):
        ids=d.get('offer_ids',[]);category=d.get('offer_category')
        require(isinstance(ids,list) and len(ids)<=1,'invalid_input','offer one ID or one category')
        if ids:
            require(isinstance(ids[0],str) and ids[0] in own['information'] and own['information'][ids[0]].get('kind') in ('definite','indirect'), 'unauthorized','offer an owned complete clue')
            return ids,own['information'][ids[0]].get('category','indirect')
        require(category in ('suspect','weapon','place','indirect') and any(x.get('category','indirect')==category and x.get('kind') in ('definite','indirect') for x in own['information'].values()),'unauthorized','category signal requires owned evidence')
        return [],category
    def matches_trade(self,info,t):
        if info.get('kind') not in ('definite','indirect'):return False
        if t['requested_category'] is not None and info.get('category','indirect')!=t['requested_category']:return False
        h=t.get('requested_hypothesis')
        if h is None:return True
        if info['kind']=='definite':return h[info['category']]==info['candidate']
        return (info.get('character')==h['suspect'] or info.get('other')==h['suspect'] or
                info.get('weapon')==h['weapon'] or info.get('room')==h['place'])
    def settle_trades(self, db, g, force=False):
        for t in g['trades'].values():
            if t['status'] == 'accepted' and (force or self.e.clock() >= t['deadline'] or all(t['fulfilled'].values())):
                t['status'] = 'settled'
                for aid, fulfilled in t['fulfilled'].items():
                    other = t['recipient'] if aid == t['proposer'] else t['proposer']
                    cid = g['players'][aid]['character_id'] if aid in g['players'] else int(aid.split(':')[1])
                    other_cid = g['players'][other]['character_id'] if other in g['players'] else int(other.split(':')[1])
                    self.relation(g, other_cid, cid, 10 if fulfilled else -20, 'trade_fulfilled' if fulfilled else 'trade_broken')
                    if aid in g['players']:
                        a = self.e.load(db, 'agents', aid)
                        a['negotiation_reputation']['fulfilled' if fulfilled else 'broken'] += 1
                        self.e.put(db, 'agents', aid, a)
                        g.setdefault('reputation', {})[aid] = copy.deepcopy(a['negotiation_reputation'])  # [FIX-A16]
                    if other in g['players']:
                        observer=self.e.load(db,'agents',other)
                        peer=observer.setdefault('peer_reputation',{}).setdefault(aid,{'fulfilled':0,'broken':0})
                        peer['fulfilled' if fulfilled else 'broken']+=1
                        self.e.put(db,'agents',other,observer)
                self.e.event(g, 'trade_settled', {'trade_id': t['trade_id'], 'fulfilled': t['fulfilled']}, 'private', (t['proposer'], t['recipient']))
        self.award_missions(g)

    def relations_report(self, g, p, d):
        """[FIX-A35] user decision 2026-11-09: each agent's own relationship scores (its memory) reach the
        GM, who records the summary in its REPORT. The engine only relays them as a GM-only event: it
        keeps no relationship state, never shows them to players and never uses them in any rule."""
        require(not g['ended'] and p['mode'] == 'active', 'not_running', 'inactive')
        scores = d.get('scores')
        others = set(g['players']) - {p['agent_id']}
        require(isinstance(scores, dict) and 0 < len(scores) <= len(others), 'invalid_input', 'scores: {agent_id: 0..100}')
        for aid, value in scores.items():
            require(aid in others, 'invalid_input', 'scores are about other players of this game')
            require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100,
                    'invalid_input', 'a score is 0..100')
        self.e.event(g, 'relations_reported', {'agent_id': p['agent_id'], 'cycle': g['cycle_id'],
                                               'scores': {k: round(float(v), 1) for k, v in scores.items()}}, 'supervisor')
        return {'relayed': len(scores)}
    def events(self, db, g, d, role, aid=None):
        after = integer(d.get('after', 0), 'after')
        events = []
        for e in self.all_events(db,g,after):
            if e['sequence'] <= after: continue
            if e['visibility'] == 'shared' or (role == 'player' and aid in e['participants']):
                events.append(copy.deepcopy(e))
            elif role == 'gm' and e['visibility'] == 'supervisor': events.append(copy.deepcopy(e))
            elif role == 'gm' and e['type'] == 'action_completed':
                r = e['payload']['result']
                projection = {k: copy.deepcopy(r[k]) for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
                projection.update(acquired_information_ids=[c['id'] for c in r['acquired_information']],
                                  execution_status=r['execution_status'])
                events.append({k: copy.deepcopy(v) for k, v in e.items() if k != 'payload'} | {'payload': {'result': projection}})
        return {'events': events, 'event_sequence': g.get('event_count',0)}
    def handle(self, db, g, route, d):
        if route == 'games/credentials': self.e.operator(d); return self.e.credentials(g)
        if route == 'games/operator': return self.operator(db, g, d)
        if route == 'games/observe':
            self.e.operator(d)
            return self.gm_view(db, g) | {'assignments': copy.deepcopy(g['assignments']), 'observer_token': g['observer_token'],
                                         'relationship_graph': self.relationship_graph(g)}
        if route == 'games/export':
            self.e.operator(d)
            return {'game_id': g['id'], 'ruleset': SPEC, 'seed': g['seed'], 'rules': copy.deepcopy(g['rules']),
                    'model': self.pool(db, g['pool_id'])['model_version'], 'metrics': self.metrics(g),
                    'relationships': self.relationship_graph(g), 'relationship_events': [copy.deepcopy(x) for x in self.all_events(db,g) if x['type'] == 'relationship_changed'],
                    'feedback': copy.deepcopy(g.get('final_feedback')), 'provenance': copy.deepcopy(g.get('provenance', {})),
                    'visibility': 'operator-research; contains private relationship measurements'}
        if route == 'observer/snapshot':
            require(token_matches(d.get('observer_token'), g['observer_token']), 'unauthorized', 'spectator credential required')
            return self.visible(g)
        if route.startswith('gm/'):
            self.e.gm(g, d, allow_waiting=True, allow_ended=True)
            if route == 'gm/observe': return self.gm_view(db, g)
            if route == 'gm/command': return self.gm_command(db, g, d)
            if route == 'gm/heartbeat':
                require(not g['ended'], 'not_running', 'ended')
                g['controller']['lease_until'] = self.e.clock()+g['rules']['gm_lease']
                return {'term': g['term'], 'lease_until': g['controller']['lease_until']}
            if route == 'gm/mission-candidates':
                require(not g['started'], 'conflict', 'candidate draw is setup-only')
                activation = d.get('activate', [])
                return {'mission_candidates': self.mission_candidates(db, g, activation),
                        'characters': self.preview_characters(db, g, activation),
                        'indirect_clue_ids': [x['id'] for x in g['clues'] if x['kind'] == 'indirect']}
        if route == 'events':
            if 'controller_token' in d:
                self.e.gm(g, d, allow_waiting=True, allow_ended=True)
                return self.events(db, g, d, 'gm')
            p = self.player(g, d, read=True)
            self.check_archive_scope(db,g,p)
            return self.events(db, g, d, 'player', p['agent_id'])
        p = self.player(g, d, read=True)
        self.check_archive_scope(db,g,p)
        if route == 'agent/observe': return self.player_view(g, p)
        if route == 'relationships/graph': return self.relationship_graph(g, p['character_id'])
        if route == 'agent/heartbeat':
            require(not g['ended'] and p['mode'] == 'active', 'not_running', 'inactive')
            p['lease_until'] = self.e.clock()+g['rules']['player_lease']
            return {'lease_until': p['lease_until']}
        if route == 'agent/action': return self.action(db, g, d)
        if route == 'agent/result':
            receipt = g['receipts'].get(d.get('action_id')) or self.record(db,g,'action',d.get('action_id'))
            require(receipt is not None and receipt['result']['agent_id'] == p['agent_id'], 'unauthorized', 'own receipt required')
            return copy.deepcopy(receipt['result'])
        if route == 'questions/respond': return self.respond(db, g, d)
        if route == 'relations/report': return self.relations_report(g, p, d)   # [FIX-A35]
        if route == 'talk/intent': return self.talk_intent(g, p, d)   # [FIX-A42]
        if route == 'talk/say': return self.talk_say(g, p, d)         # [FIX-A42]
        if route == 'questions/inbox':
            return {'questions': [self.pair(q) | {'asker_id': q['asker_id'], 'asker_character': q['asker_character'],
                        'approach': q['approach'], 'hypothesis': copy.deepcopy(q['hypothesis']), 'successes': q['successes'],
                        'response_options': self.options(g, q), 'deadline': q['deadline'],
                        'extra_vague': q.get('extra_vague', 0), 'asker_message': q.get('asker_message')}
                    for q in g['questions'].values() if q['target_agent'] == p['agent_id'] and q['status'] == 'pending' and self.e.clock() < q['deadline']]}
        if route.startswith('trades/'):
            operation = route.split('/')[1]
            if operation == 'inbox':  # [FIX-A26] declared trades: open ones and this cycle's results only
                return {'trades': [self.trade_view(g, t, p['agent_id']) for t in g['trades'].values()
                                   if p['agent_id'] in (t['proposer'], t['recipient']) and
                                   (t.get('form') != 'declared' or t['status'] == 'proposed' or t['cycle'] == g['cycle_id'])]}
            require(operation in ('propose', 'respond', 'transfer'), 'not_found', 'unknown trade route')
            mid = identifier(d.get('message_id', f'{d.get("trade_id")}:{operation}:{p["agent_id"]}'), 'message_id')
            encoded = fingerprint(d)
            old = g['trade_receipts'].get(mid) or self.record(db,g,'trade_message',mid)
            if old:
                require(old['fingerprint'] == encoded, 'conflict', 'trade message_id reused')
                return copy.deepcopy(old['result'])
            result = self.trade(db, g, d, operation)
            g['trade_receipts'][mid] = {'fingerprint': encoded, 'result': copy.deepcopy(result)}
            return result
        if route == 'agent/share': raise ContractError('invalid_input', 'voluntary sharing removed; use negotiation')
        if route == 'documents/search':
            query = text(d.get('query', ''), 'query', 200)
            documents = [copy.deepcopy(x) for x in self.ch(g, p)['information'].values() if query in x.get('text', '')]
            self.e.event(g, 'document_search', {'agent_id': p['agent_id'], 'result_ids': [x['id'] for x in documents]}, 'private', (p['agent_id'],))
            return {'documents': documents, 'source': 'owned-case-records'}
        raise ContractError('not_found', 'unknown competitive route')
    def check_archive_scope(self,db,g,p):
        if g['ended']:
            member=self.e.load(db,'agents',p['agent_id'])
            require(member['busy_game_id'] in (None,g['id']),'stale_assignment',
                    'old episode secrets are sealed while assigned to another game; use pool/feedback')


def public_clue(clue): return {k: copy.deepcopy(v) for k, v in clue.items() if k != 'source_room'}
def possession(clue): return {'id': clue['id'], 'kind': 'possession', 'category': clue.get('category', 'indirect')}
