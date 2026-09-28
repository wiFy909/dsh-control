"""Canonical static brand widget; only geometry changes trigger a repaint."""
from textual.widgets import Static
from .brand_renderer import plan, render


class Whale(Static):
    motion=False  # Legacy compatibility; motion no longer changes brand geometry.
    field=None
    pointer=None
    protocol='text'
    tick=0
    previous_size=None

    def on_mount(self):
        self._plan=None
        self.call_after_refresh(self.animate)

    def on_resize(self,event):
        self.call_after_refresh(self.animate)

    def on_leave(self,event):
        self.pointer=None

    def animate(self):
        if not self.is_mounted or not self.content_size.width or not self.content_size.height:return
        p=plan(self.content_size.width,self.content_size.height,
               capabilities=self.app.brand_capabilities,mode=self.app.glyph_mode)
        if p==self._plan:return
        self._plan=p
        self.previous_size=(p.width,p.rows)
        self.update(render(p),layout=False)
        self.tick+=1
