# DEPRECATED: historical experiment only; not used by ControlApp or onboarding.
"""Opt-in pixel whale, hosted by Textual's terminal UI."""
from __future__ import annotations
import os
import time
from PIL import Image, ImageDraw
from textual.app import ComposeResult
from textual.containers import Container
from textual_image.widget import SixelImage
from textual_image._terminal import get_cell_size
from .particles import ParticleField
from .kitty_image import KittyImage
from .static_art import points


class PixelWhale(Container):
    def __init__(self, protocol, **kwargs):
        super().__init__(**kwargs)
        if protocol not in ('sixel','kitty'):
            raise ValueError('unsupported_pixel_protocol')
        self.protocol = protocol
        self.motion = False
        self.field = None
        self.pointer = None
        self.last_frame = time.monotonic()
        self.previous_size = None
        self.tick = 0
        self.cell_pixels = None
        self.hidden_placement = False

    def compose(self) -> ComposeResult:
        image = Image.new('RGB',(4,8),'#071121')
        yield (SixelImage if self.protocol == 'sixel' else KittyImage)(image,id='pixel-image')

    def on_mount(self):
        self.cell_pixels = get_cell_size()
        self.set_interval(1/8,self.animate)

    def on_mouse_move(self,event):
        self.pointer=((event.screen_x-self.region.x)*self.cell_pixels.width,
                      (event.screen_y-self.region.y)*self.cell_pixels.height)

    def on_leave(self,event):
        if event.control is self:
            self.pointer=None

    def animate(self):
        now=time.monotonic()
        dt=min(.5,max(0.,now-self.last_frame))
        self.last_frame=now
        if not self.is_on_screen or not self.visible or self.app.screen is not self.screen or not self.app.app_focus:
            self.pointer=None
            if self.field:self.field.pointer=None;self.field.pointer_power=0.
            if self.protocol=='kitty':
                self.query_one('#pixel-image',KittyImage).clear()
                self.hidden_placement=True
            return
        if not self.size.width or self.size.height<3:return
        size=(min(self.content_size.width,40),min(self.content_size.height,18))
        if not size[0] or not size[1]:return
        pixels=(size[0]*self.cell_pixels.width,size[1]*self.cell_pixels.height)
        resized=pixels!=self.previous_size
        if resized:
            self.field=ParticleField(*pixels)
            self.previous_size=pixels
        if not resized and self.tick and not self.motion:
            if self.protocol=='kitty' and self.hidden_placement:
                self.query_one('#pixel-image',KittyImage).schedule_image()
                self.hidden_placement=False
            return
        image=Image.new('RGB',pixels,'#071121')
        pen=ImageDraw.Draw(image)
        for x,y,alpha,edge,detail,influence in points(*pixels):
            color=(int(15+165*alpha),int(28+189*alpha),int(48+210*alpha))
            radius=1 if edge>.5 or detail>.4 else 0
            if radius:pen.ellipse((int(x)-1,int(y)-1,int(x)+1,int(y)+1),fill=color)
            else:pen.point((int(x),int(y)),fill=color)
        self.query_one('#pixel-image').image=image
        self.tick+=1
