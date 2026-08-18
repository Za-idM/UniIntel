import sys, asyncio, os
sys.path.insert(0, r'C:/Users/Zaid Mujawar/Desktop/UNILOG - claude/uniintel/backend')
os.chdir(r'C:/Users/Zaid Mujawar/Desktop/UNILOG - claude/uniintel')
import logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(name)s: %(message)s')
import csv
GT = r'C:/Users/Zaid Mujawar/Desktop/UNILOG - claude/uniintel/data/ground_truth/gt_delivery_200.csv'
with open(GT, encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
INPUT_COLS = ['Mfg_Part_Num','Part_Desc','E1_Brand','Unilog_Brand','DIB_Brand','Part_Manuf']
for mpn in ('S21354',):
    for r in rows:
        if r['Mfg_Part_Num'] == mpn:
            input_row = {k: r[k] for k in INPUT_COLS}
            break

from pipeline.orchestrator import process_row
async def t():
    return await process_row(input_row, 'diag-job')
p = asyncio.run(t())
print()
print('=== product for', mpn, '===')
print('classpath:', p.classpath)
print('mfr_url:', p.mfr_url)
print('attributes (filled only):')
for a in p.attributes:
    if a.value:
        print(f'  slot{a.slot:02d} {a.label:38s} value={a.value!r:20s} uom={a.uom!r:8s} origin={a.origin}')
