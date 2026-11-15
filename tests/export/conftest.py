from pathlib import Path
import json
import pytest

FIXTURES = Path(__file__).parent / 'fixtures'
SESSIONS = {
    'codex': FIXTURES / 'codex/rollout-simple.jsonl',
    'claude': FIXTURES / 'claude/session-simple.jsonl',
    'cline': FIXTURES / 'cline/task-1',
}


@pytest.fixture
def make_jsonl(tmp_path):
    def write(rows, tail=''):
        path = tmp_path / 'session.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows) + tail, encoding='utf-8')
        return path
    return write


def codex_item(payload, **extra):
    return {'type': 'response_item', 'payload': payload, **extra}


def user(text='hello'):
    return codex_item({'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]})
