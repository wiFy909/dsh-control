"""User installs must survive moving the download and changing working directory."""
import importlib.util
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec=importlib.util.spec_from_file_location('user_install',Path(__file__).resolve().parents[1]/'scripts/install-control.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class UserInstallTests(unittest.TestCase):
    def release(self,root):
        source=root/'download';source.mkdir()
        files={'requirements.lock':b'fixture','core/dsh_control.py':b'',
               'dsh_control_app/__init__.py':b'',
               'dsh_control_app/assets/build.json':b'{"build_id":"fixture"}',
               'dsh_control_app/app.py':b'import json\ndef main():\n print(json.dumps({"build_id":"fixture","loaded_module":__file__}))\n return 0\n'}
        manifest={'build_id':'fixture','files':{}}
        for name,data in files.items():
            p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            manifest['files'][name]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        (source/'MANIFEST.json').write_text(json.dumps(manifest))
        return source

    @unittest.skipIf(os.name=='nt','POSIX command shim; Windows wrapper generated separately')
    def test_global_commands_are_isolated_from_download_cwd_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.release(root);app=root/'user app';bin_dir=root/'user bin'
            bin_dir.mkdir();(bin_dir/'dsh').write_text('old launcher')
            with patch.object(installer,'provision',return_value=Path(sys.executable)):
                result=installer.install(source,app,bin_dir,replace_dsh=True,update_path=False)
            source.rename(root/'download-moved-away')
            poison=root/'dsh_control_app';poison.mkdir();(poison/'__init__.py').write_text('raise RuntimeError("wrong cwd")')
            env={**os.environ,'PYTHONPATH':str(root)}
            for args in ([str(bin_dir/'dsh-control'),'--build-info'],[str(bin_dir/'dsh'),'--build-info'],[str(bin_dir/'dsh'),'control','--build-info']):
                info=json.loads(subprocess.check_output(args,cwd=root,env=env,text=True))
                self.assertTrue(Path(info['loaded_module']).is_relative_to(Path(result['release'])))
            self.assertEqual((app/'previous-commands/dsh').read_text(),'old launcher')
            self.assertNotIn(str(source),(bin_dir/'dsh-control').read_text())

    def test_modified_package_and_failed_dependencies_preserve_current_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.release(root);app=root/'app';app.mkdir();current=app/'current.json';current.write_text('old')
            with patch.object(installer,'provision',side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):installer.install(source,app,root/'bin',update_path=False)
            self.assertEqual(current.read_text(),'old');self.assertFalse((app/'install.lock').exists())
            (source/'requirements.lock').write_text('modified')
            with self.assertRaises(ValueError):installer.install(source,app,root/'bin',update_path=False)
            self.assertEqual(current.read_text(),'old')

    def test_archive_traversal_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('../escape','/absolute','C:/escape','folder\\escape'):
                archive=root/'bad.zip'
                with zipfile.ZipFile(archive,'w') as z:z.writestr(name,b'bad')
                with self.assertRaises(ValueError):installer.unpack(archive,root/'out')
            with zipfile.ZipFile(root/'link.zip','w') as z:
                info=zipfile.ZipInfo('link');info.external_attr=0o120777<<16;z.writestr(info,'outside')
            with self.assertRaises(ValueError):installer.unpack(root/'link.zip',root/'out')

    def test_existing_install_lock_does_not_get_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.release(root);app=root/'app';app.mkdir();lock=app/'install.lock';lock.write_text('another install')
            with self.assertRaises(ValueError):installer.install(source,app,root/'bin',update_path=False)
            self.assertTrue(lock.exists())
