#!/usr/bin/env python3
"""Build the public, checksum-verified source distribution."""
from pathlib import Path
import hashlib,io,json,re,zipfile
root=Path(__file__).resolve().parents[1]
version=re.search(r'^version = "([^" ]+)"', (root/'pyproject.toml').read_text(),re.M)[1]
assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version)
assert "VERSION = '"+version+"'" in (root/'core/dsh_control.py').read_text()
names={p.relative_to(root).as_posix() for d in ('core','dsh_control_app','assets') for p in (root/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
names.update(('README.md','LICENSE','pyproject.toml','requirements.lock','install.sh','install.ps1','Install DSH Control.command','Install DSH Control.cmd','DSH Control.command','scripts/install-control.py','scripts/start-dsh-control.sh','scripts/start-dsh-control.cmd','scripts/prepare-wsl-tui.py','scripts/start-dsh-control-tui.ps1'))
marker=root/'dsh_control_app/assets/build.json'
build={'build_id':'v'+version,'version':version,'status':'macOS and WSL user-tested; Windows preview','patch_files':{}}
build['patch_files']={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in sorted(names) if n!='dsh_control_app/assets/build.json'}
marker.write_text(json.dumps(build,ensure_ascii=False,indent=2)+'\n')
out=root/'dist';out.mkdir(exist_ok=True);manifest={'build_id':'v'+version,'version':version,'files':{}}
payload=io.BytesIO()
with zipfile.ZipFile(payload,'w',zipfile.ZIP_DEFLATED) as z:
    for n in sorted(names):
        data=(root/n).read_bytes();info=zipfile.ZipInfo(n);info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3
        info.external_attr=(0o100755 if n.endswith(('.sh','.command')) else 0o100644)<<16
        z.writestr(info,data);manifest['files'][n]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
artifacts=[]
for platform in ('macos','windows','linux'):
    name='dsh-control-'+platform+'.zip'
    (out/name).write_bytes(payload.getvalue())
    artifacts.append(name)
# Local build evidence only; release uploads consist of the three platform ZIPs.
(root/'.release-governor').mkdir(exist_ok=True)
(root/'.release-governor/build-artifacts.json').write_text(json.dumps({n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in artifacts},indent=2)+'\n')
with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as z:
    assert z.testzip() is None
    for n,v in manifest['files'].items():assert hashlib.sha256(z.read(n)).hexdigest()==v['sha256']
print('Built v'+version+': '+str(len(names))+' files; ZIP and hashes verified')
