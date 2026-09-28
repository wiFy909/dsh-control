"""Explicitly accept newly reviewed brand text grids (never called by tests)."""
import argparse
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--accept',action='store_true',required=True)
parser.parse_args()
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root),str(root/'tests')]
from test_brand_renderer import GOLDENS,golden_cases
GOLDENS.mkdir(parents=True,exist_ok=True)
for name,plan,content in golden_cases():
    (GOLDENS/name).write_text(content,encoding='utf-8')
print('Updated 30 brand text grids; review their diff before accepting.')
