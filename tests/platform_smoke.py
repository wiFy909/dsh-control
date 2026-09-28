"""Portable isolated process/IPC/TUI smoke; runs on macOS, Linux/WSL and Windows.

No DSH credentials or installed DSH HOME are used. The Node server is a fixture.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import dsh_control as c
from dsh_control_app.backend import Backend
from dsh_control_app.app import ControlApp
from dsh_control_app.account import Account

FAKE = '''const http=require('http'); const port=Number(process.argv[process.argv.indexOf('--port')+1]);
const s=http.createServer((q,r)=>{r.writeHead(401);r.end('fixture')});
s.listen(port,'127.0.0.1',()=>console.log(`dsh web: http://127.0.0.1:${port}/?token=FIXTURE_ONLY`));
process.on('SIGTERM',()=>s.close(()=>process.exit(0)));
'''

def main():
    with tempfile.TemporaryDirectory(prefix='dct-',dir=None if os.name=='nt' else '/tmp') as directory:
        base=Path(directory).resolve(); root=base/'install'; candidate=root/'candidates/test'
        entry=candidate/c.ENTRY; entry.parent.mkdir(parents=True); entry.write_text(FAKE)
        (candidate/'package-lock.json').write_text('{}')
        if os.name=='nt':
            subprocess.run(['cmd.exe','/c','mklink','/J',str(root/'current'),str(candidate)],check=True,capture_output=True)
        else: (root/'current').symlink_to(candidate)
        home=base/'home 中文'; home.mkdir()
        node=shutil.which('node'); major=int(subprocess.check_output([node,'--version'],text=True).strip()[1:].split('.')[0])
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        definition=base/'definition.json'
        c.atomic_json(definition,dict(root=str(root),home=str(home),node=node,node_major=major,web_port=port))
        c.atomic_json(root/'MANAGED_INSTALL.json',dict(current=str(candidate.resolve()),version='fixture',lock_sha256=c.sha(candidate/'package-lock.json')))
        ctl=c.Controller(base/'control',timeout=15)
        adopted=ctl.execute({'action':'adopt','definition':str(definition)})
        assert adopted['ok'],adopted
        iid=adopted['instance_id']; config=ctl.config(iid)
        results=[]
        try:
            first=ctl.execute({'action':'start','instance_id':iid,'open_browser':False}); assert first['ok'],first
            pid=first['evidence']['pid']; results.append('start')
            assert ctl.execute({'action':'start','instance_id':iid,'open_browser':False})['evidence']['pid']==pid
            results.append('repeat-start-one-process')
            if os.name=='nt':
                record=c.read_json(ctl.folder(iid)/'running.json')
                with socket.socket() as s:
                    s.settimeout(2); s.connect(('127.0.0.1',record['control_port']))
                    s.sendall(json.dumps({'action':'stop','instance_id':iid,'secret':'wrong'}).encode())
                    assert not s.recv(4096)
                assert ctl.status(config)['state']=='running'; results.append('unauthorized-ipc-rejected')
            other=c.Controller(base/'other-control')
            c.atomic_json(other.folder(iid)/'instance.json',config)
            assert other.status(config)['state']=='unknown'; results.append('cross-state-owner-protected')
            async def ui():
                app=ControlApp(Backend(base/'control',iid))
                with patch.object(Account,'restore'):
                    async with app.run_test(size=(130,44)) as pilot:
                        await pilot.pause(.6)
                        assert app.backend.snapshot['state']=='running'
                        await pilot.click('#mcp'); assert app.selected=='mcp'
                        await pilot.click('#balance'); await pilot.press('escape')
                        await pilot.resize_terminal(100,34); await pilot.pause(.2)
                        results.append('tui-controls-resize-modal')
            asyncio.run(ui())
            stop=ctl.execute({'action':'stop','instance_id':iid}); assert stop['ok'],stop
            assert c.port_free(port); results.append('stop-releases-port')
            assert ctl.execute({'action':'stop','instance_id':iid})['ok']; results.append('repeat-stop')
            print(json.dumps({'platform':sys.platform,'ok':True,'checks':results},ensure_ascii=False))
        finally:
            stopped=ctl.execute({'action':'stop','instance_id':iid})
            if not stopped['ok']: print('ISOLATED_FIXTURE_REQUIRES_INSPECTION',str(base)); raise RuntimeError('fixture stop failed')

if __name__=='__main__': main()
