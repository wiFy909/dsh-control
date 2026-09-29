"""Portable rendering contracts, independent of a terminal image protocol."""
import asyncio
from io import StringIO
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from rich.console import Console
from rich.cells import cell_len
from rich.text import Text
from dsh_control_app.app import ControlApp, main
from dsh_control_app.backend import Backend
from dsh_control_app.terminal_art import terminal_frame, compact_whale_frame
from dsh_control_app.welcome_art import WelcomeArt

class TerminalArtTests(unittest.TestCase):
    def test_compact_whale_modes_stay_in_bounds(self):
        for mode in ('braille','block','ascii'):
            for width,rows in ((16,5),(20,7),(28,9)):
                frame=compact_whale_frame(width,rows,mode=mode)
                self.assertEqual(len(frame.plain.splitlines()),rows)
                self.assertTrue(all(cell_len(line)==width for line in frame.plain.splitlines()))
                self.assertTrue(frame.plain.strip())

    def test_caption_pulse_changes_color_without_moving_dots(self):
        bright=terminal_frame(72,20,welcome=True,title=True,title_alpha=1.)
        dim=terminal_frame(72,20,welcome=True,title=True,title_alpha=.45)
        self.assertEqual(bright.plain,dim.plain)
        self.assertNotEqual(bright.spans,dim.spans)
        self.assertEqual(bright.spans[:-1],dim.spans[:-1])

    def test_caption_is_left_of_whale_and_never_overwrites_it(self):
        for width, rows in [(50,14),(76,14),(96,20),(96,30),(96,36),(112,32)]:
            titled=terminal_frame(width,rows,welcome=True,title=True).plain
            plain=terminal_frame(width,rows,welcome=True,title=False).plain
            self.assertEqual(titled.replace('探索未至之境',' '*12),plain)
            caption_row=next(y for y,line in enumerate(titled.splitlines()) if '探索未至之境' in line)
            occupied=[y for y,line in enumerate(plain.splitlines()) if line.strip()]
            self.assertGreater(caption_row,(occupied[0]+occupied[-1])/2)
            caption_x=titled.splitlines()[caption_row].index('探索未至之境')
            ink_left=min(len(line)-len(line.lstrip()) for line in plain.splitlines()[caption_row-1:caption_row+2] if line.strip())
            self.assertEqual(ink_left-(caption_x+12),3)
            self.assertTrue(any(line[16:].strip() for line in plain.splitlines()))

    def test_cells_and_caption_survive_sizes_and_color_depths(self):
        for width,rows in [(50,14),(72,20),(112,32),(20,7)]:
            frame=terminal_frame(width,rows,welcome=True,title=True)
            lines=frame.plain.splitlines()
            self.assertEqual(len(lines),rows)
            self.assertTrue(all(cell_len(line)==width for line in lines))
            self.assertIn('探索未至之境',frame.plain)
        # Each terminal starts a separate process. Rich caches ANSI codes in
        # Style instances; do not reuse them across different color systems.
        for depth in ('256','truecolor'):
            code=("from rich.console import Console; "
                  "from dsh_control_app.terminal_art import terminal_frame; "
                  f"Console(width=72,color_system={depth!r},force_terminal=True,legacy_windows=False,no_color=False).print(terminal_frame(72,20,welcome=True,title=True))")
            output=subprocess.check_output([sys.executable,'-c',code],text=True)
            self.assertIn('探索未至之境',Text.from_ansi(output).plain)
            self.assertNotIn('\x1b_G',output)
            self.assertNotIn('\x1bP',output)
            self.assertIn('38;5;' if depth=='256' else '38;2;',output)
        original=terminal_frame(72,20,welcome=True,title=True).plain
        edited=terminal_frame(72,20,welcome=True,title=True);edited.append('changed')
        self.assertEqual(terminal_frame(72,20,welcome=True,title=True).plain,original)

    def test_default_entry_never_queries_an_image_protocol(self):
        with patch('dsh_control_app.app.ControlApp') as app, \
             patch('textual_image.renderable.sixel.query_terminal_support') as sixel, \
             patch('textual_image.renderable.tgp.query_terminal_support') as kitty:
            main([])
            self.assertEqual(app.call_args.kwargs['graphics'],'text')
            sixel.assert_not_called();kitty.assert_not_called()
            app.return_value.run.assert_called_once()

    def test_resize_reveal_clear_replay_stays_in_viewport(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as root:
                app=ControlApp(Backend(root),graphics='text')
                async with app.run_test(size=(120,30)) as pilot:
                    screen=app.screen;art=screen.query_one(WelcomeArt)
                    for w,h in [(80,24),(120,30),(150,45),(100,28),(80,24)]:
                        await pilot.resize_terminal(w,h);await pilot.pause(.08)
                        self.assertTrue(art.region in screen.region,(w,h,art.region))
                        self.assertFalse(any(button.display and button.region.width for button in screen.query('Button')))
                        frame=art.query_one('#welcome-character').render().plain
                        self.assertRegex(frame, '[\u2801-\u28ff]')
                        self.assertTrue(all(cell_len(line)==art.character_size[0] for line in frame.splitlines()))
                    self.assertIn('探索未至之境',art.query_one('#welcome-character').render().plain)
                    await pilot.resize_terminal(120,30);await pilot.pause(.08)
                    self.assertIn('探索未至之境',art.query_one('#welcome-character').render().plain)
                    await pilot.press('enter');await pilot.pause(.08)
                    self.assertEqual(screen.phase,'platform')
                    self.assertFalse(screen.query_one('#welcome-view').display)
                    await pilot.click('#onboarding-back');await pilot.pause(.08)
                    self.assertEqual(screen.phase,'title')
                    self.assertIn('探索未至之境',art.query_one('#welcome-character').render().plain)
                    self.assertTrue(art.region in screen.region)
        asyncio.run(scenario())

if __name__=='__main__':unittest.main()
