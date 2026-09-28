#!/usr/bin/env python3
"""Prepare an immutable DSH Control WSL release, then switch one user symlink.

This installs Control only. It never touches the DSH service or its HOME.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid
import fcntl

PARTS = ('core','dsh_control_app','pyproject.toml','requirements.lock','README.md','LICENSE')


def source_files(source):
    for name in PARTS:
        part=source/name
        if part.is_symlink(): raise RuntimeError('source_link_not_allowed')
        if part.is_dir():
            for path in sorted(part.rglob('*')):
                if path.is_symlink(): raise RuntimeError('source_link_not_allowed')
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    yield path.relative_to(source)
        elif part.is_file():
            yield Path(name)
        else:
            raise RuntimeError('missing_source:'+name)


def fingerprint(source):
    digest=hashlib.sha256()
    for rel in source_files(source):
        digest.update(rel.as_posix().encode()+b'\0')
        digest.update(hashlib.sha256((source/rel).read_bytes()).digest())
    return digest.hexdigest()


def switch_link(link,target):
    temporary=link.with_name('.'+link.name+'.'+uuid.uuid4().hex)
    temporary.symlink_to(target,target_is_directory=True)
    os.replace(temporary,link)


def prepare(source,root,offline=False,plan=False):
    root=Path(root).expanduser().resolve()
    if plan:
        return _prepare_locked(source,root,offline,True)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Serialize only Control's own release tree. The lock is never in DSH HOME.
    with (root/'prepare.lock').open('a+b') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        return _prepare_locked(source,root,offline,False)


def _prepare_locked(source,root,offline=False,plan=False):
    source=Path(source).resolve();root=Path(root).expanduser().resolve()
    digest=fingerprint(source)
    release=root/'releases'/digest[:20]
    if plan:
        return {'release':str(release),'fingerprint':digest,'offline':offline,'exists':release.exists()}
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    (root/'releases').mkdir(exist_ok=True)
    stamp={'purpose':'dsh-control-tui-release','source_sha256':digest,
           'lock_sha256':hashlib.sha256((source/'requirements.lock').read_bytes()).hexdigest(),
           'python':f'{sys.version_info.major}.{sys.version_info.minor}',
           'platform':sys.platform,'machine':platform.machine()}
    marker=release/'INSTALL.json'
    if release.exists():
        try:
            complete=(marker.is_file() and json.loads(marker.read_text())==stamp and
                      (release/'.venv/bin/python').exists())
        except (OSError,ValueError):
            complete=False
        if not complete:
            # Never remove a release referenced by either rollback link. A failed
            # attempt at this exact fingerprint is the only candidate we rebuild.
            for name in ('current','previous'):
                link=root/name
                if link.is_symlink() and link.resolve()==release:
                    raise RuntimeError('referenced_release_incomplete')
            if release.is_symlink() or not release.is_dir() or release.parent!=root/'releases':
                raise RuntimeError('unrecognized_release')
            shutil.rmtree(release)
    if not release.exists():
        # The venv has absolute interpreter paths; prepare in its final directory,
        # then expose it only after every check and the receipt have succeeded.
        stage=release
        stage.mkdir(mode=0o700)
        for rel in source_files(source):
            dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/rel,dest)
        python=stage/'.venv/bin/python'
        subprocess.run([sys.executable,'-m','venv',str(stage/'.venv')],check=True)
        wheels=source/'wheelhouse'
        if offline and not wheels.is_dir(): raise RuntimeError('offline_wheelhouse_missing')
        args=['--no-index','--find-links',str(wheels)] if wheels.is_dir() else []
        command=[str(python),'-m','pip','install',*args,'--require-hashes','-r',str(stage/'requirements.lock')]
        first=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        if first.returncode:
            if offline or not wheels.is_dir(): raise RuntimeError('locked_dependencies_unavailable')
            subprocess.run([str(python),'-m','pip','install','--timeout','15','--retries','1',
                            '--require-hashes','-r',str(stage/'requirements.lock')],check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(python),'-m','pip','install','--no-build-isolation','--no-deps','-e',str(stage)],
                       check=True,stdout=subprocess.DEVNULL)
        (stage/'INSTALL.json').write_text(json.dumps(stamp,sort_keys=True))
    current=root/'current'
    previous=root/'previous'
    if current.is_symlink() and current.resolve()!=release:
        switch_link(previous,current.resolve())
    if not current.is_symlink() or current.resolve()!=release:
        if current.exists() and not current.is_symlink(): raise RuntimeError('unrecognized_current')
        switch_link(current,release)
    return str(current/'.venv/bin/python')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--root',default=str(Path.home()/'.local/share/dsh-control/tui-releases'))
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args()
    result=prepare(args.source,args.root,args.offline,args.plan)
    print(json.dumps(result) if args.plan else result)

if __name__=='__main__':main()
