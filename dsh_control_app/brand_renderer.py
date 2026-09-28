"""Shared terminal brand renderer; runtime platform never selects geometry."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys

from PIL import Image
from rich.text import Text

ASSETS = Path(__file__).with_name('assets') / 'brand'
MODES = ('auto', 'braille', 'block', 'ascii')
BITS = ((0, 1, 2, 6), (3, 4, 5, 7))
QUADRANTS = ' ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█'
# Maximum physical width in cell-width units; SVG viewBox is square.
LIMITS = {'small': 18, 'medium': 22, 'hero': 64}


@dataclass(frozen=True)
class Capabilities:
    runtime_platform: str
    terminal: str
    unicode: bool
    braille: bool
    block: bool
    cell_aspect: float  # cell width / cell height
    aspect_source: str
    evidence: str = 'environment/encoding heuristic; font coverage unverified'


def detect(env=None, system=None, release=None, encoding=None):
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    release = platform.release() if release is None else release
    runtime = ('windows-native' if system == 'Windows' else
               'wsl' if system == 'Linux' and (env.get('WSL_DISTRO_NAME') or 'microsoft' in release.lower()) else
               'macos' if system == 'Darwin' else 'linux' if system == 'Linux' else 'unknown')
    term, program = env.get('TERM',''), env.get('TERM_PROGRAM','').lower()
    # No OS x renderer table, and no renderer-specific assets.
    terminal = ('Windows Terminal' if env.get('WT_SESSION') else
                'Ghostty' if program == 'ghostty' or env.get('GHOSTTY_RESOURCES_DIR') else
                'iTerm2' if program == 'iterm.app' else
                'kitty' if env.get('KITTY_WINDOW_ID') or term == 'xterm-kitty' else
                'WezTerm' if program == 'wezterm' or env.get('WEZTERM_PANE') else
                'Konsole' if env.get('KONSOLE_VERSION') else
                'Terminal.app' if program == 'apple_terminal' else
                'GNOME Terminal/VTE' if env.get('VTE_VERSION') else 'unknown')
    encoding = encoding or getattr(sys.stdout,'encoding',None) or 'ascii'
    def encodable(value):
        try:
            value.encode(encoding); return True
        except (UnicodeError, LookupError):
            return False
    unicode = encodable('鲸') and term != 'dumb'
    known = terminal != 'unknown' and term != 'dumb'
    braille = known and unicode and encodable('⣿')
    block = known and encodable(QUADRANTS)
    aspect, source = cell_aspect(env)
    return Capabilities(runtime,terminal,unicode,braille,block,aspect,source)


def cell_aspect(env):
    override = env.get('DSHCTL_CELL_ASPECT')
    if override is not None:
        try: value = float(override)
        except ValueError: raise ValueError('DSHCTL_CELL_ASPECT must be a number between 0.25 and 1.0') from None
        if not math.isfinite(value) or not .25 <= value <= 1:
            raise ValueError('DSHCTL_CELL_ASPECT must be between 0.25 and 1.0')
        return value, 'configuration'
    # Passive ioctl only: no cursor queries, no stdin consumption, no timeouts.
    if os.name == 'posix' and env is os.environ:
        try:
            import fcntl
            import struct
            import termios
            rows, cols, xp, yp = struct.unpack('HHHH',fcntl.ioctl(sys.stdout.fileno(),termios.TIOCGWINSZ,b'\0'*8))
            if all((rows,cols,xp,yp)):
                ratio = (xp/cols)/(yp/rows)
                if .25 <= ratio <= 1: return ratio, 'TIOCGWINSZ pixels/cells'
        except (OSError,ValueError,AttributeError):
            pass
    return .5, 'assumed 1:2; override for your font using DSHCTL_CELL_ASPECT'


def choose_mode(capabilities, requested='auto'):
    if requested not in MODES: raise ValueError(f'Invalid glyph mode: {requested}')
    if requested != 'auto': return requested
    return 'braille' if capabilities.braille else 'block' if capabilities.block else 'ascii'


@dataclass(frozen=True)
class Plan:
    width: int
    rows: int
    lod: str
    mode: str
    cell_aspect: float
    draw_columns: int
    draw_rows: int


def plan(width, rows, *, hero=False, capabilities=None, mode='auto'):
    cap = capabilities or detect()
    width, rows = max(1,int(width)), max(1,int(rows))
    available = min(max(1,width-2), max(1,rows-1)/cap.cell_aspect)
    lod = 'hero' if hero else 'medium' if available >= 20 else 'small'
    size = min(LIMITS[lod], available)
    return Plan(width,rows,lod,choose_mode(cap,mode),cap.cell_aspect,
                max(1,round(size)),max(1,round(size*cap.cell_aspect)))


def welcome_size(columns, rows):
    return max(1,min(96,columns-4)),max(1,min(36,rows-10))


def welcome_caption_columns(width, rows, unicode=True):
    """Left margin + caption + gap; omit only when the viewport cannot fit both."""
    caption_width=12 if unicode else 21
    return caption_width+4 if width>=caption_width+8 and rows>=3 else 0


@lru_cache(maxsize=3)
def mask(lod):
    with Image.open(ASSETS/f'{lod}.png') as image: return image.convert('L')


@lru_cache(maxsize=128)
def _plain(p):
    sx,sy = {'braille':(2,4),'block':(2,2),'ascii':(1,1)}[p.mode]
    w,h = p.width*sx,p.rows*sy
    # Fit in physical coordinates before mapping into the glyph's sample grid.
    # Continuous scale is shared by X/Y; only final sample rounding differs.
    size = min(LIMITS[p.lod],max(1,p.width-2),max(1,p.rows-1)/p.cell_aspect)
    iw,ih = max(1,round(size*sx)),max(1,round(size*p.cell_aspect*sy))
    resized = mask(p.lod).resize((iw,ih),Image.Resampling.BOX)
    canvas = Image.new('L',(w,h)); canvas.paste(resized,((w-iw)//2,(h-ih)//2))
    # Small LOD uses a coverage threshold that keeps narrow source features;
    # no hand-authored eye, tail, contours, or terminal-specific geometry.
    threshold = {'small':90,'medium':110,'hero':128}[p.lod]
    lines=[]
    for y in range(p.rows):
        line=[]
        for x in range(p.width):
            if p.mode == 'ascii':
                v=canvas.getpixel((x,y)); line.append(' .:+#@'[min(5,round(v/255*5))])
            else:
                bits=0
                for dy in range(sy):
                    for dx in range(sx):
                        if canvas.getpixel((x*sx+dx,y*sy+dy)) >= threshold:
                            bits |= 1 << (BITS[dx][dy] if p.mode=='braille' else dy*2+dx)
                line.append((chr(0x2800+bits) if bits else ' ') if p.mode=='braille' else QUADRANTS[bits])
        lines.append(''.join(line))
    return '\n'.join(lines)


def render(p):
    return Text(_plain(p),style='#89afd2',no_wrap=True,overflow='crop')


def diagnostics(columns=None, rows=None, mode='auto'):
    cap=detect(); size=shutil.get_terminal_size((80,24))
    columns=columns or size.columns; rows=rows or size.lines
    w,h=welcome_size(columns,rows)
    hero=plan(w-welcome_caption_columns(w,h,cap.unicode),h,hero=True,capabilities=cap,mode=mode)
    # Dashboard allocation follows current CSS; live widgets use content_size.
    nav_width=19 if columns<110 else 20 if columns<125 or rows<48 else min(32,int(min(columns,160)*.16)-2)
    nav_rows=(4 if rows<30 else min(7 if columns<110 else 10,max(4,rows-20)) if rows<=32
              else min(7,max(3,rows-27)) if columns<110 else min(13,max(3,rows-27)) if rows<48 else 13)
    small=plan(nav_width,nav_rows,capabilities=cap,mode=mode)
    return {**asdict(cap),'columns':columns,'rows':rows,'requested_mode':mode,
            'glyph_mode':hero.mode,'whale_lod':hero.lod,
            'welcome':asdict(hero),'dashboard_estimate':asdict(small),
            'note':'dashboard estimate; actual widget content_size is authoritative; ASCII applies to brand artwork only'}
