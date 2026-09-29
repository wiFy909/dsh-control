#!/usr/bin/env python3
"""Build the public, checksum-verified source distribution."""
from pathlib import Path
import hashlib,io,json,re,shutil,tarfile,zipfile
root=Path(__file__).resolve().parents[1]
version=re.search(r'^version = "([^" ]+)"', (root/'pyproject.toml').read_text(),re.M)[1]
assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version)
assert "VERSION = '"+version+"'" in (root/'core/dsh_control.py').read_text()
names={p.relative_to(root).as_posix() for d in ('core','dsh_control_app','assets') for p in (root/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
names.update(('README.md','LICENSE','pyproject.toml','requirements.lock','install.sh','install.ps1','Install DSH Control.command','Install DSH Control.cmd','DSH Control.command','scripts/install-control.py','scripts/start-dsh-control.sh','scripts/start-dsh-control.cmd','scripts/prepare-wsl-tui.py','scripts/start-dsh-control-tui.ps1'))
marker=root/'dsh_control_app/assets/build.json'
build={'build_id':'v'+version,'version':version,'status':'macOS accepted; other platforms preview','patch_files':{}}
build['patch_files']={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in sorted(names) if n!='dsh_control_app/assets/build.json'}
marker.write_text(json.dumps(build,ensure_ascii=False,indent=2)+'\n')
out=root/'dist';out.mkdir(exist_ok=True);manifest={'build_id':'v'+version,'version':version,'files':{}}
with zipfile.ZipFile(out/'dsh-control.zip','w',zipfile.ZIP_DEFLATED) as z:
    for n in sorted(names):
        data=(root/n).read_bytes();info=zipfile.ZipInfo(n);info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3
        info.external_attr=(0o100755 if n.endswith(('.sh','.command')) else 0o100644)<<16
        z.writestr(info,data);manifest['files'][n]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
for n in ('install.sh','install.ps1'):shutil.copyfile(root/n,out/n)
artifacts=['dsh-control.zip','install.sh','install.ps1']
for platform in ('macos','windows','linux'):
    name='dsh-control-'+platform+'.zip'
    # Shared Python payload supports both x64 and arm64; installers select the host runtime.
    shutil.copyfile(out/'dsh-control.zip',out/name)
    artifacts.append(name)
package={'name':'dsh-control-installer','version':version,'private':True,
         'description':'Install DSH Control from its GitHub Release',
         'bin':{'dsh-control-install':'install.cjs'},'engines':{'node':'>=24'}}
with tarfile.open(out/'dsh-control-installer.tgz','w:gz') as archive:
    payload={'package.json':(json.dumps(package,indent=2)+'\n').encode(),
             'install.cjs':(root/'scripts/npm-install.cjs').read_bytes(),
             'install.sh':(root/'install.sh').read_bytes(),
             'install.ps1':(root/'install.ps1').read_bytes(),
             'LICENSE':(root/'LICENSE').read_bytes()}
    for name,data in payload.items():
        info=tarfile.TarInfo('package/'+name);info.size=len(data)
        info.mode=0o755 if name.endswith(('.cjs','.sh')) else 0o644
        archive.addfile(info,io.BytesIO(data))
artifacts.append('dsh-control-installer.tgz')
shutil.copyfile(root/'assets/RELEASE_NOTES.md',out/'RELEASE_NOTES.md')
artifacts.append('RELEASE_NOTES.md')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256((out/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in artifacts))
with zipfile.ZipFile(out/'dsh-control.zip') as z:
    assert z.testzip() is None
    for n,v in manifest['files'].items():assert hashlib.sha256(z.read(n)).hexdigest()==v['sha256']
print('Built v'+version+': '+str(len(names))+' files; ZIP and hashes verified')
