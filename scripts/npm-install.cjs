#!/usr/bin/env node
// A single npx entry for all supported shells. Payload stays on GitHub Releases.
'use strict';
const { spawnSync } = require('node:child_process');
const path = require('node:path');
if (Number(process.versions.node.split('.')[0]) < 24) {
  console.error('请先安装 Node.js 24 或更新版本。');
  process.exit(1);
}
const win = process.platform === 'win32';
const script = path.join(__dirname, win ? 'install.ps1' : 'install.sh');
const command = win ? 'powershell.exe' : 'sh';
const args = win ? ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script] : [script];
const result = spawnSync(command, [...args, ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error) console.error('安装器无法启动：'+result.error.code);
process.exit(result.status ?? 1);
