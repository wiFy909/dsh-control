"""Compatibility entry points for the canonical Terminal Brand Renderer."""
from rich.text import Text
from .brand_renderer import detect, plan, render, welcome_caption_columns

CAPTION = '探索未至之境'


def terminal_frame(width, rows, *, welcome=False, title=False, title_alpha=1., capabilities=None, mode='auto'):
    width,rows=max(1,width),max(1,rows)
    cap=capabilities or detect()
    caption = CAPTION if cap.unicode else 'EXPLORE THE UNEXPLORED'
    reserved=welcome_caption_columns(width,rows,cap.unicode) if welcome else 0
    art=render(plan(width-reserved,rows,hero=welcome,capabilities=cap,mode=mode))
    # Reserve the left caption column even before reveal: the whale never moves.
    lines=[' '*reserved+line for line in art.plain.splitlines()]
    show=title and reserved>0
    if show:
        caption_width=12 if cap.unicode else len(caption)
        # Optical grouping follows the visible ink, not the centered canvas.
        # Place the baseline slightly below the mark's visual midpoint.
        occupied=[y for y,line in enumerate(lines) if line.strip()]
        if occupied:
            caption_row=occupied[0]+round((occupied[-1]-occupied[0])*.60)
            neighbors=lines[max(0,caption_row-1):caption_row+2]
            ink_left=min(len(line)-len(line.lstrip()) for line in neighbors if line.strip())
            caption_x=max(1,ink_left-caption_width-3)
        else:
            caption_row=rows//2;caption_x=1
        line=lines[caption_row]
        lines[caption_row]=line[:caption_x]+caption+line[caption_x+caption_width:]
    frame=Text('\n'.join(lines),style='#89afd2',no_wrap=True,overflow='crop')
    if show:
        alpha=max(0.,min(1.,title_alpha))
        rgb=tuple(round(bg+(fg-bg)*alpha) for bg,fg in zip((11,21,37),(209,233,255)))
        start=frame.plain.index(caption)
        frame.stylize('#%02x%02x%02x' % rgb,start,start+len(caption))
    return frame


def compact_whale_frame(width, rows, *, capabilities=None, mode='auto'):
    return render(plan(width,rows,capabilities=capabilities,mode=mode))
