"""Regression checks for the reported particle breakup and lingering frame."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.welcome_art import WelcomeArt
from rich.console import Console

class StaticWelcomeTests(unittest.TestCase):
    def test_canonical_frame_is_static_and_independent(self):
        from dsh_control_app.terminal_art import terminal_frame
        a=terminal_frame(72,20,welcome=True,title=True)
        b=terminal_frame(72,20,welcome=True,title=True)
        b.append('modified')
        self.assertNotEqual(a.plain,b.plain)
        self.assertEqual(a.plain,terminal_frame(72,20,welcome=True,title=True).plain)

    def test_reveal_transitions_and_replay_do_not_reanimate(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as root:
                app=ControlApp(Backend(root),graphics='text')
                async with app.run_test(size=(120,30)) as pilot:
                    screen=app.screen;art=screen.query_one(WelcomeArt)
                    first=art.query_one('#welcome-character').render().plain
                    await pilot.pause(.5)
                    self.assertEqual(first,art.query_one('#welcome-character').render().plain)
                    self.assertIn('探索未至之境',art.query_one('#welcome-character').render().plain)
                    with patch.object(app,'repaint_terminal',wraps=app.repaint_terminal) as repaint:
                        await pilot.press('enter');await pilot.pause(.1)
                        self.assertEqual(screen.phase,'platform');self.assertTrue(repaint.called)
                    await pilot.click('#onboarding-back')
                    self.assertEqual(screen.phase,'title')
                    self.assertEqual(first,art.query_one('#welcome-character').render().plain)
        asyncio.run(scenario())

    def test_slow_strong_caption_breath_and_reduced_motion(self):
        from dsh_control_app.welcome_art import caption_alpha
        self.assertEqual(caption_alpha(0),1.)
        self.assertLessEqual(caption_alpha(4),.09)
        self.assertEqual(caption_alpha(8),1.)
        self.assertGreater(caption_alpha(2),caption_alpha(3))
        async def scenario():
            for reduced in (False,True):
                with tempfile.TemporaryDirectory() as root:
                    app=ControlApp(Backend(root),no_animation=reduced)
                    async with app.run_test(size=(80,24)):
                        art=app.screen.query_one(WelcomeArt)
                        art.title_elapsed=0;art.animate()
                        bright=art.query_one('#welcome-character').render()
                        art.title_elapsed=4;art.animate()
                        dim=art.query_one('#welcome-character').render()
                        self.assertEqual(bright.plain,dim.plain)
                        if reduced:self.assertEqual(bright.spans,dim.spans)
                        else:
                            self.assertNotEqual(bright.spans,dim.spans)
                            self.assertEqual(bright.spans[:-1],dim.spans[:-1])
                        with patch.object(art.query_one('#welcome-character'),'update') as update:
                            art.animate();update.assert_not_called()
        asyncio.run(scenario())

    def test_truecolor_preserves_muted_palette(self):
        from rich.text import Text
        from io import StringIO
        stream=StringIO();console=Console(file=stream,force_terminal=True,legacy_windows=False,color_system='truecolor',_environ={'TERM':'xterm-256color','COLORTERM':'truecolor'})
        console.print(Text('panel',style='on #0b1729'))
        self.assertIn('48;2;11;23;41',stream.getvalue())
        self.assertEqual(console.color_system,'truecolor')
        launcher=__import__('pathlib').Path('scripts/start-dsh-control-tui.ps1').read_text()
        self.assertIn("'--exec','env','COLORTERM=truecolor','TERM=xterm-256color'",launcher)

if __name__=='__main__':unittest.main()
