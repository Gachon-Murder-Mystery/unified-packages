"""Local operator entrypoint. Explicit credentials are never printed by the server."""
import argparse
import json
from pathlib import Path

from integration import ExternalEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/games.sqlite3')
    parser.add_argument('operation', choices=('credentials', 'call', 'tick', 'export'))
    parser.add_argument('--route')
    parser.add_argument('--json', default='{}')
    parser.add_argument('--game-id')
    parser.add_argument('--scope', choices=('all', 'summary'), default='all')
    parser.add_argument('--output', help='Write UTF-8 JSON directly, without shell encoding conversion')
    args = parser.parse_args()
    Path(args.data).parent.mkdir(parents=True, exist_ok=True)
    engine = ExternalEngine(args.data)
    if args.operation == 'credentials':
        result = {'operator_token': engine.operator_token}
    elif args.operation == 'tick':
        engine.tick()
        result = {'status': 'watchdog_checked'}
    elif args.operation == 'export':
        if not args.game_id:
            parser.error('export requires --game-id')
        result = engine.handle('games/export', {'game_id': args.game_id, 'scope': args.scope,
                                               'operator_token': engine.operator_token})
    else:
        if not args.route:
            parser.error('call requires --route')
        data = json.loads(args.json)
        result = engine.handle(args.route, data | {'operator_token': engine.operator_token})
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded+'\n', encoding='utf-8')
        print('Saved:', target)
    else:
        print(encoded)


if __name__ == '__main__':
    main()
