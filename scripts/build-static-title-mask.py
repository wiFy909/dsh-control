"""Build only the small caption bitmap; never distribute the source font."""
import argparse
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont

def build(font_path,output):
    # Design C: 22 px at the 1113 px review width, exported at 1440 px.
    image=Image.new('L',(235,86))
    pen=ImageDraw.Draw(image)
    font=ImageFont.truetype(str(font_path),28)
    for i,char in enumerate('探索未至之境'):
        pen.text((i*35,0),char,font=font,fill=255)
    small=ImageFont.load_default(size=13)
    x=0
    for char in 'EXPLORE THE UNREACHED':
        pen.text((x,57),char,font=small,fill=160)
        x+=small.getlength(char)+2.5
    output.parent.mkdir(parents=True,exist_ok=True)
    image.save(output)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('font',type=Path)
    parser.add_argument('--output',type=Path,default=Path('dsh_control_app/assets/static-title-mask.png'))
    args=parser.parse_args();build(args.font,args.output)
