#!/usr/bin/env python3
"""Validate release inputs and uploaded assets before making a draft public."""
import argparse
import hashlib
import json
from pathlib import Path
import re


ASSETS = {f'dsh-control-{platform}.zip' for platform in ('windows', 'macos', 'linux')}


def check(root, tag=None, published=None):
    version = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"',
                        (root / 'pyproject.toml').read_text(), re.M)
    if not version:
        raise ValueError('Expected a stable version in pyproject.toml')
    expected = 'v' + version[1]
    if tag is not None and tag != expected:
        raise ValueError('Release tag does not match the package version')
    if (root / 'assets/RELEASE_NOTES.md').read_text().splitlines()[0] != '# ' + expected:
        raise ValueError('Release notes must describe the current version')
    for name in ('install.ps1', 'install.sh'):
        refs = re.findall(r'releases/(?:tags|download)/(v[0-9.]+)', (root / name).read_text())
        if not refs or set(refs) != {expected}:
            raise ValueError(f'{name} must download the current release')
    if {p.name for p in (root / 'dist').iterdir()} != ASSETS:
        raise ValueError('Release output must contain exactly the three platform ZIPs')
    hashes = json.loads((root / '.release-governor/build-artifacts.json').read_text())
    if set(hashes) != ASSETS:
        raise ValueError('Build hash records must cover exactly the release assets')
    for name, digest in hashes.items():
        if hashlib.sha256((root / 'dist' / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Build hash mismatch: {name}')
    if published is not None:
        assets = published['assets']
        if published['tag_name'] != expected or len(assets) != 3 or {a['name'] for a in assets} != ASSETS:
            raise ValueError('Uploaded release tag or assets do not match the build')
        for asset in assets:
            if asset['state'] != 'uploaded' or asset.get('digest') != 'sha256:' + hashes[asset['name']]:
                raise ValueError(f'Uploaded hash mismatch: {asset["name"]}')
    return expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    parser.add_argument('--published-assets', type=Path)
    args = parser.parse_args()
    data = json.loads(args.published_assets.read_text()) if args.published_assets else None
    print('Release checks passed: ' + check(Path(__file__).resolve().parents[1], args.tag, data))
