"""Offline canonical SVG compiler. Supports this pinned SVG's M/C/Z grammar only.

No traced control points: all cubics come verbatim from canonical.svg. Nonzero
scan conversion preserves winding/hole semantics. Each LOD is sampled directly
from the curves, never resized from another LOD. Pillow is already a dependency.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / 'dsh_control_app/assets/brand'
SIZES = {'small': 96, 'medium': 192, 'hero': 384}


def curves(source, resolution):
    root = ET.fromstring(source)
    if root.attrib.get('viewBox') != '0 0 50 50':
        raise ValueError('Unexpected canonical viewBox')
    paths = list(root)
    if len(paths) != 1 or paths[0].attrib.get('fill-rule') != 'nonzero':
        raise ValueError('Unexpected canonical structure')
    path = paths[0].attrib['d']
    if set(re.findall('[a-zA-Z]', path)) - set('MCZ'):
        raise ValueError('Unsupported SVG command; use a full SVG compiler for new sources')
    tokens = re.findall(r'[MCZ]|-?\d*\.?\d+', path)
    groups, points, i = [], [], 0
    while i < len(tokens):
        command = tokens[i]; i += 1
        if command == 'M':
            pos = tuple(float(v) for v in tokens[i:i+2]); i += 2
            points = [pos]
        elif command == 'C':
            values = list(map(float, tokens[i:i+6])); i += 6
            a, b, end = values[:2], values[2:4], values[4:]
            # Bound segment length in destination supersamples, including bends.
            length = sum(math.dist(p, q) for p, q in ((pos,a),(a,b),(b,end)))
            steps = max(2, math.ceil(length * resolution / 50 * 2))
            for step in range(1, steps+1):
                t = step/steps; u = 1-t
                points.append(tuple(u**3*pos[k]+3*u*u*t*a[k]+3*u*t*t*b[k]+t**3*end[k] for k in (0,1)))
            pos = end
        elif command == 'Z':
            groups.append(points); points = []
        else:
            raise ValueError('Malformed canonical path')
    return groups


def raster(source, size):
    sampling = 4
    n = size * sampling
    groups = curves(source, size)
    edges = []
    for group in groups:
        for a, b in zip(group, group[1:]+group[:1]):
            edges.append(tuple(v*n/50 for v in (*a,*b)))
    image = Image.new('L', (n,n))
    pixels = image.load()
    for y in range(n):
        scan = y+.5
        intersections = []
        for x1,y1,x2,y2 in edges:
            if min(y1,y2) <= scan < max(y1,y2):
                intersections.append((x1+(scan-y1)*(x2-x1)/(y2-y1), 1 if y2>y1 else -1))
        intersections.sort()
        winding = 0; left = 0
        for x, delta in intersections:
            if winding:
                for ix in range(max(0,math.ceil(left-.5)), min(n,math.ceil(x-.5))):
                    pixels[ix,y] = 255
            winding += delta; left = x
    return image.resize((size,size),Image.Resampling.BOX)


def build(destination=ROOT):
    destination.mkdir(parents=True,exist_ok=True)
    source = (ROOT/'canonical.svg').read_bytes()
    metadata = {'schema':1,'source_sha256':hashlib.sha256(source).hexdigest(),
                'compiler':'M/C/Z nonzero scanline, 4x area coverage',
                'lods':{}}
    for lod,size in SIZES.items():
        image = raster(source,size)
        path = destination/f'{lod}.png'; image.save(path)
        metadata['lods'][lod] = {'pixels':size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'sampling':'independent destination-space cubic flattening and area coverage'}
    (destination/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')

if __name__ == '__main__':
    build()
