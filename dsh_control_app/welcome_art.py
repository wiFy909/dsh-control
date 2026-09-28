"""Canonical hero brand in the existing onboarding container."""
import time
import math
from textual.containers import Container
from textual.widgets import Static
from .brand_renderer import welcome_size
from .terminal_art import terminal_frame


def caption_alpha(elapsed):
    """Eight-second smooth breath, 8% to 100%, quantized to avoid idle churn."""
    return round((.08+.92*(.5+.5*math.cos(elapsed*math.tau/8)))*64)/64


class WelcomeArt(Container):
    def __init__(self,protocol='text',reduced_motion=False,**kwargs):
        super().__init__(**kwargs)
        self.protocol='text'
        self.motion=False
        self.reduced_motion=reduced_motion
        self.title_visible=True
        self.title_elapsed=0.
        self.scene_alpha=1.
        self.pointer=None
        self.character_size=(72,20)
        self.first_frame_at=None
        self._painted=None

    def compose(self):
        yield Static('',id='welcome-character')

    def on_mount(self):
        self.sync_geometry();self.animate()

    def sync_geometry(self):
        width,height=welcome_size(self.app.size.width,self.app.size.height)
        self.character_size=(width,height)
        self.styles.width=width;self.styles.height=height
        self.parent.styles.width=width

    def on_resize(self,event):
        if self.is_mounted:
            self.sync_geometry();self.animate()

    def hit_title(self,screen_x,screen_y):
        return self.region.contains(screen_x,screen_y)

    def animate(self):
        alpha=1. if self.reduced_motion else caption_alpha(self.title_elapsed)
        key=(self.title_visible,self.character_size,self.app.glyph_mode,self.app.brand_capabilities,alpha if self.title_visible else None)
        if key==self._painted or not self.is_mounted:return
        self.query_one('#welcome-character',Static).update(terminal_frame(
            *self.character_size,welcome=True,title=self.title_visible,title_alpha=alpha,
            capabilities=self.app.brand_capabilities,mode=self.app.glyph_mode),layout=False)
        self._painted=key
        if self.first_frame_at is None:self.first_frame_at=time.monotonic()

    def clear(self):
        self._painted=None
