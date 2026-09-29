"""Canonical provenance, capability fixtures, physical geometry and UI regressions.

Golden files are text grids, not evidence of native terminal/font compatibility.
"""
import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rich.cells import cell_len
from PIL import Image
from dsh_control_app.brand_renderer import ASSETS, detect, diagnostics, plan, render, welcome_size, welcome_caption_columns
from dsh_control_app.app import ControlApp, main
from dsh_control_app.backend import Backend
from dsh_control_app.welcome_art import WelcomeArt
from dsh_control_app.whale import Whale

VIEWPORTS=((80,24),(120,30),(160,40),(200,50),(242,63))
GOLDENS=Path(__file__).with_name('golden')/'brand'
CAP=detect({'TERM':'xterm-256color','TERM_PROGRAM':'Apple_Terminal'},'Darwin','', 'utf-8')
CASES=[('Windows',{'WT_SESSION':'fixture'},'windows-native','Windows Terminal'),
       ('Linux',{'WSL_DISTRO_NAME':'Ubuntu','WT_SESSION':'fixture'},'wsl','Windows Terminal'),
       ('Darwin',{'TERM_PROGRAM':'Apple_Terminal'},'macos','Terminal.app'),
       ('Linux',{'VTE_VERSION':'7800'},'linux','GNOME Terminal/VTE'),
       ('Linux',{},'linux','unknown'),
       ('Darwin',{'TERM_PROGRAM':'ghostty'},'macos','Ghostty'),
       ('Darwin',{'TERM_PROGRAM':'iTerm.app'},'macos','iTerm2'),
       ('Linux',{'KITTY_WINDOW_ID':'1'},'linux','kitty'),
       ('Windows',{'TERM_PROGRAM':'WezTerm'},'windows-native','WezTerm'),
       ('Linux',{'KONSOLE_VERSION':'240801'},'linux','Konsole')]


def golden_cases():
    for width,rows in VIEWPORTS:
        for mode in ('braille','block','ascii'):
            d=diagnostics(width,rows,mode)
            for role,key in (('welcome','welcome'),('dashboard','dashboard_estimate')):
                values=d[key]
                p=plan(values['width'],values['rows'],hero=role=='welcome',capabilities=CAP,mode=mode)
                yield f'{width}x{rows}-{role}-{mode}.txt',p,render(p).plain+'\n'


class BrandRendererTests(unittest.TestCase):
    def test_source_and_independent_lod_rebuild(self):
        manifest=json.loads((ASSETS/'manifest.json').read_text())
        self.assertEqual(hashlib.sha256((ASSETS/'canonical.svg').read_bytes()).hexdigest(),manifest['source_sha256'])
        spec=importlib.util.spec_from_file_location('brand_build','scripts/build-brand-assets.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            mod.build(Path(tmp))
            rebuilt=json.loads((Path(tmp)/'manifest.json').read_text())
            self.assertEqual(rebuilt['source_sha256'],manifest['source_sha256'])
            self.assertEqual(rebuilt['compiler'],manifest['compiler'])
            self.assertEqual(set(rebuilt['lods']),set(manifest['lods']))
            for lod in manifest['lods']:
                name=lod+'.png'
                with Image.open(Path(tmp)/name) as fresh, Image.open(ASSETS/name) as stored:
                    self.assertEqual((fresh.size,fresh.mode),(stored.size,stored.mode))
                    self.assertEqual(fresh.tobytes(),stored.tobytes())
                for folder,metadata in ((Path(tmp),rebuilt),(ASSETS,manifest)):
                    self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),metadata['lods'][lod]['sha256'])

    def test_runtime_renderer_matrix_and_fallback(self):
        from dsh_control_app.brand_renderer import choose_mode
        for system,env,runtime,terminal in CASES:
            cap=detect({'TERM':'xterm-256color',**env},system,'','utf-8')
            self.assertEqual((cap.runtime_platform,cap.terminal),(runtime,terminal))
            self.assertEqual(choose_mode(cap),'ascii' if terminal=='unknown' else 'braille')
            self.assertEqual(choose_mode(replace(cap,braille=False,block=True)),'block')
            self.assertEqual(choose_mode(replace(cap,braille=False,block=False)),'ascii')
            for mode in ('braille','block','ascii'):self.assertEqual(choose_mode(cap,mode),mode)
        self.assertFalse(detect({'TERM':'dumb','WT_SESSION':'x'},'Windows','','utf-8').braille)
        self.assertFalse(detect({'WT_SESSION':'x'},'Windows','','ascii').block)
        self.assertEqual(detect({},'Linux','microsoft-standard','utf-8').runtime_platform,'wsl')

    def test_goldens_bounds_modes_and_physical_ratio(self):
        for name,p,content in golden_cases():
            self.assertEqual(content,(GOLDENS/name).read_text(),name)
            self.assertEqual(len(content.splitlines()),p.rows)
            self.assertTrue(all(cell_len(line)==p.width for line in content.splitlines()))
            self.assertTrue(content.strip())
            if p.mode=='ascii':self.assertTrue(content.isascii())
            else:self.assertTrue(all(c in ' \n' or (0x2801<=ord(c)<=0x28ff if p.mode=='braille' else c in '▘▝▀▖▌▞▛▗▚▐▜▄▙▟█') for c in content))
            lines=content.splitlines()
            self.assertTrue(all(line[0]==line[-1]==' ' for line in lines))
        # Physical dimensions share one scale, including unusual cell metrics.
        for ratio in (.3,.5,.65,1.):
            cap=replace(CAP,cell_aspect=ratio)
            for w,h in VIEWPORTS:
                p=plan(w,h,hero=True,capabilities=cap,mode='braille')
                self.assertLessEqual(abs(p.draw_rows/p.cell_aspect-p.draw_columns),1/p.cell_aspect)
                self.assertLessEqual(p.draw_columns,64)
        self.assertEqual(plan(20,9,capabilities=CAP).lod,'small')
        self.assertEqual(plan(26,13,capabilities=CAP).lod,'medium')
        self.assertEqual(plan(242,63,hero=True,capabilities=CAP).draw_columns,64)

    def test_diagnostics_and_cli_validation(self):
        import io
        with patch.dict('os.environ',{'TERM':'xterm-256color','TERM_PROGRAM':'Apple_Terminal','DSHCTL_CELL_ASPECT':'.6','DSHCTL_GLYPH_MODE':'block'},clear=True):
            d=diagnostics(120,30,'block')
            self.assertEqual(d['glyph_mode'],'block');self.assertEqual(d['cell_aspect'],.6)
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(main(['render-doctor','--columns','80','--rows','24']),0)
                self.assertEqual(json.loads(output.getvalue())['glyph_mode'],'block')
        with patch.dict('os.environ',{'DSHCTL_CELL_ASPECT':'nan'}):
            with self.assertRaises(ValueError):detect()

    def test_resize_flow_all_modes_no_idle_redraw(self):
        async def scenario():
            for mode in ('braille','block','ascii'):
                with tempfile.TemporaryDirectory() as root:
                    app=ControlApp(Backend(root),glyph_mode=mode)
                    with patch.object(app,'brand_capabilities',CAP):
                        async with app.run_test(size=(80,24)) as pilot:
                            screen=app.screen;art=screen.query_one(WelcomeArt)
                            for w,h in (*VIEWPORTS,*reversed(VIEWPORTS)):
                                await pilot.resize_terminal(w,h);await pilot.pause(.03)
                                self.assertTrue(art.region in screen.region,(mode,w,h,art.region))
                                self.assertFalse(any(button.display and button.region.width for button in screen.query('Button')))
                                before=art.query_one('#welcome-character').render().plain
                                reserved=welcome_caption_columns(*art.character_size,CAP.unicode)
                                blank=before.replace('探索未至之境',' '*12)
                                self.assertEqual('\n'.join(line[reserved:] for line in blank.splitlines())+'\n',(GOLDENS/f'{w}x{h}-welcome-{mode}.txt').read_text())
                                art.animate()
                                self.assertEqual(before,art.query_one('#welcome-character').render().plain)
                            await pilot.press('enter');await pilot.pause()
                            self.assertEqual(screen.phase,'platform')
                            await pilot.click('#onboarding-back');await pilot.pause()
                            self.assertEqual(screen.phase,'title')
                            app.pop_screen();await pilot.pause()
                            whale=app.query_one(Whale)
                            for w,h in VIEWPORTS:
                                await pilot.resize_terminal(w,h);await pilot.pause(.05)
                                for name in ('whale','start','stop','open','selftest','update','exit'):
                                    widget=app.query_one('#'+name)
                                    self.assertTrue(widget.region in app.screen.region,(mode,w,h,name,widget.region))
                                    self.assertTrue(widget.region in app.query_one('#nav').content_region,(w,h,name,widget.region))
                                from dataclasses import asdict
                                self.assertEqual(asdict(whale._plan),diagnostics(w,h,mode)['dashboard_estimate'])
                                self.assertEqual(whale.render().plain+'\n',(GOLDENS/f'{w}x{h}-dashboard-{mode}.txt').read_text())
                                tick=whale.tick;whale.animate();self.assertEqual(tick,whale.tick)
                                self.assertEqual(whale._plan.lod,'medium' if w>=200 else 'small')
                            app.background_ready=True;app.dashboard_ready_at=0
                            app.query_one('#start').focus()
                            with patch.object(app.backend,'run') as run:
                                await pilot.press('1');await pilot.pause()
                                run.assert_called_once()
        asyncio.run(scenario())

if __name__=='__main__':unittest.main()
