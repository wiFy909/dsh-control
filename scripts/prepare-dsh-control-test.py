#!/usr/bin/env python3
"""Prepare an explicitly selected, separate manual-test instance inside WSL."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--definition')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('control', Path(__file__).resolve().parents[1] / 'core/dsh_control.py')
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    directory = Path(args.directory).expanduser().resolve()
    marker = directory / 'MANUAL_TEST.json'
    ctl = c.Controller(directory / 'control')
    if marker.exists():
        saved = c.read_json(marker)
        if saved.get('purpose') != 'dsh-control-manual-test-v1':
            raise RuntimeError('directory ownership is not recognized')
        config = c.definition(directory / 'definition.json')
        result = ctl.execute({'action': 'status', 'instance_id': config['instance_id']})
        if not result['ok']:
            raise RuntimeError('existing test state needs inspection; nothing replaced')
    else:
        if directory.exists() and any(directory.iterdir()):
            raise RuntimeError('test directory is not empty; nothing replaced')
        sources = [Path(args.definition)] if args.definition else c.discover_paths()
        if len(sources) != 1:
            raise RuntimeError('select exactly one compatible installation definition')
        source = c.definition(sources[0])
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        installation = directory / 'installation'
        candidate = installation / 'candidates' / 'pinned'
        candidate.mkdir(parents=True)
        original_candidate = Path(source['entry']).parents[4]
        # Reuse program files only; never copy the original HOME or credentials.
        (candidate / 'node_modules').symlink_to(original_candidate / 'node_modules')
        shutil.copyfile(original_candidate / 'package-lock.json', candidate / 'package-lock.json')
        (installation / 'current').symlink_to(candidate)
        test_home = directory / 'home'
        test_home.mkdir(mode=0o700)
        (test_home / 'cordis.patch.yml').write_text(
            '- id: session-log-deepseek\n  config:\n    enabled: false\n'
            '- id: session-telemetry-otel\n  config:\n    mode: DISABLED\n'
            '- id: llm-deepseek\n  config:\n    apiKeyEnv: DSH_CONTROL_TEST_KEY\n', encoding='utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        c.atomic_json(directory / 'definition.json', {
            'root': str(installation), 'home': str(test_home), 'node': source['node'],
            'node_major': source['node_major'], 'web_port': port})
        c.atomic_json(installation / 'MANAGED_INSTALL.json', {
            'current': str(candidate), 'version': source['version'],
            'lock_sha256': c.sha(candidate / 'package-lock.json')})
        result = ctl.execute({'action': 'adopt', 'definition': str(directory / 'definition.json')})
        if not result['ok']:
            raise RuntimeError('test adoption failed; keep directory for inspection')
        config = result['instance']
        c.atomic_json(marker, {'purpose': 'dsh-control-manual-test-v1', 'instance_id': config['instance_id']})
    print(json.dumps({'purpose': 'dsh-control-manual-test-v1',
                      'distro': os.environ.get('WSL_DISTRO_NAME'),
                      'user': __import__('pwd').getpwuid(os.getuid()).pw_name,
                      'instance_id': config['instance_id'], 'port': config['port'],
                      'state_directory': str(directory / 'control'),
                      'definition': str(directory / 'definition.json')}))


if __name__ == '__main__':
    main()
