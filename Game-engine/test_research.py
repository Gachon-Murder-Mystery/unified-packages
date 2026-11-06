"""Private language archive tests. No provider or paid LLM calls."""
import io
import json
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
import threading
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_competitive
from integration import ExternalEngine, ContractError
from engine_client import EngineLLMClient, EngineClient, EngineAPIError
from launcher import LocalClient
from llm import Zen, GatewayError
from llm_recording import RecordingLLMClient
from research import MAX_RECORD_BYTES, native_sink


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.f = test_competitive.CompetitiveTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.e = self.f.e
        self.creds = self.f.pc(self.f.ids[0])
        self.client = LocalClient(self.e, self.creds)
        # Exercise the same convenience method used by the HTTP client.
        self.client.record_llm = lambda record: self.client.call('llm/records', record=record)
        self.client.credentials = self.creds

    def archive(self):
        return self.e.handle('games/export', self.f.op | {'game_id': 'case', 'scope': 'all'})

    def submit(self, status, call_id='call-1', **fields):
        return self.client.record_llm({'call_id': call_id, 'status': status, **fields})

    def assertCode(self, code, fn):
        with self.assertRaises(ContractError) as result:
            fn()
        self.assertEqual(result.exception.code, code)

    def provider(self):
        return {'output': [{'type': 'reasoning', 'summary': [{'text': '비공개 판단 요약'}]},
                           {'type': 'message', 'content': [{'type': 'output_text', 'text': '  답변\n두 번째 줄  '}]}],
                'usage': {'output_tokens': 37, 'output_tokens_details': {'reasoning_tokens': 20}}}

    def response(self, body=None):
        return io.BytesIO(json.dumps(body or self.provider(), ensure_ascii=False).encode())

    def test_language_preserved_restart_complete_archive_and_no_credentials(self):
        original = '  판단: 김씨 → 이씨\n<think>가설 검토</think> 😀  '
        self.submit('started', request={'messages': [{'role': 'user', 'content': original}], 'api_key': 'SECRET'},
                    correlation={'action_id': 'a1'})
        self.submit('completed', response={'text': original, 'reasoning_content': original}, usage={'output_tokens': 50})
        self.e = ExternalEngine(self.f.path)
        result = self.archive()
        self.assertEqual(result['llm_records'][1]['response']['text'], original)
        self.assertEqual(result['llm_records'][1]['response']['reasoning_content'], original)
        self.assertEqual(result['capture']['call_count'], 1)
        self.assertEqual(result['capture']['unfinished_calls'], 0)
        self.assertEqual(len(result['events']), self.f.state()['event_count'])
        self.assertTrue(result['archived_records'])
        self.assertTrue(all('record_id' in x for x in result['archived_records']))
        encoded = json.dumps(result)
        for secret in (self.f.op['operator_token'], self.creds['token'], self.f.gm['controller_token'], 'SECRET'):
            self.assertNotIn(secret, encoded)
        self.assertNotIn('writer_hash', encoded)
        self.assertIn('truth', result['game'])  # Research only, not player export.

    def test_authentication_and_no_research_data_in_player_gm_or_spectator_views(self):
        self.submit('completed', response='PRIVATE-LANGUAGE')
        for creds in (self.creds, self.f.gm, {'observer_token': self.f.new['observer_token'], 'game_id': 'case'}):
            self.assertCode('unauthorized', lambda: self.e.handle('games/export', creds | {'scope': 'all'}))
        for route, creds in (('agent/observe', self.creds), ('gm/observe', self.f.gm),
                             ('events', self.creds), ('events', self.f.gm),
                             ('observer/snapshot', {'game_id': 'case', 'observer_token': self.f.new['observer_token']})):
            self.assertNotIn('PRIVATE-LANGUAGE', json.dumps(self.e.handle(route, creds)))
        self.assertCode('unauthorized', lambda: self.e.handle('llm/records', self.creds | {'agent_id': self.f.ids[1],
            'record': {'call_id': 'spoof', 'status': 'started'}}))

    def test_replay_conflicts_concurrent_duplicates_and_unfinished_calls(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.submit('started', request='same'), range(4)))
        self.assertEqual(sum(not x['replayed'] for x in results), 1)
        self.assertCode('conflict', lambda: self.submit('started', request='different'))
        self.assertEqual(self.archive()['capture']['unfinished_calls'], 1)
        self.submit('completed', response='ok')
        self.assertTrue(self.submit('completed', response='ok')['replayed'])
        self.assertCode('conflict', lambda: self.submit('failed', error='different terminal'))
        self.assertEqual(self.archive()['capture']['record_count'], 2)

    def test_retired_gm_can_finish_own_trace_but_cannot_start_or_command(self):
        gm = dict(self.f.gm)
        self.e.handle('llm/records', gm | {'record': {'call_id': 'slow-gm', 'status': 'started'}})
        self.f.call('games/operator', self.f.op, game_id='case', operation='FAIL_GM', term=gm['term'])
        self.e.handle('llm/records', gm | {'record': {'call_id': 'slow-gm', 'status': 'completed', 'response': 'late'}})
        self.assertCode('stale_controller', lambda: self.e.handle('llm/records', gm | {'record': {'call_id': 'new', 'status': 'started'}}))
        self.assertCode('stale_controller', lambda: self.e.handle('gm/heartbeat', gm))
        trace = self.archive()['llm_records'][-1]
        self.assertEqual(trace['role'], 'gm')
        self.assertEqual(trace['engine_context']['term'], gm['term'])
        self.assertCode('unauthorized', lambda: self.e.handle('llm/records', gm | {'controller_token': 'wrong',
            'record': {'call_id': 'slow-gm', 'status': 'completed', 'response': 'late'}}))

    def test_bad_record_no_authority_fields_and_oversize_not_truncated(self):
        self.assertCode('invalid_input', lambda: self.submit('completed', agent_id='spoof'))
        self.assertCode('invalid_input', lambda: self.submit('completed', usage={'x': float('nan')}))
        self.assertCode('invalid_input', lambda: self.submit('completed', response='x'*MAX_RECORD_BYTES))
        self.assertFalse(self.archive()['llm_records'])

    def test_external_wrapper_preserves_return_type_and_recovers_upload_failure(self):
        class Fake:
            model = 'stub'
            def request(self, system, user, budget=700):
                return '判断\n{"consideration":"검토"}', {'output_tokens': 10}
        spool = Path(self.f.tmp.name)/'journal.jsonl'
        recorder = RecordingLLMClient(Fake(), self.client, spool=spool)
        original = self.client.record_llm
        def interrupted(record):
            if record['status'] == 'completed':
                raise ConnectionError('engine disconnected')
            return original(record)
        self.client.record_llm = interrupted
        with self.assertRaises(ConnectionError):
            recorder.request('system', 'user')
        self.assertEqual(self.archive()['capture']['unfinished_calls'], 1)
        self.client.record_llm = original
        self.assertEqual(recorder.flush(), 1)
        self.assertEqual(recorder.flush(), 0)
        self.assertEqual(self.archive()['llm_records'][-1]['response'][1]['output_tokens'], 10)
        self.assertIsInstance(recorder.request('system', 'user'), tuple)
        self.assertNotIn(self.creds['token'], spool.read_text(encoding='utf-8'))

    def test_wrapper_archives_exception_and_reraises(self):
        class Broken:
            def request(self, *args, **kw):
                raise TimeoutError('timed out')
        recorder = RecordingLLMClient(Broken(), self.client, spool=Path(self.f.tmp.name)/'spool.jsonl')
        with self.assertRaises(TimeoutError):
            recorder.request('system', 'user')
        result = self.archive()
        self.assertEqual(result['llm_records'][-1]['error']['type'], 'TimeoutError')
        self.assertEqual(result['capture']['unfinished_calls'], 0)

    def test_native_gateway_keeps_provider_reasoning_and_original_body(self):
        body = self.provider()
        gateway = Zen('sk-test-key', connection='openai', trace_sink=native_sink(self.f.path, 'case')).for_actor('native-player', 'player')
        with patch('urllib.request.urlopen', return_value=self.response(body)):
            self.assertEqual(gateway.request('system', 'user'), '답변\n두 번째 줄')
        trace = self.archive()['llm_records'][-1]
        self.assertEqual(trace['agent_id'], 'native-player')
        self.assertEqual(trace['response']['json'], body)
        self.assertEqual(json.loads(trace['response']['raw_body']), body)
        self.assertEqual(trace['usage']['output_tokens_details']['reasoning_tokens'], 20)

    def test_native_gateway_malformed_output_and_http_error_are_archived(self):
        gateway = Zen('sk-test-secret', connection='openai', trace_sink=native_sink(self.f.path, 'case'))
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'NOT JSON')):
            with self.assertRaises(GatewayError):
                gateway.request('system', 'user')
        self.assertEqual(self.archive()['llm_records'][-1]['response']['raw_body'], 'NOT JSON')
        error = urllib.error.HTTPError('https://example.invalid', 429, 'limited', {}, io.BytesIO(b'{"error":{"message":"sk-test-secret"}}'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(GatewayError):
                gateway.request('system', 'user')
        self.assertNotIn('sk-test-secret', json.dumps(self.archive()))

    def test_engine_gateway_provider_capture_idempotence_and_no_network_for_unauthorized(self):
        llm = EngineLLMClient(self.client, 'sk-fake-key', model='gpt-test')
        with patch('urllib.request.urlopen', return_value=self.response()) as network:
            first = llm.request('system', 'user', call_id='stable-id')
            second = llm.request('system', 'user', call_id='stable-id')
        self.assertEqual(first, second)
        self.assertEqual(network.call_count, 1)
        result = self.archive()
        self.assertEqual(result['llm_records'][-1]['response']['json']['output'][0]['type'], 'reasoning')
        self.assertNotIn('sk-fake-key', json.dumps(result))
        self.assertEqual(result['llm_records'][-1]['archive_source'], 'engine-gateway')
        with patch('urllib.request.urlopen') as network:
            self.assertCode('conflict', lambda: llm.request('different system', 'user', call_id='stable-id'))
            self.assertCode('unauthorized', lambda: self.e.handle('llm/invoke', self.creds | {'token': 'wrong',
                'call_id': 'bad', 'system': 's', 'user': 'u', 'api_key': 'fake'}))
            network.assert_not_called()

    def test_engine_gateway_failure_replay_and_outstanding_call(self):
        llm = EngineLLMClient(self.client, 'fake', model='gpt-test')
        with patch('urllib.request.urlopen', side_effect=TimeoutError('offline')) as network:
            self.assertCode('llm_error', lambda: llm.request('s', 'u', call_id='failed'))
            self.assertCode('llm_error', lambda: llm.request('s', 'u', call_id='failed'))
        self.assertEqual(network.call_count, 1)
        self.assertEqual(self.archive()['capture']['unfinished_calls'], 0)

    def test_cli_exports_utf8_file_without_stdout_secrets(self):
        self.submit('completed', response='한국어 원문')
        target = Path(self.f.tmp.name)/'export.json'
        result = subprocess.run([sys.executable, '-B', 'integration_cli.py', '--data', self.f.path, 'export',
            '--game-id', 'case', '--output', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['llm_records'][0]['response'], '한국어 원문')
        self.assertNotIn('operator_token', result.stdout)

    def test_real_http_large_prompts_and_ingestion_then_operator_export(self):
        from app import Handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.external_engine = self.e
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = EngineClient(f'http://127.0.0.1:{server.server_port}', self.creds, timeout=15)
            llm = EngineLLMClient(client, 'fake', model='gpt-test')
            original = urllib.request.urlopen
            def route(request, *args, **kw):
                if request.full_url.startswith('https://api.openai.com/'):
                    return self.response()
                return original(request, *args, **kw)
            with patch('urllib.request.urlopen', side_effect=route):
                text, usage = llm.request('system', '긴 입력 '*10000, call_id='http-call')
            self.assertIn('답변', text)
            self.assertEqual(usage['output_tokens'], 37)
            client.record_llm({'call_id': 'direct-http', 'status': 'completed', 'response': '추론 '*10000})
            operator = EngineClient(client.base_url, self.f.op | {'game_id': 'case'})
            archived = operator.export_game()
            self.assertEqual(archived['capture']['call_count'], 2)
            self.assertEqual(archived['llm_records'][-1]['response'], '추론 '*10000)
            with self.assertRaises(EngineAPIError) as error:
                client.export_game()
            self.assertEqual(error.exception.status, 401)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_legacy_external_and_native_single_games_can_export(self):
        import test_integration
        from app import Store
        from engine import new_game
        legacy = test_integration.IntegrationTests()
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        legacy.call('llm/records', legacy.gm, record={'call_id': 'legacy', 'status': 'completed', 'response': '옛 연결도 보존'})
        full = legacy.call('games/export', legacy.op, game_id='case-one', scope='all')
        self.assertEqual(full['llm_records'][0]['response'], '옛 연결도 보존')
        self.assertTrue(full['events'])
        game = new_game('테스터', 0, 4, '사건 해결')
        Store(self.f.path).save(game)
        sink = native_sink(self.f.path, game['id'])
        sink({'call_id': 'single', 'status': 'completed', 'response': '단일 플레이 원문'})
        full = self.e.handle('games/export', self.f.op | {'game_id': game['id'], 'scope': 'all'})
        self.assertEqual(full['llm_records'][0]['response'], '단일 플레이 원문')
        import multi
        game = multi.create(3, 7)
        Store(self.f.path).save(game)
        full = self.e.handle('games/export', self.f.op | {'game_id': game['id'], 'scope': 'all'})
        self.assertIn(game['controller']['id'], full['capture']['participants_without_records'])

    def test_engine_gateway_inflight_replay_never_duplicates_provider_call(self):
        started, release = threading.Event(), threading.Event()
        llm = EngineLLMClient(self.client, 'fake', model='gpt-test')
        def network(*args, **kw):
            started.set()
            release.wait(timeout=5)
            return self.response()
        with patch('urllib.request.urlopen', side_effect=network) as request:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: llm.request('s', 'u', call_id='pending'))
                try:
                    self.assertTrue(started.wait(timeout=3))
                    self.assertCode('call_in_progress', lambda: llm.request('s', 'u', call_id='pending'))
                    # While the provider is blocked, engine reads/writes remain available.
                    self.assertEqual(self.archive()['capture']['unfinished_calls'], 1)
                    self.f.call('agent/heartbeat', self.creds)
                finally:
                    release.set()
                future.result(timeout=5)
        self.assertEqual(request.call_count, 1)

    def test_mixed_identity_cannot_read_another_actors_replayed_response(self):
        gm = EngineLLMClient(LocalClient(self.e, self.f.gm), 'fake', model='gpt-test')
        player = EngineLLMClient(self.client, 'fake', model='gpt-test')
        def answer(text):
            return io.BytesIO(json.dumps({'output': [{'content': [{'type': 'output_text', 'text': text}]}]}).encode())
        with patch('urllib.request.urlopen', return_value=answer('GM PRIVATE')):
            gm.request('same', 'same', call_id='same-id')
        with patch('urllib.request.urlopen', return_value=answer('PLAYER OWN')):
            player.request('same', 'same', call_id='same-id')
        mixed = LocalClient(self.e, self.creds | {'controller_id': self.f.gm['controller_id']})
        result = EngineLLMClient(mixed, 'fake', model='gpt-test').request('same', 'same', call_id='same-id')
        self.assertEqual(result[0], 'PLAYER OWN')

    def test_engine_gateway_stops_new_calls_after_pause_or_end(self):
        llm = EngineLLMClient(self.client, 'fake', model='gpt-test')
        self.f.call('games/operator', self.f.op, game_id='case', operation='PAUSE')
        with patch('urllib.request.urlopen') as network:
            self.assertCode('not_running', lambda: llm.request('s', 'u', call_id='paused'))
            network.assert_not_called()
        self.assertEqual(len(self.archive()['llm_records']), 0)
        self.f.call('games/operator', self.f.op, game_id='case', operation='RESUME')
        self.f.call('games/operator', self.f.op, game_id='case', operation='FINISH')
        with patch('urllib.request.urlopen') as network:
            self.assertCode('not_running', lambda: llm.request('s', 'u', call_id='ended'))
            network.assert_not_called()

    def test_engine_gateway_large_provider_output_is_not_truncated_by_upload_limit(self):
        text = '가'*750000
        body = {'output': [{'content': [{'type': 'output_text', 'text': text}]}]}
        llm = EngineLLMClient(self.client, 'fake', model='gpt-test')
        with patch('urllib.request.urlopen', return_value=self.response(body)):
            self.assertEqual(llm.request('s', 'u', call_id='large')[0], text)
        trace = self.archive()['llm_records'][-1]
        self.assertEqual(trace['response']['json'], body)
        self.assertEqual(trace['response']['returned_text'], text)

    def test_native_browser_chat_automatically_archives_both_llm_calls(self):
        from app import Handler, Store
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.external_engine, server.store = self.e, Store(self.f.path)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        original = urllib.request.urlopen
        def post(route, body):
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+route,
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-GM-Client': 'local'})
            with urllib.request.urlopen(request, timeout=10) as result:
                return json.load(result)
        def route(request, *args, **kw):
            if request.full_url.startswith('https://api.openai.com/'):
                instructions = json.loads(request.data)['instructions']
                text = '{"kind":"look"}' if '행동 해석기' in instructions else '내레이션 원문'
                return self.response({'output': [{'content': [{'type': 'output_text', 'text': text}]}]})
            return original(request, *args, **kw)
        try:
            game = post('/start', {'name': '연구자', 'role': 0})
            with patch('urllib.request.urlopen', side_effect=route):
                post('/chat', {'id': game['id'], 'text': '주변을 살핀다', 'key': 'fake-native-key',
                               'model': 'gpt-test', 'connection': 'openai'})
            archive = self.e.handle('games/export', self.f.op | {'game_id': game['id'], 'scope': 'all'})
            self.assertEqual(archive['capture']['call_count'], 2)
            self.assertEqual(archive['capture']['unfinished_calls'], 0)
            self.assertEqual(archive['llm_records'][-1]['response']['returned_text'], '내레이션 원문')
            self.assertNotIn('fake-native-key', json.dumps(archive))
        finally:
            server.shutdown()
            worker.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
