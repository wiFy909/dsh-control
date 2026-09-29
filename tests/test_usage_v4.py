"""DSH 0.2 v4 accounting shapes, without private conversation content."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import zstandard

from dsh_control_app.usage import Ledger, TZ

AT = int(datetime(2026, 9, 30, 1, tzinfo=TZ).timestamp() * 1000)
NOW = datetime(2026, 9, 30, 2, tzinfo=TZ)
USAGE = {'inputTokens': 25, 'outputTokens': 20, 'cacheReadTokens': 100,
         'cacheWriteTokens': 0, 'totalTokens': 145}


def event(kind, seq, data):
    return {'type': kind, 'seq': seq, 'time': AT, 'data': data}


def session(name='parent', version=4, seeded=False):
    return [
        {'type': 'session', 'version': version, 'id': name, 'createdAt': AT,
         'isSeeded': seeded, 'delegationDepth': int(name == 'child')},
        event('request/context', 0, {'model': 'deepseek-flash', 'provider': 'deepseek-official'}),
        {**event('assistant/message', 1, {'usage': USAGE, 'stream': [],
                                        'message': {'content': 'PRIVATE'}}),
         'surfaceOp': {'type': 'append'}},
        event('session-log-deepseek/delivery-accepted', 2,
              {'sessionFormatVersion': version, 'throughSeq': 1}),
    ]


def encode(events):
    return ('\n'.join(json.dumps(e) for e in events) + '\n').encode()


class V4UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'sessions'
        self.ledger = Ledger(self.base / 'usage.db', 'test')

    def write(self, name, events, version=4, compressed=False):
        path = self.root / 'project' / name / f'session.v{version}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = encode(events)
        if compressed:
            path = path.with_suffix('.jsonl.zstd')
            data = zstandard.ZstdCompressor().compress(data)
        path.write_bytes(data)
        return path

    def totals(self):
        return self.ledger.summary(NOW)['today']

    def test_compressed_parent_and_child_usage_retries_and_missing_usage(self):
        parent = session()
        # Delivery markers and surface edits do not incur another model charge.
        parent.append(event('tool/result', 3, {'message': {'content': 'PRIVATE'}}))
        child = session('child')
        child.append(event('assistant/attempt', 3, {'stream': [
            {'type': 'chunk', 'chunk': {'type': 'usage', 'usage': USAGE}}]}))
        child.append(event('assistant/attempt', 4, {'stream': []}))
        self.write('parent', parent, compressed=True)
        self.write('child', child, compressed=True)
        for _ in range(2):
            result = self.ledger.scan([self.root])
            self.assertEqual(result['readable_files'], 2)
            self.assertFalse(result['unavailable'])
            self.assertEqual(self.totals()['tokens'], 435)
            self.assertEqual(self.totals()['requests'], 3)
            self.assertEqual(self.totals()['unpriced'], 0)
        self.assertIn('部分请求未返回 usage', ' '.join(result['notes']))
        self.assertNotIn('不支持', ' '.join(result['notes']))
        self.assertNotIn(b'PRIVATE', self.ledger.path.read_bytes())

    def test_upgrade_to_v4_replaces_v3_and_ignores_compression_copy(self):
        self.write('parent', session(version=3), version=3)
        self.ledger.scan([self.root])
        current = session()
        current.append(event('assistant/message', 3, {'usage': USAGE}))
        self.write('parent', current)
        self.write('parent', current, compressed=True)
        for _ in range(2):
            self.ledger.scan([self.root])
            self.assertEqual(self.totals()['tokens'], 290)
            self.assertEqual(self.totals()['requests'], 2)

    def test_v4_seeded_prefix_excluded_and_append_not_double_counted(self):
        events = session('child', seeded=True)
        events.append(event('session/end-seed', 3, {'inherited': True}))
        events.append(event('assistant/message', 4, {'usage': USAGE}))
        path = self.write('child', events)
        self.ledger.scan([self.root])
        self.assertEqual(self.totals()['tokens'], 145)
        with path.open('ab') as f:
            f.write(encode([event('assistant/message', 5, {'usage': USAGE})]))
        with patch.object(self.ledger, 'read', side_effect=AssertionError('full reread')):
            self.ledger.scan([self.root])
        self.assertEqual(self.totals()['tokens'], 290)

    def test_future_format_is_identified_and_not_guessed(self):
        self.write('parent', session(version=5), version=5)
        result = self.ledger.scan([self.root])
        self.assertTrue(result['unavailable'])
        self.assertEqual(self.totals()['tokens'], 0)
        self.assertIn('v5', ' '.join(result['notes']))
        self.assertIn('更新', ' '.join(result['notes']))
