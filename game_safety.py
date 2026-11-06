"""Release only games successfully created by this driver on failure."""
import json
from pathlib import Path


class TrackedClient:
    def __init__(self, client, created=None):
        self.client = client
        self.created = [] if created is None else created
    def __getattr__(self, name):
        return getattr(self.client, name)
    def bind(self, **credentials):
        return TrackedClient(self.client.bind(**credentials), self.created)
    def call(self, route, **data):
        result = self.client.call(route, **data)
        if route == 'games/create':
            self.created.append(result['game_id'])
        return result


def cleanup(client, operator_token, root):
    errors = []
    operator = client.bind(operator_token=operator_token)
    for gid in client.created:
        try:
            members = [a['agent_id'] for a in operator.call('pool/list').get('agents', [])
                       if a.get('busy_game_id') == gid and a.get('pool_status') == 'fault']
            state = operator.call('games/observe', game_id=gid)
            if not state['ended']:
                operator.call('games/operator', game_id=gid, operation='FINISH')
            for aid in members:
                operator.call('pool/status', agent_id=aid, pool_status='available')
        except Exception as exc:
            errors.append(f'{gid}: cleanup failed ({type(exc).__name__})')
        try:
            record = operator.call('games/export', game_id=gid, scope='all')
            path = Path(root) / 'runs' / gid / 'full-record.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            from research_trace import export_views
            export_views(record, path.parent)
        except Exception as exc:
            errors.append(f'{gid}: export failed ({type(exc).__name__})')
    return errors
