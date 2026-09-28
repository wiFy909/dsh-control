"""Local, deduplicated token ledger; estimates are never official account charges."""
from __future__ import annotations
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import io
import json
import re
import sqlite3
import time
import math
import zstandard

TZ = ZoneInfo('Asia/Shanghai')
PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
HOLIDAY_SOURCE = 'https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm'
HOLIDAY_YEAR = 2026
# State Council 2026 holiday schedule. Make-up weekends stay off-peak under
# DeepSeek's explicit Monday-Friday rule; holiday weekdays are off-peak too.
HOLIDAYS = (
    (date(2026, 1, 1), date(2026, 1, 3)),
    (date(2026, 2, 15), date(2026, 2, 23)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
)
VERIFIED = date(2026, 9, 25)
EXPIRES = date(2026, 10, 24)
EFFECTIVE = date(2026, 8, 17)
PRICE_RULE_ID = 'deepseek-cny-20260817-holidays2026-reviewed-20260925'
UNPRICED_RULE_ID = PRICE_RULE_ID + ':unpriced'
PARSER_REVISION = 'accounting-20260927-1'
RATES = {'deepseek-flash': (.02, 1., 4.), 'deepseek-v4-pro': (.15, 4.5, 13.5)}
ALIASES = {'deepseek-v4-flash': 'deepseek-flash', 'deepseek-v4-flash-vision-exp': 'deepseek-flash'}


def peak(at):
    at = at.astimezone(TZ)
    day = at.date()
    if day.year != HOLIDAY_YEAR:
        raise ValueError('holiday schedule is not available for this year')
    return (at.weekday() < 5 and not any(start <= day <= end for start, end in HOLIDAYS)
            and (9 <= at.hour < 12 or 14 <= at.hour < 18))


def period(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if now.date() > EXPIRES:
        return '价格规则已过期', '金额暂不可可靠估算'
    if now.date() < EFFECTIVE:
        return '价格规则待生效', '—'
    current = peak(now)
    cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24 * 15):
        if peak(cursor) != current:
            remaining = max(0, int((cursor - now).total_seconds()))
            return ('高峰' if current else '空闲'), f'{remaining//3600:02}:{remaining%3600//60:02}:{remaining%60:02}'
        cursor += timedelta(hours=1)
    return ('高峰' if current else '空闲'), '—'


def fee(model, timestamp, usage, provider='deepseek'):
    try:
        at = datetime.fromtimestamp(timestamp / 1000, TZ)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    if provider not in ('deepseek','deepseek-official') or not EFFECTIVE <= at.date() <= EXPIRES:
        return None
    rate = RATES.get(ALIASES.get(model, model))
    if not rate or 'cacheReadTokens' not in usage or usage.get('cacheWriteTokens', 0):
        return None
    values = [usage.get(k) for k in ('cacheReadTokens','inputTokens','outputTokens')]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    return sum(v*r for v,r in zip(values,rate)) * (2 if peak(at) else 1) / 1_000_000


class Ledger:
    """SQLite stores accounting fields only, keyed by installation/session/seq.

    Changed logs are streamed, unchanged files are skipped. Rewrites replace that
    session atomically. Highest format generation wins; inherited fork prefix is
    excluded. A malformed or incomplete log retains the last good observation.
    """
    def __init__(self, path, scope):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scope = scope
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS usage (
                scope TEXT, session TEXT, seq INTEGER, time INTEGER, tokens INTEGER,
                cost REAL, model TEXT, provider TEXT, input_tokens INTEGER,
                cache_read_tokens INTEGER, output_tokens INTEGER, price_rule TEXT,
                legacy_cost REAL, cache_write_tokens INTEGER, PRIMARY KEY(scope,session,seq));
                CREATE TABLE IF NOT EXISTS files (
                scope TEXT, path TEXT, signature TEXT, session TEXT, partial INTEGER,
                PRIMARY KEY(scope,path));
                CREATE TABLE IF NOT EXISTS file_cursors (
                scope TEXT, path TEXT, inode INTEGER, size INTEGER, offset INTEGER,
                prefix_sha256 TEXT, tail_sha256 TEXT, context TEXT,
                PRIMARY KEY(scope,path));''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(usage)')}
            additions = {'provider':'TEXT','input_tokens':'INTEGER','cache_read_tokens':'INTEGER',
                         'output_tokens':'INTEGER','price_rule':'TEXT','legacy_cost':'REAL',
                         'cache_write_tokens':'INTEGER'}
            missing = {key:kind for key,kind in additions.items() if key not in columns}
            if missing:
                backup = self.path.with_suffix('.pre-20260924.bak')
                if not backup.exists():
                    with sqlite3.connect(backup) as target: db.backup(target)
                    backup.chmod(0o600)
                for key,kind in missing.items(): db.execute(f'ALTER TABLE usage ADD COLUMN {key} {kind}')
                db.execute('UPDATE usage SET legacy_cost=cost, cost=NULL, price_rule=? WHERE price_rule IS NULL',
                           ('legacy-unverified',))
        try: self.path.chmod(0o600)
        except OSError: pass
        self.notes = []

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def boundary_hashes(path, size):
        with path.open('rb') as source:
            first=source.read(min(4096,size))
            source.seek(max(0,size-4096))
            last=source.read(min(4096,size))
        return hashlib.sha256(first).hexdigest(),hashlib.sha256(last).hexdigest()

    def scan(self, roots):
        self.notes = []
        groups = {}
        limit = 0
        readable = 0
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            for p in base.glob('*/*/session*.jsonl*'):
                match = re.fullmatch(r'session(?:\.v([1-9][0-9]*))?\.jsonl(?:\.zstd)?', p.name)
                if not match or p.is_symlink():
                    continue
                version = int(match[1] or 0)
                if p.parent not in groups or version > groups[p.parent][0]:
                    groups[p.parent] = (version, p)
                elif version == groups[p.parent][0]:
                    # Never sum alternate compression copies. Choose newer physical copy.
                    if p.stat().st_mtime_ns > groups[p.parent][1].stat().st_mtime_ns:
                        groups[p.parent] = (version,p)
                limit += 1
                if limit >= 10000:
                    self.notes.append('日志扫描达到 10000 文件上限，统计可能不完整')
                    break
        with self.connect() as db:
            for version, path in groups.values():
                if version not in (2,3):
                    self.notes.append('存在尚未支持的历史日志格式')
                    continue
                try:
                    st = path.stat()
                    sig = f'{PARSER_REVISION}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}'
                    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
                    old = db.execute('SELECT signature, partial, session FROM files WHERE scope=? AND path=?', (self.scope,key)).fetchone()
                    if old and old[0] == sig:
                        readable += 1
                        if old[1]: self.notes.append('部分请求未返回 usage，用量为已观测下限')
                        continue
                    cursor=db.execute('''SELECT inode,size,offset,prefix_sha256,tail_sha256,context
                                         FROM file_cursors WHERE scope=? AND path=?''',(self.scope,key)).fetchone()
                    append=False
                    if (cursor and old and old[0].startswith(PARSER_REVISION+':')
                            and path.suffix != '.zstd' and st.st_ino==cursor[0] and st.st_size>cursor[1]):
                        prefix,tail=self.boundary_hashes(path,cursor[1])
                        append=(prefix,tail)==(cursor[3],cursor[4])
                    if append:
                        with path.open('rb') as stream:
                            stream.seek(cursor[2])
                            rows,session,partial=self.parse(stream,version,resume=json.loads(cursor[5]),allow_tail=True)
                        partial=partial or bool(old and old[1])
                    else:
                        rows,session,partial=self.read(path,version,allow_tail=path.suffix!='.zstd')
                    # Do not replace the prior committed generation if source changed mid-read.
                    new = path.stat()
                    if (new.st_size,new.st_mtime_ns) != (st.st_size,st.st_mtime_ns):
                        self.notes.append('日志正在写入，将在下一轮更新')
                        continue
                    with db:
                        if not append:
                            if old and old[2]!=session:
                                db.execute('DELETE FROM usage WHERE scope=? AND session=?',(self.scope,old[2]))
                            db.execute('DELETE FROM usage WHERE scope=? AND session=?',(self.scope,session))
                        db.executemany('''INSERT OR REPLACE INTO usage
                            (scope,session,seq,time,tokens,cost,model,provider,input_tokens,
                             cache_read_tokens,output_tokens,price_rule,cache_write_tokens)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
                        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',(self.scope,key,sig,session,int(partial)))
                        if path.suffix != '.zstd':
                            prefix,tail=self.boundary_hashes(path,st.st_size)
                            db.execute('INSERT OR REPLACE INTO file_cursors VALUES (?,?,?,?,?,?,?,?)',
                                       (self.scope,key,st.st_ino,st.st_size,self._parse_offset,
                                        prefix,tail,json.dumps(self._parse_state)))
                        else:
                            db.execute('DELETE FROM file_cursors WHERE scope=? AND path=?',(self.scope,key))
                    if partial: self.notes.append('部分请求未返回 usage，用量为已观测下限')
                    readable += 1
                except (OSError, ValueError, KeyError, TypeError, zstandard.ZstdError, sqlite3.Error):
                    self.notes.append('部分日志未完整或不可读，保留上次统计')
        if not groups:
            self.notes.append('尚无可读取会话日志')
        self.notes = list(dict.fromkeys(self.notes))
        self.reprice()
        result = self.summary()
        result['readable_files'] = readable
        result['unavailable'] = not readable and not result['week']['requests']
        return result

    def reprice(self):
        """Apply the versioned rule only where raw classified usage is available."""
        with self.connect() as db:
            rows=db.execute('''SELECT scope,session,seq,time,model,provider,input_tokens,
                              cache_read_tokens,output_tokens,cache_write_tokens FROM usage
                              WHERE scope=? AND input_tokens IS NOT NULL
                              AND (price_rule IS NULL OR price_rule NOT IN (?,?))''',
                            (self.scope,PRICE_RULE_ID,UNPRICED_RULE_ID)).fetchall()
            for scope,session,seq,at,model,provider,inp,cache,out,write in rows:
                amount=fee(model,at,{'inputTokens':inp,'cacheReadTokens':cache,'outputTokens':out,
                                     'cacheWriteTokens':write or 0},provider)
                db.execute('UPDATE usage SET cost=?,price_rule=? WHERE scope=? AND session=? AND seq=?',
                           (amount,PRICE_RULE_ID if amount is not None else UNPRICED_RULE_ID,scope,session,seq))

    def read(self, path, version, allow_tail=False):
        with path.open('rb') as raw:
            if path.suffix == '.zstd':
                with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                    return self.parse(io.BufferedReader(stream), version)
            return self.parse(raw, version,allow_tail=allow_tail)

    def parse(self, stream, version, resume=None, allow_tail=False):
        if resume is None:
            first = stream.readline(65537)
            header = json.loads(first)
            if header.get('type') != 'session' or header.get('version') != version or not isinstance(header.get('id'),str):
                raise ValueError('unsupported_header')
            session = hashlib.sha256(header['id'].encode()).hexdigest()
            model,provider,inherited,previous_seq='', '', None, -1
            seeded=bool(header.get('isSeeded'))
            total=len(first)
        else:
            session=resume['session']
            model,provider=resume['model'],resume['provider']
            inherited,previous_seq=resume['inherited'],resume['previous_seq']
            seeded=resume['seeded']
            total=0
        rows=[]
        partial=False
        offset=stream.tell() if resume is not None else total
        while True:
            line = stream.readline(8_000_001)
            if not line: break
            total += len(line)
            if len(line) > 8_000_000 or total > 256_000_000:
                raise ValueError('scan_limit')
            if not line.endswith(b'\n'):
                if allow_tail:
                    partial=True
                    break
                raise ValueError('incomplete_tail')
            offset+=len(line)
            event = json.loads(line)
            if not isinstance(event,dict): raise ValueError('invalid_event')
            kind, data, seq = event.get('type'), event.get('data',{}), event.get('seq')
            if not isinstance(data,dict): raise ValueError('invalid_event_data')
            if type(seq) is not int or seq <= previous_seq:
                raise ValueError('duplicate_sequence')
            previous_seq = seq
            if kind == 'session/end-seed' and data.get('inherited') is True:
                inherited = seq
            if kind == 'request/context':
                model, provider = data.get('model',''), data.get('provider','')
            elif kind == 'request/header':
                config = data.get('header',{}).get('config',{})
                model, provider = config.get('model',model), config.get('provider',provider)
            if kind not in ('assistant/message','assistant/attempt','compaction/summary'):
                continue
            # Seeded history informs routing, but was billed in the parent.
            if seeded and inherited is None:
                continue
            usage = data.get('usage') if kind != 'assistant/attempt' else None
            if usage is None:
                for record in data.get('stream',[]):
                    chunk = record.get('chunk',{})
                    if record.get('type') == 'chunk' and chunk.get('type') == 'usage':
                        usage = chunk.get('usage')
            if not isinstance(usage,dict):
                partial = True
                continue
            fields = ('inputTokens','outputTokens','cacheReadTokens','cacheWriteTokens')
            if 'inputTokens' not in usage or 'outputTokens' not in usage:
                partial = True
                continue
            if 'cacheReadTokens' not in usage and 'totalTokens' not in usage:
                partial = True
            if any(type(usage.get(k,0)) is not int or usage.get(k,0)<0 for k in fields):
                partial = True
                continue
            token = usage.get('totalTokens', sum(usage.get(k,0) for k in fields))
            timestamp = event.get('time')
            if type(token) is not int or token < 0 or type(timestamp) is not int or timestamp < 0:
                partial = True
                continue
            try:
                datetime.fromtimestamp(timestamp / 1000, TZ)
            except (OverflowError, OSError, ValueError):
                partial = True
                continue
            sample_model = data.get('model',model) if kind=='compaction/summary' else model
            sample_provider = data.get('provider',provider) if kind=='compaction/summary' else provider
            if not isinstance(sample_model,str) or not isinstance(sample_provider,str):
                partial=True
                continue
            amount=fee(sample_model,timestamp,usage,sample_provider)
            rows.append((self.scope,session,seq,timestamp,token,amount,sample_model[:100],sample_provider[:100],
                         usage.get('inputTokens'),usage.get('cacheReadTokens'),usage.get('outputTokens'),
                         PRICE_RULE_ID if amount is not None else UNPRICED_RULE_ID,usage.get('cacheWriteTokens',0)))
        if seeded:
            if inherited is None: raise ValueError('missing_inherited_cut')
            rows = [r for r in rows if r[2]>inherited]
        self._parse_offset=offset
        self._parse_state={'session':session,'model':model,'provider':provider,
                           'inherited':inherited,'previous_seq':previous_seq,'seeded':seeded}
        return rows, session, partial

    def summary(self, now=None):
        now = now or datetime.now(TZ)
        today = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=6)
        with self.connect() as db:
            rows = db.execute('SELECT time,tokens,cost FROM usage WHERE scope=? AND time>=? AND time<=?',
                              (self.scope,int(start.timestamp()*1000),int(now.timestamp()*1000))).fetchall()
        def aggregate(items):
            return {'tokens':sum(r[1] for r in items), 'cost':math.fsum(r[2] or 0 for r in items),
                    'unpriced':sum(r[2] is None for r in items), 'requests':len(items)}
        result = {'today':aggregate([r for r in rows if r[0]>=today.timestamp()*1000]), 'week':aggregate(rows),
                  'notes':list(self.notes), 'scope':'当前 DSH HOME · 本地已观测用量', 'days':len({datetime.fromtimestamp(r[0]/1000,TZ).date() for r in rows})}
        return result
