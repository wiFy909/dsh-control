"""DeepSeek CNY token prices, with time-bounded observations from the official page."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
PRICE_HOST = 'api-docs.deepseek.com'
PRICE_PATH = '/zh-cn/quick_start/pricing/'
PRICE_SOURCE = 'https://' + PRICE_HOST + PRICE_PATH
EFFECTIVE = datetime(2026, 8, 17, tzinfo=TZ)
FLASH_EFFECTIVE = datetime(2026, 9, 10, 12, tzinfo=TZ)
BUNDLED_CHECK = datetime(2026, 11, 23, tzinfo=TZ)
FRESH_DAYS = 30
# 国办发明电〔2025〕7号、〔2026〕10号. Make-up weekends stay off-peak.
HOLIDAYS = {
    2026: ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5), (6, 19, 21),
           (9, 25, 27), (10, 1, 7)),
    2027: ((1, 1, 3), (2, 5, 13), (4, 3, 5), (5, 1, 5), (6, 9, 9),
           (9, 15, 15), (10, 1, 7)),
}
FLASH_ALIASES = ('deepseek-v4-flash', 'deepseek-v4-flash-vision-exp')
BUCKETS = ('百万tokens输入（缓存命中）', '百万tokens输入（缓存未命中）', '百万tokens输出')
PERIODS = ('空闲时段', '高峰时段')
MODEL = re.compile(r'[a-z0-9][a-z0-9._-]{0,79}')
PRICE = re.compile(r'(\d{1,6}(?:\.\d{1,8})?)元')
TIME = r'(?:[01]?\d|2[0-3]):[0-5]\d|24:00'
HOURS = re.compile(r'北京时间周一至周五（不含中国法定节假日）(.+?)为高峰时段；其余时段，包括周末及中国法定节假日全天均为空闲时段。')
BUNDLED_TERMS = {
    'hours': [[540, 720], [840, 1080]],
    'rates': {'deepseek-flash': [[.02, 1., 4.], [.04, 2., 8.]],
              'deepseek-v4-pro': [[.15, 4.5, 13.5], [.30, 9., 27.]]},
    'aliases': dict.fromkeys(FLASH_ALIASES, 'deepseek-flash'),
}
MAX_PAGE = 512 * 1024


class PricingPage(HTMLParser):
    """Collect rows and paragraphs, keeping the model table's cell boundaries."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.paragraphs = []
        self.row = []
        self.parts = None
        self.in_note = False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th', 'p'):
            self.parts = []
        elif tag == 'sup':
            self.in_note = True

    def handle_endtag(self, tag):
        if tag == 'sup':
            self.in_note = False
        elif tag in ('td', 'th', 'p') and self.parts is not None:
            value = re.sub(r'\s+', '', ''.join(self.parts))
            if tag == 'p':
                self.paragraphs.append(value)
            else:
                self.row.append(value)
            self.parts = None
        elif tag == 'tr':
            self.rows.append(self.row)

    def handle_data(self, data):
        if self.parts is not None and not self.in_note:
            self.parts.append(data)


def parse_pricing(html):
    page = PricingPage()
    page.feed(html)
    page.close()
    headings = [i for i, row in enumerate(page.rows) if row and row[0] == '模型']
    if len(headings) != 1:
        raise ValueError('无法识别官网模型价格表')
    heading = headings[0]
    models = page.rows[heading][1:]
    if not models or len(set(models)) != len(models) or any(not MODEL.fullmatch(m) for m in models):
        raise ValueError('无法核实官网模型列')
    rates = {model: [[], []] for model in models}
    for bucket_index, bucket in enumerate(BUCKETS):
        starts = [i for i, row in enumerate(page.rows) if i > heading and bucket in row]
        if len(starts) != 1:
            raise ValueError('官网 Token 计费项已变化')
        first = starts[0]
        prefix = ['价格', bucket] if bucket_index == 0 else [bucket]
        for row, label, index in ((page.rows[first], prefix + [PERIODS[0]], 0),
                                  (page.rows[first + 1] if first + 1 < len(page.rows) else [],
                                   [PERIODS[1]], 1)):
            prices = [PRICE.fullmatch(value) for value in row[len(label):]]
            if row[:len(label)] != label or len(prices) != len(models) or not all(prices):
                raise ValueError('官网价格的单位、列或时段已变化')
            for model, price in zip(models, prices):
                rates[model][index].append(float(price[1]))

    schedule = HOURS.search(''.join(page.paragraphs))
    if not schedule:
        raise ValueError('无法核实官网峰谷时段与节假日政策')
    hours = []
    for interval in schedule[1].split('、'):
        match = re.fullmatch(rf'({TIME})[-–—]({TIME})', interval)
        if not match:
            raise ValueError('无法核实官网峰谷时段')
        starts_at, ends_at = [int(h) * 60 + int(m) for h, m in (t.split(':') for t in match.groups())]
        if starts_at >= ends_at or (hours and starts_at < hours[-1][1]):
            raise ValueError('官网峰谷时段重叠或无效')
        hours.append([starts_at, ends_at])
    aliases = {}
    for paragraph in page.paragraphs:
        alias_note = re.search(r'模型名请使用([a-z0-9.-]+)。旧模型名([a-z0-9.、-]+)仍可调用', paragraph)
        if alias_note and alias_note[1] in rates:
            aliases.update({alias: alias_note[1] for alias in alias_note[2].split('、') if alias not in rates})
    return {'rates': rates, 'hours': hours, 'aliases': aliases}


def official_terms():
    conn = http.client.HTTPSConnection(PRICE_HOST, timeout=8)
    try:
        conn.request('GET', PRICE_PATH, headers={'Accept': 'text/html', 'Cache-Control': 'no-cache'})
        reply = conn.getresponse()
        if reply.status != 200:
            raise ValueError(f'官网返回 HTTP {reply.status}')
        raw = reply.read(MAX_PAGE + 1)
        if len(raw) > MAX_PAGE:
            raise ValueError('官网价格页超过读取限制')
        return parse_pricing(raw.decode('utf-8'))
    finally:
        conn.close()


@dataclass(frozen=True)
class PriceWindow:
    start: datetime
    end: datetime
    terms: dict


def valid_until(checked):
    return checked.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=FRESH_DAYS)


class PriceBook:
    def __init__(self, observations=()):
        windows = []
        starts_at, checked, terms = EFFECTIVE, BUNDLED_CHECK, BUNDLED_TERMS
        for observed_at, observed_terms in observations:
            if observed_at < checked:
                continue
            end = valid_until(checked)
            if observed_terms != terms:
                # A current table cannot date a past change. Stop trusting the old
                # projection at its last observation and leave any gap unpriced.
                windows.append(PriceWindow(starts_at, min(end, checked + timedelta(milliseconds=1)), terms))
                starts_at = observed_at
            elif observed_at > end:
                windows.append(PriceWindow(starts_at, end, terms))
                starts_at = observed_at
            checked, terms = observed_at, observed_terms
        windows.append(PriceWindow(starts_at, valid_until(checked), terms))
        self.windows = tuple(windows)
        payload = [(row.start.isoformat(), row.end.isoformat(), row.terms) for row in self.windows]
        self.revision = 'deepseek-cny-v2-' + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]

    def terms_at(self, at):
        if at.year in HOLIDAYS:
            for row in reversed(self.windows):
                if row.start <= at < row.end:
                    return row.terms
        return None


BUNDLED_PRICES = PriceBook()


def refresh_prices(base):
    checked = datetime.now(TZ)
    terms = official_terms()
    path = Path(base) / 'pricing.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS observations (checked INTEGER PRIMARY KEY, terms TEXT NOT NULL)')
        db.execute('INSERT INTO observations VALUES (?, ?) ON CONFLICT(checked) DO UPDATE SET terms=excluded.terms',
                   (int(checked.timestamp() * 1000), json.dumps(terms, sort_keys=True)))
    return checked


def load_prices(base):
    path = Path(base) / 'pricing.sqlite3'
    if not path.is_file():
        return BUNDLED_PRICES
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            rows = db.execute('SELECT checked, terms FROM observations ORDER BY checked').fetchall()
        return PriceBook((datetime.fromtimestamp(checked / 1000, TZ), json.loads(terms)) for checked, terms in rows)
    except (sqlite3.Error, ValueError, OSError):
        return BUNDLED_PRICES
