"""Readable, verbatim client outputs; never invent narration from action IDs."""
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def natural_text(raw):
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(parsed, dict):
        return raw
    texts = []
    for key in ('consideration', 'narration', 'utterance', 'dialogue'):
        if isinstance(parsed.get(key), str):
            texts.append(parsed[key])
    for statement in parsed.get('statements', []) if isinstance(parsed.get('statements'), list) else []:
        if isinstance(statement, dict) and isinstance(statement.get('text'), str):
            texts.append(statement['text'])
    return '\n'.join(texts)


class TranscriptClient:
    def __init__(self, client, directory, agent_id, role):
        self.client, self.directory = client, Path(directory)
        self.agent_id, self.role = agent_id, role
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, *args, **kwargs):
        self.calls += 1
        observation = self.role.call('agent/observe')
        header = (f"[cycle {observation['cycle_id']} / {observation['phase']} / "
                  f"{self.agent_id} / call {self.calls}]")
        raw, usage = self.client.request(*args, **kwargs)
        text = natural_text(raw)
        self.directory.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            for filename, content in [('llm-raw.txt', raw), ('llm-output.txt', text)]:
                with (self.directory / filename).open('a', encoding='utf-8') as stream:
                    stream.write(header + '\n' + (content or '[자연어 필드 없음: llm-raw.txt 참조]') + '\n\n')
                    stream.flush()
        return raw, usage
