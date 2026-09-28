# DEPRECATED: historical experiment only; not used by ControlApp or onboarding.
"""Deterministic particle field shared by terminal text and optional pixel views.

The approved HTML in design/tui-refined/index.html supplies the geometry and
motion rules: signed outline field, inner detail, rightward flow, pointer bend.
No static filled silhouette is drawn into the visible frame.
"""
from __future__ import annotations
from importlib.resources import files
from functools import lru_cache
import math
import random
import re
from PIL import Image, ImageDraw


def contours():
    svg = files('dsh_control_app').joinpath('assets/deepseek-whale.svg').read_text()
    path = re.search(r'\bd="([^"]+)"', svg)[1]
    tokens = re.findall(r'[MCVZ]|-?\d*\.?\d+(?:[eE][-+]?\d+)?', path)
    groups, points, pos, command = [], [], (0., 0.), None
    i = 0
    while i < len(tokens):
        if tokens[i] in ('M', 'C', 'V', 'Z'):
            command = tokens[i]
            i += 1
        if command == 'Z':
            if points:
                groups.append(points)
                points = []
            command = None
        elif command == 'M':
            pos = tuple(map(float, tokens[i:i + 2]))
            i += 2
            points.append(pos)
        elif command == 'V':
            pos = (pos[0], float(tokens[i]))
            i += 1
            points.append(pos)
        elif command == 'C':
            a, b, c, d, e, f = map(float, tokens[i:i + 6])
            i += 6
            for step in range(1, 13):
                t = step / 12
                u = 1 - t
                points.append((u**3*pos[0] + 3*u*u*t*a + 3*u*t*t*c + t**3*e,
                               u**3*pos[1] + 3*u*u*t*b + 3*u*t*t*d + t**3*f))
            pos = (e, f)
        else:
            raise ValueError('unsupported_whale_path')
    return groups


def inside(x, y, polygon):
    result = False
    j = len(polygon) - 1
    for i, (a, b) in enumerate(polygon):
        c, d = polygon[j]
        if (b > y) != (d > y) and x < (c-a)*(y-b)/(d-b)+a:
            result = not result
        j = i
    return result


def distance_field(seed, width, height):
    distance = [0. if value else 1e6 for value in seed]
    for y in range(height):
        for x in range(width):
            i = y*width+x
            if x: distance[i] = min(distance[i], distance[i-1]+1)
            if y: distance[i] = min(distance[i], distance[i-width]+1)
            if x and y: distance[i] = min(distance[i], distance[i-width-1]+1.414)
    for y in range(height-1, -1, -1):
        for x in range(width-1, -1, -1):
            i = y*width+x
            if x+1 < width: distance[i] = min(distance[i], distance[i+1]+1)
            if y+1 < height: distance[i] = min(distance[i], distance[i+width]+1)
            if x+1 < width and y+1 < height: distance[i] = min(distance[i], distance[i+width+1]+1.414)
    return distance


@lru_cache(maxsize=8)
def geometry(width, height, svg_text):
    """Rasterize the silhouette and the actual detail strokes once per asset/size."""
    scale = min(width*.78/24, height*.70/18)
    left, top = (width-24*scale)/2, (height-18*scale)/2
    polygons = contours()
    def points(group):
        return [(round(left+x*scale), round(top+y*scale)) for x,y in group]
    shape = Image.new('1',(width,height))
    detail = Image.new('1',(width,height))
    ImageDraw.Draw(shape).polygon(points(polygons[0]),fill=1)
    detail_pen = ImageDraw.Draw(detail)
    for group in polygons[1:]:
        detail_pen.line(points(group),fill=1,width=1,joint='curve')
    outer = tuple(bool(v) for v in shape.getdata())
    inner = tuple(bool(v) for v in detail.getdata())
    edge = []
    for y in range(height):
        for x in range(width):
            i=y*width+x
            edge.append(any(outer[i] != outer[ny*width+nx]
                            for nx,ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1))
                            if 0<=nx<width and 0<=ny<height))
    return outer, tuple(distance_field(edge,width,height)), tuple(distance_field(inner,width,height))


class ParticleField:
    def __init__(self, width, height, seed=42):
        self.width, self.height = width, height
        self.time = 0.
        self.pointer = None
        self.pointer_power = 0.
        rng = random.Random(seed)
        scale = min(width*.78/24, height*.70/18)
        self.unit = max(.5, scale/4.4)
        left, top = (width-24*scale)/2, (height-18*scale)/2
        svg_text=files('dsh_control_app').joinpath('assets/deepseek-whale.svg').read_text()
        self.outer,self.signed,self.inner=geometry(width,height,svg_text)
        spacing = max(1.6, min(4.0, 2.7*self.unit))
        self.dots = []
        y = -spacing
        while y < height+spacing:
            x = -spacing
            while x < width+spacing:
                jitter = spacing*.35
                self.dots.append([x+rng.uniform(-jitter,jitter), y+rng.uniform(-jitter,jitter),
                                  y, 0., rng.random()])
                x += spacing
            y += spacing

    def sample(self, field, x, y):
        ix = min(self.width-1, max(0, round(x)))
        iy = min(self.height-1, max(0, round(y)))
        return field[iy*self.width+ix]

    def frame(self, dt=0., pointer=None):
        dt = max(0., min(.5, dt))
        self.time += dt
        self.pointer = pointer
        self.pointer_power += ((1. if self.pointer else 0.)-self.pointer_power)*(1-math.exp(-dt*10))
        points = []
        u = self.unit
        for dot in self.dots:
            x,y,home_y,vy,seed = dot
            distance = self.sample(self.signed,x,home_y)
            near = math.exp(-distance/max(1, 11*u))
            gradient = self.sample(self.signed,x,home_y+u)-self.sample(self.signed,x,home_y-u)
            influence = 0.
            push = 0.
            if self.pointer and self.pointer_power > .01:
                px,py = self.pointer
                dist = math.hypot(x-px,home_y-py)
                influence = self.pointer_power*max(0., 1-dist/max(1.,40*u))
                push = (home_y-py)/max(1.,dist)*influence*influence*19*u
            x += dt*12*u*(1+near*.55+influence*.35)
            if x > self.width+5*u: x = -5*u; y = home_y; vy = 0.
            target = home_y + (1 if gradient>0 else -1 if gradient<0 else 0)*near*8*u + push
            if dt:
                # Keep the full elapsed time at 8 Hz as well as 12 Hz.
                remaining = dt
                while remaining > 0:
                    substep = min(remaining, 1/60)
                    step = substep*60
                    vy = (vy+(target-y)*.045*step)*(.78**step)
                    y += vy*step
                    remaining -= substep
            dot[:] = [x,y,home_y,vy,seed]
            if not (0 <= x < self.width and 0 <= y < self.height): continue
            i = min(self.height-1,int(y))*self.width+min(self.width-1,int(x))
            signed = -self.signed[i] if self.outer[i] else self.signed[i]
            edge = math.exp(-abs(signed)/max(1.,2.6*u))
            detail = math.exp(-self.inner[i]/max(1.,1.15*u)) if signed < 0 else 0.
            fade = min(x/max(1.,9*u),(self.width-x)/max(1.,9*u),y/max(1.,9*u),
                       (self.height-y)/max(1.,9*u),1.)
            alpha = min(.95,((.16+edge*.64) if signed >= 0 else detail*.87)*fade + influence*.3*fade)
            if alpha > .08:
                points.append((x,y,alpha,edge,detail,influence))
        return points
