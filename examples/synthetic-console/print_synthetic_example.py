"""Print explicitly synthetic illustration values, without reading measurement data."""
import argparse
from decimal import Decimal

VALUES = tuple(map(Decimal, ('65.8', '67.4', '66.9', '68.2', '67.2')))
MEAN = sum(VALUES) / Decimal(len(VALUES))
assert len(set(VALUES)) == 5
assert MEAN == Decimal('67.1')

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--summary', action='store_true', help='Hide individual synthetic rows.')
args = parser.parse_args()

print('KETRIS GPU evaluation | 합성 예시 — 실측 결과 아님')
print('[WORK]   Qwen2.5-0.5B | 7 workers')
print(f'[RUNS]   entries={len(VALUES)}')
print('[METHOD] Arithmetic mean of five relative-utilization indices')
print('[METRIC] Relative GPU activity index | NVIDIA MPS reference=100')
print('[TARGET] Year 2 reference target: 60.0%')
print('\n--- NVIDIA MPS vs KETRIS ---')
if not args.summary:
    for number, value in enumerate(VALUES, 1):
        print(f'[RUN {number}/5] workers=7  relative_gpu_activity={value:.1f}%')
print(f'[COUNT] entries={len(VALUES)}')
print('\n--- Arithmetic summary ---')
print(f'[MEAN] Relative GPU activity index={MEAN:.1f}%')
print(f'[RANGE] min={min(VALUES):.1f}% | max={max(VALUES):.1f}%')
print('[CHECK] Arithmetic consistency verified.')
print('[NOTE] These are relative indices, not absolute GPU-busy percentages.')
