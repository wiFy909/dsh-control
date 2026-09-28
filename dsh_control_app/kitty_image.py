"""Textual-owned Kitty image placement for terminals without virtual cells.

The installed Ghostty accepts direct TGP placement but not textual-image's
virtual placeholder path.  All writes are scheduled on Textual's UI thread
after a screen refresh; no worker writes to the terminal.
"""
from __future__ import annotations
import base64
from io import BytesIO
import random
import sys
from PIL import Image
from textual.widget import Widget


class KittyImage(Widget):
    def __init__(self,image=None,**kwargs):
        super().__init__(**kwargs)
        self._image=image
        self._image_id=None
        self._next_id=random.randint(1,2**30)
        self._placement_id=random.randint(1,2**30)
        self._scheduled=False

    @property
    def image(self):return self._image

    @image.setter
    def image(self,value):
        self._image=value
        self.schedule_image()

    def on_mount(self):self.schedule_image()
    def on_resize(self,event):self.schedule_image()

    def render(self):return ''

    def schedule_image(self):
        if self.is_mounted and not self._scheduled:
            self._scheduled=True
            self.app.call_after_refresh(self.draw_image)

    @staticmethod
    def _message(**fields):
        return '\x1b_G'+','.join(f'{key}={value}' for key,value in fields.items())+';'

    def clear(self):
        if self._image_id is not None:
            sys.__stdout__.write(self._message(a='d',d='i',i=self._image_id)+'\x1b\\')
            sys.__stdout__.flush()
            self._image_id=None

    def on_unmount(self):self.clear()

    def draw_image(self):
        self._scheduled=False
        if not self.is_mounted or not self.is_on_screen or not self._image:return
        region=self.region
        if region.width<1 or region.height<1:return
        data=BytesIO()
        image=self._image
        if not isinstance(image,Image.Image):image=Image.open(image)
        image.save(data,format='PNG',compress_level=1)
        encoded=base64.b64encode(data.getvalue()).decode('ascii')
        old=self._image_id
        self._next_id=(self._next_id+1)%(2**31-1) or 1
        self._image_id=self._next_id
        stream=sys.__stdout__
        stream.write('\x1b7'+f'\x1b[{region.y+1};{region.x+1}H')
        for offset in range(0,len(encoded),4096):
            chunk=encoded[offset:offset+4096]
            more=1 if offset+4096<len(encoded) else 0
            if offset==0:
                prefix=self._message(a='T',f=100,i=self._image_id,p=self._placement_id,
                                     c=region.width,r=region.height,m=more,q=2)
            else:
                prefix=self._message(f=100,i=self._image_id,m=more,q=2)
            stream.write(prefix+chunk+'\x1b\\')
        if old is not None:
            stream.write(self._message(a='d',d='i',i=old)+'\x1b\\')
        stream.write('\x1b8')
        stream.flush()
