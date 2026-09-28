# DEPRECATED: historical experiment only; not used by ControlApp or onboarding.
"""Stable point samples of the bundled vector. No simulation or mouse forces."""
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFilter
from .particles import contours

@lru_cache(maxsize=12)
def points(width, height):
    scale=min(width*.80/24,height*.80/18)
    left,top=(width-24*scale)/2,(height-18*scale)/2
    mask=Image.new('L',(width,height))
    pen=ImageDraw.Draw(mask)
    for i,group in enumerate(contours()):
        pen.polygon([(round(left+x*scale),round(top+y*scale)) for x,y in group],fill=255 if i==0 else 0)
    edge=mask.filter(ImageFilter.FIND_EDGES)
    out=[]
    step=2 if width<240 else 3
    for y in range(1,height-1,step):
        for x in range(1,width-1,step):
            nearby=edge.crop((x-1,y-1,x+2,y+2)).getextrema()[1]>0
            inside=mask.getpixel((x,y))>0
            if nearby:alpha=.86
            elif inside:alpha=.26+((x*17+y*31)%19)/95
            else:continue
            out.append((x,y,alpha,float(nearby),0.,0.))
    return tuple(out)
