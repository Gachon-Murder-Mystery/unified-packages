"""Operator-only research archive. Provider text is data, never game authority."""
import copy
import hashlib
import json
import time
import sqlite3
from contextlib import closing

SECRET_KEYS = {'key', 'api_key', 'apikey', 'authorization', 'token', 'pool_token',
               'controller_token', 'operator_token', 'observer_token', 'gm_token',
               'roll_secret', 'random_secret', 'writer_hash', 'headers'}
MAX_RECORD_BYTES = 2 * 1024 * 1024


def secrets_in(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SECRET_KEYS and isinstance(item, str) and item:
                found.append(item)
            else:
                found.extend(secrets_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(secrets_in(item))
    return found


def clean(value, secrets=()):
    """Keep language verbatim, except credentials and credential-bearing fields."""
    if isinstance(value, dict):
        return {key: clean(item, secrets) for key, item in value.items()
                if key.lower() not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [clean(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value


def tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS llm_records (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, call_id TEXT NOT NULL, status TEXT NOT NULL,
        fingerprint TEXT NOT NULL, state TEXT NOT NULL,
        UNIQUE(game_id,agent_id,call_id,status))''')
    db.execute('CREATE INDEX IF NOT EXISTS llm_records_game ON llm_records(game_id,sequence)')


def append(db, game_id, actor, role, context, record, writer_hash='', now=None, secrets=(), source='agent-submitted'):
    from integration import require, identifier, fingerprint
    require(isinstance(record, dict), 'invalid_input', 'record must be an object')
    require(set(record) <= {'call_id', 'status', 'stage', 'runtime', 'model', 'provider',
                           'request', 'response', 'usage', 'error', 'correlation',
                           'started_at', 'finished_at', 'capture'},
            'invalid_input', 'unknown LLM record field')
    call_id = identifier(record.get('call_id'), 'call_id')
    status = record.get('status')
    require(status in ('started', 'completed', 'failed'), 'invalid_input', 'invalid call status')
    for name in ('stage', 'runtime', 'model', 'provider', 'capture'):
        require(name not in record or isinstance(record[name], str) and len(record[name]) <= 500,
                'invalid_input', name+': invalid string')
    try:
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        require(False, 'invalid_input', 'record must be finite JSON')
    require(source != 'agent-submitted' or len(encoded.encode('utf-8')) <= MAX_RECORD_BYTES,
            'invalid_input', 'LLM record exceeds 2 MiB; no truncation performed')
    tables(db)
    safe = clean(json.loads(encoded), secrets)
    digest = fingerprint(safe)
    old = db.execute('SELECT fingerprint,state FROM llm_records WHERE game_id=? AND agent_id=? AND call_id=? AND status=?',
                     (game_id, actor, call_id, status)).fetchone()
    if old:
        require(old[0] == digest, 'conflict', 'call status already archived with different content')
        return {'sequence': json.loads(old[1])['sequence'], 'call_id': call_id, 'status': status, 'replayed': True}
    if status == 'started':
        require(not db.execute('SELECT 1 FROM llm_records WHERE game_id=? AND agent_id=? AND call_id=?',
                              (game_id, actor, call_id)).fetchone(), 'conflict', 'call already has a terminal record')
    else:
        require(not db.execute("SELECT 1 FROM llm_records WHERE game_id=? AND agent_id=? AND call_id=? AND status IN ('completed','failed')",
                              (game_id, actor, call_id)).fetchone(), 'conflict', 'call already terminated')
    state = safe | {'game_id': game_id, 'agent_id': actor, 'role': role,
                    'engine_context': copy.deepcopy(context), 'received_at': time.time() if now is None else now,
                    'writer_hash': writer_hash, 'archive_source': source}
    cursor = db.execute('INSERT INTO llm_records(game_id,agent_id,call_id,status,fingerprint,state) VALUES (?,?,?,?,?,?)',
                        (game_id, actor, call_id, status, digest, '{}'))
    state['sequence'] = cursor.lastrowid
    db.execute('UPDATE llm_records SET state=? WHERE sequence=?',
               (json.dumps(state, ensure_ascii=False, allow_nan=False), cursor.lastrowid))
    return {'sequence': cursor.lastrowid, 'call_id': call_id, 'status': status, 'replayed': False}


def ingest(engine, db, g, d, source='agent-submitted'):
    from integration import require, identifier, token_matches
    record = d.get('record')
    require(isinstance(record, dict), 'invalid_input', 'record required')
    call_id = identifier(record.get('call_id'), 'call_id')
    gm = 'controller_token' in d
    actor = identifier(d.get('controller_id') if gm else d.get('agent_id'), 'agent_id')
    capability = d.get('controller_token') if gm else d.get('token')
    require(isinstance(capability, str) and capability, 'unauthorized', 'role credential required')
    writer = hashlib.sha256(capability.encode()).hexdigest()
    tables(db)
    old = db.execute("SELECT state FROM llm_records WHERE game_id=? AND agent_id=? AND call_id=? AND status='started'",
                     (g['id'], actor, call_id)).fetchone()
    started = json.loads(old[0]) if old else None
    if started and record.get('status') in ('completed', 'failed'):
        # Retired workers may archive their own in-flight result, never act as GM.
        require(token_matches(writer, started['writer_hash']), 'unauthorized', 'original call credential required')
        role, context = started['role'], started['engine_context']
    else:
        if gm:
            engine.gm(g, d, allow_waiting=True, allow_ended=True)
            role = 'gm'
        else:
            if g.get('mode') == 'competitive':
                from competitive import Rules
                player = Rules(engine).player(g, d, read=True)
                Rules(engine).check_archive_scope(db, g, player)
            else:
                engine.player(g, d, allow_ended=True)
            role = 'player'
        context = {key: g.get(key) for key in ('cycle_id', 'phase', 'phase_epoch', 'term')}
    return append(db, g['id'], actor, role, context, record, writer, engine.clock(),
                  secrets_in(g) + secrets_in(d) + [engine.operator_token], source=source)


def invoke(engine, d):
    """Engine-hosted provider gateway; network I/O never holds the SQLite lock."""
    from integration import require, identifier, text, integer, ContractError
    from llm import Zen, GatewayError
    call_id = identifier(d.get('call_id'), 'call_id')
    system = text(d.get('system'), 'system', 500000)
    user = text(d.get('user'), 'user', 500000)
    key = text(d.get('api_key'), 'api_key', 500)
    require(bool(key.strip()), 'invalid_input', 'api_key required')
    budget = integer(d.get('budget', 700), 'budget', 1, 100000)
    gateway = Zen(key, text(d.get('model', 'gpt-6-luna'), 'model', 100),
                  d.get('protocol', 'responses'), d.get('connection', 'openai'))
    base = {'call_id': call_id, 'status': 'started', 'stage': d.get('stage', 'decision'),
            'runtime': 'engine-llm-gateway', 'capture': 'provider-json-response',
            'model': gateway.model, 'provider': gateway.service,
            'correlation': d.get('correlation', {}),
            'request': {'system': system, 'user': user, 'budget': budget, 'protocol': gateway.endpoint_family()}}
    credentials = {name: d[name] for name in ('game_id', 'agent_id', 'token', 'controller_id', 'controller_token', 'term') if name in d}
    # The unique started record is the claim. A replay never makes another paid call.
    def submit(record):
        with engine.transaction() as db:
            game = engine.game(db, credentials)
            result = ingest(engine, db, game, credentials | {'record': clean(record, [key])}, source='engine-gateway')
            if record['status'] == 'started' and not result['replayed']:
                require(not game['ended'] and game['status'] != 'paused', 'not_running', 'no new LLM calls while paused or ended')
                if 'controller_token' not in credentials:
                    player = game['players'][credentials['agent_id']]
                    require(player['mode'] == 'active', 'unauthorized', 'active player required for new LLM call')
                    require(engine.clock() < player['lease_until'], 'expired', 'player lease expired')
            return result
    receipt = submit(base)
    if receipt['replayed']:
        actor = credentials['controller_id'] if 'controller_token' in credentials else credentials['agent_id']
        with engine.transaction() as db:
            row = db.execute("SELECT state FROM llm_records WHERE game_id=? AND agent_id=? AND call_id=? AND status IN ('completed','failed')",
                             (d['game_id'], actor, call_id)).fetchone()
        require(row is not None, 'call_in_progress', 'call started; do not resubmit with a new ID unless intentionally calling again')
        record = json.loads(row[0])
        require(record.get('archive_source') == 'engine-gateway', 'conflict', 'call ID belongs to externally submitted data')
        require(record['status'] == 'completed', 'llm_error', 'archived LLM call failed; inspect operator export')
        return {'call_id': call_id, 'text': record['response']['returned_text'],
                'usage': record.get('usage'), 'replayed': True}
    captured = {}
    def save(record):
        if record['status'] == 'started':
            return
        terminal = base | {name: record[name] for name in ('status', 'response', 'usage', 'error', 'finished_at') if name in record}
        terminal = clean(terminal, [key])
        submit(terminal)
        captured.update(terminal)
    gateway.trace_sink = save
    try:
        returned = gateway.request(system, user, budget)
    except GatewayError as exc:
        raise ContractError('llm_truncated' if getattr(exc,'code',None)=='truncated' else 'llm_error', str(exc)) from None
    return {'call_id': call_id, 'text': returned, 'usage': captured.get('usage'), 'replayed': False}


def records(db, game_id):
    tables(db)
    return [json.loads(row[0]) for row in db.execute(
        'SELECT state FROM llm_records WHERE game_id=? ORDER BY sequence', (game_id,))]


def native_sink(path, game_id):
    """Only engine-owned gateways use this sink; never exposed as an HTTP route."""
    def receive(record):
        with closing(sqlite3.connect(str(path), timeout=10)) as db:
            with db:
                row = db.execute('SELECT state FROM games WHERE id=?', (game_id,)).fetchone()
                if row is None:
                    raise ValueError('LLM capture requires an existing game')
                game = json.loads(row[0])
                correlation = record.get('correlation', {})
                append(db, game_id, correlation.get('agent_id', 'native:gateway'), correlation.get('role', 'gateway'),
                       {key: game.get(key) for key in ('turn', 'cycle', 'phase')} | correlation, record,
                       secrets=secrets_in(game), source='engine-native-gateway')
    return receive


def export_all(engine, db, g):
    from integration import VERSION
    traces = records(db, g['id'])
    archived = []
    events = copy.deepcopy(g.get('events', []))
    if g.get('mode') == 'competitive':
        from competitive import Rules
        rules = Rules(engine)
        events = rules.all_events(db, g)
        archived = [{'kind': row[0], 'record_id': row[1], 'record': json.loads(row[2])}
                    for row in db.execute('SELECT kind,key,state FROM competitive_records WHERE game_id=? ORDER BY kind,key', (g['id'],))]
    calls = {}
    for record in traces:
        calls.setdefault((record['agent_id'], record['call_id']), set()).add(record['status'])
    terminal = sum(bool(status & {'completed', 'failed'}) for status in calls.values())
    participants = set(g.get('players', {}))
    participants.update(g.get('agents', {}))
    for event in events:
        payload = event.get('payload', {})
        if isinstance(payload, dict) and payload.get('controller_id'):
            participants.add(payload['controller_id'])
        if event.get('controller_id'):
            participants.add(event['controller_id'])
    if g.get('controller'):
        participants.add(g['controller'].get('controller_id', g['controller'].get('id')))
    experiences = [item for (raw,) in db.execute('SELECT state FROM experience_events')
                   if (item := json.loads(raw)).get('game_id') == g['id']]
    result = {'export_version': 'research-v1', 'schema_version': VERSION, 'scope': 'all',
              'game_id': g['id'], 'exported_at': engine.clock(), 'visibility': 'operator-only-private-research',
              'game': copy.deepcopy(g), 'events': events, 'archived_records': archived,
              'experience_events': experiences, 'llm_records': traces,
              'capture': {'record_count': len(traces), 'call_count': len(calls), 'terminal_calls': terminal,
                          'unfinished_calls': len(calls)-terminal,
                          'agents_with_records': sorted({x['agent_id'] for x in traces}),
                          'participants_without_records': sorted(participants - {x['agent_id'] for x in traces}),
                          'coverage': 'instrumented-calls-only; not proof that every external call was submitted',
                          'reasoning': 'provider-returned text/summary only; unreturned internal reasoning unavailable',
                          'redactions': 'authentication fields and known credentials; language otherwise preserved'}}
    return clean(result, secrets_in(g) + [engine.operator_token])
