"""Deterministic best-effort redaction over every exported field."""
from collections import Counter
import hashlib
import re
from .model import InputError

SENSITIVE = re.compile(r'(?i)(?:.*[_-](?:key|token|secret|password|passwd)|(?:api[_-]?)?key|token|secret|password|passwd)$')
ASSIGNMENT = re.compile(
    r'''(?ix)(\b(?:[a-z][\w-]*[_-](?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)|API_KEY|PASSWORD|PASSWD)\b["']?\s*[:=]\s*)'''
    r'''("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''')
PATTERNS = [
    ('api-key', re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|AKIA[A-Z0-9]{16})\b')),
    ('bearer', re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')),
    ('email', re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?![\w.-])')),
    ('home', re.compile(r'/(?:home|Users)/[^/\s"\x27<>:]+')),
]
STRICT = [
    ('private-key', re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----')),
    ('url-auth', re.compile(r'(?i)(?:https?|ssh)://[^\s/@]+:[^\s/@]+@')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')),
    ('query-secret', re.compile(r'(?i)[?&](?:token|key|secret|password|access_token|api_key)=[^\s&#"\x27]+')),
    ('absolute-path', re.compile(r'(?<![\w:/~])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*')),
]


class Redactor:
    def __init__(self, mode='default'):
        if mode not in {'default', 'strict', 'off'}:
            raise InputError('invalid redaction mode')
        self.mode = mode
        self.counts = Counter()

    def token(self, kind, value):
        self.counts[kind] += 1
        if kind == 'home':
            return '~'
        digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
        return f'[REDACTED:{kind}:{digest}]'

    def text(self, value):
        if self.mode == 'off':
            return value
        if self.mode == 'strict':
            # Whole multiline keys before any smaller replacements.
            kind, pattern = STRICT[0]
            value = pattern.sub(lambda m: self.token(kind, m[0]), value)
        value = ASSIGNMENT.sub(lambda m: m[1] + self.token('assignment', m[2]), value)
        for kind, pattern in PATTERNS + (STRICT[1:] if self.mode == 'strict' else []):
            value = pattern.sub(lambda m: self.token(kind, m[0]), value)
        return value

    def walk(self, value):
        if self.mode == 'off':
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.walk(x) for x in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                new_key = self.text(key)
                if new_key in result:
                    raise InputError('redaction caused a metadata key collision')
                if SENSITIVE.fullmatch(key) and item is not None:
                    from .model import dumps
                    result[new_key] = self.token('field', item if isinstance(item, str) else dumps(item))
                else:
                    result[new_key] = self.walk(item)
            return result
        return value
