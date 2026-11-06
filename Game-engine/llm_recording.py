"""Record external B/C client.request calls without changing their decisions.

Pass a role-bound EngineClient and keep the wrapped client's original return
value. A local journal survives upload failures; flush() retries pending records.
This captures what client.request returns, not inaccessible provider internals.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from research import clean, secrets_in


class RecordingLLMClient:
    def __init__(self, client, engine_client, *, model='', provider='', stage='decision',
                 correlation=None, spool='data/llm_spool.jsonl'):
        self.client, self.engine = client, engine_client
        self.model = model or getattr(client, 'model', '')
        self.provider, self.stage = provider, stage
        self.correlation = dict(correlation or {})
        self.spool = Path(spool)
        self.lock = threading.RLock()
        self.secrets = secrets_in(engine_client.credentials)
        for name in ('key', 'api_key'):
            value = getattr(client, name, None)
            if isinstance(value, str) and value:
                self.secrets.append(value)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _journal(self, item):
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        with self.spool.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(clean(item, self.secrets), ensure_ascii=False, allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())

    def _identity(self):
        c = self.engine.credentials
        return {'game_id': c['game_id'], 'agent_id': c['controller_id'] if 'controller_token' in c else c['agent_id']}

    def _send(self, record):
        with self.lock:
            item = self._identity() | {'record': record}
            self._journal(item)
            # Never silently drop a record or pretend the call was archived.
            result = self.engine.record_llm(clean(record, self.secrets))
            self._journal(self._identity() | {'ack': [record['call_id'], record['status']]})
            return result

    def flush(self):
        """Retry this identity's unacknowledged records; original IDs are stable."""
        with self.lock:
            if not self.spool.exists():
                return 0
            pending = {}
            identity = self._identity()
            for line in self.spool.read_text(encoding='utf-8').splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    raise ValueError('Incomplete LLM journal; recover the final line before retrying') from None
                if any(item.get(k) != v for k, v in identity.items()):
                    continue
                if 'record' in item:
                    record = item['record']
                    pending[(record['call_id'], record['status'])] = record
                elif 'ack' in item:
                    pending.pop(tuple(item['ack']), None)
            for record in pending.values():
                self.engine.record_llm(record)
                self._journal(identity | {'ack': [record['call_id'], record['status']]})
            return len(pending)

    def request(self, *args, **kwargs):
        call_id = uuid.uuid4().hex
        base = {'call_id': call_id, 'model': self.model, 'provider': self.provider,
                'stage': self.stage, 'runtime': 'external-llm-client',
                'capture': 'client-return-value', 'correlation': dict(self.correlation),
                'started_at': time.time()}
        self._send(base | {'status': 'started', 'request': {'args': args, 'kwargs': kwargs}})
        try:
            returned = self.client.request(*args, **kwargs)
        except Exception as exc:
            self._send(base | {'status': 'failed', 'finished_at': time.time(),
                               'error': {'type': type(exc).__name__, 'message': str(exc)}})
            raise
        usage = returned[1] if isinstance(returned, tuple) and len(returned) == 2 else None
        self._send(base | {'status': 'completed', 'finished_at': time.time(),
                           'response': returned, 'usage': usage})
        return returned
