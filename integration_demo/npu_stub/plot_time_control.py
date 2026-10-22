#!/usr/bin/env python3
"""Korean research figures from immutable REAL RNGD measurements; no hardware use."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties, fontManager
from matplotlib.patches import Patch
import numpy as np

A_COLOR = '#0089b3'
B_COLOR = '#c74787'
NAVY = '#073d5a'
TARGET = '#c8444d'


def dump_csv(path, rows):
    with path.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def main(source, output):
    source = Path(source).resolve(); output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for relative, expected in json.loads((source/'evidence-manifest.json').read_text()).items():
        if hashlib.sha256((source/relative).read_bytes()).hexdigest() != expected:
            raise ValueError('source evidence changed: '+relative)
    raw = json.loads((source/'measurements.json').read_text())
    names = ['original', 'denominator-only', 'budget-only']
    labels = ['기존 제어 로직\n실제 수락량을 분모에 반영',
              '분모 변경 진단 대조군\n명목 지급량을 분모에 반영',
              '예산 변경 진단 대조군\n틱당 예산 300 → 200 ms']
    values = {}; rows = []
    for name in names:
        cases = sorted((r for r in raw if r['label'].startswith(name+'-r')), key=lambda r:r['repeat'])
        if [r['repeat'] for r in cases] != [1, 2, 3]:
            raise ValueError('missing independent repetition')
        shares = []
        for case in cases:
            delta = {n:case['after'][n]['charged_us']-case['before'][n]['charged_us'] for n in ['A', 'B']}
            share = 100*delta['A']/sum(delta.values())
            if abs(share-case['summary']['A']['contract_share']*100) > 1e-10:
                raise ValueError('raw delta and summary disagree')
            shares.append(share)
            rows.append(dict(condition=name, repeat=case['repeat'], A_contract_percent=share,
                             B_contract_percent=100-share, A_charged_us=delta['A'], B_charged_us=delta['B']))
        values[name] = shares
    dump_csv(output/'distribution_raw.csv', rows)
    font_path = '/usr/share/fonts/truetype/nanum/NanumGothic.ttf'
    fontManager.addfont(font_path)
    font = FontProperties(fname=font_path)
    matplotlib.rcParams.update({'font.family':font.get_name(), 'axes.unicode_minus':False,
        'font.size':12, 'axes.spines.top':False, 'axes.spines.right':False,
        'text.color':NAVY, 'axes.labelcolor':NAVY, 'xtick.color':NAVY, 'ytick.color':NAVY,
        'pdf.fonttype':42, 'svg.fonttype':'path'})

    fig, ax = plt.subplots(figsize=(12.8, 6.5))
    fig.subplots_adjust(left=.255, right=.965, top=.76, bottom=.25)
    fig.text(.055,.925,'NPU 시간 크레딧 제어 — 목표 대비 실제 분배',fontsize=22,weight='bold')
    fig.text(.055,.862,'실제 RNGD 4PE · Qwen2.5-0.5B-Instruct · 조건별 3회 · 목표 A:B = 70:30',fontsize=13)
    for i, name in enumerate(names):
        mean = statistics.mean(values[name])
        ax.barh(i, mean, height=.53, color=A_COLOR)
        ax.barh(i, 100-mean, left=mean, height=.53, color=B_COLOR)
        ax.text(mean/2,i,f'A  {mean:.2f}%',ha='center',va='center',color='white',weight='bold',fontsize=16)
        ax.text(mean+(100-mean)/2,i,f'B  {100-mean:.2f}%',ha='center',va='center',color='white',weight='bold',fontsize=16)
        ax.scatter(values[name],[i-.12,i,i+.12],s=38,c=NAVY,edgecolors='white',linewidths=.8,zorder=5)
    ax.axvline(70,color=TARGET,ls='--',lw=2,zorder=4)
    ax.text(70,-.62,'목표 A 70%',ha='center',color=TARGET,fontsize=12,weight='bold')
    ax.set_xlim(0,100);ax.set_ylim(2.6,-.72)
    ax.set_yticks(range(3),labels);ax.tick_params(axis='y',length=0,pad=14)
    ax.set_xlabel('크레딧으로 실행된 요청의 경과시간 비중 (%)',labelpad=12)
    ax.set_xticks(range(0,101,10));ax.grid(axis='x',color='#e4edf2',lw=.8);ax.set_axisbelow(True)
    ax.legend(handles=[Patch(color=A_COLOR,label='워크로드 A'),Patch(color=B_COLOR,label='워크로드 B')],
              loc='upper center',bbox_to_anchor=(.5,-.23),ncol=2,frameon=False)
    fig.text(.055,.105,'막대: 3회 평균  /  점: 각 반복의 A 비중  /  세 조건 모두 요청 게이트 사용',fontsize=11)
    fig.text(.055,.064,'계약 계상시간 기준(WC 추가 실행 제외). 중첩된 generate 호출의 경과시간이며 NPU 독점 점유율이 아님.',fontsize=10,color='#647c8b')
    fig.text(.055,.028,'실측 2026-10-16 · 그래프 작성 2026-10-21 · 진단 대조군은 운영 수정 완료를 뜻하지 않음',fontsize=10,color='#647c8b')
    for suffix in ['png','pdf','svg']:
        fig.savefig(output/f'01_npu_time_credit_distribution.{suffix}',dpi=200,facecolor='white')
    plt.close(fig)

    trace_path = source/'denominator-only-r1/samples.jsonl'
    samples = [json.loads(line) for line in trace_path.read_text().splitlines()]
    origin = samples[0]['monotonic']
    full = []
    for s in samples:
        a,b = s['tenants']['A'],s['tenants']['B']
        total = a['charged_us']+b['charged_us']
        full.append(dict(elapsed_s=s['monotonic']-origin, A_credit_ms=a['credit_us']/1000,
            B_credit_ms=b['credit_us']/1000,A_inflight=a['diag_inflight'],B_inflight=b['diag_inflight'],
            A_contract_us=a['charged_us'],B_contract_us=b['charged_us'],
            A_cumulative_percent=100*a['charged_us']/total if total else None))
    if any(b['elapsed_s']<=a['elapsed_s'] for a,b in zip(full,full[1:])):
        raise ValueError('nonmonotonic samples')
    zoom = [r for r in full if 25<=r['elapsed_s']<=32]
    if len(zoom)<20:raise ValueError('insufficient recorded samples')
    dump_csv(output/'credit_trace_full.csv',full);dump_csv(output/'credit_trace_zoom.csv',zoom)
    fig,axes=plt.subplots(3,1,figsize=(12.8,10),gridspec_kw={'height_ratios':[1.05,1.05,.75]})
    fig.subplots_adjust(left=.115,right=.965,top=.81,bottom=.19,hspace=.73)
    fig.text(.055,.945,'NPU 시간 크레딧 제어 — 기록된 잔액과 요청 상태',fontsize=21,weight='bold')
    fig.text(.055,.897,'분모 변경 진단 대조군 · 반복 1 · 4PE · 요청 워커 2개 · 약 0.2초 간격 저장',fontsize=12)
    x=[r['elapsed_s'] for r in full]
    axes[0].plot(x,[r['A_cumulative_percent'] if r['A_cumulative_percent'] is not None else np.nan for r in full],
                 color=A_COLOR,lw=1.6,label='A 누적 계약시간 비중')
    axes[0].axhline(70,color=TARGET,ls='--',lw=1.2,label='목표 A 70%')
    axes[0].axvspan(25,32,color='#cfe6ef',alpha=.6)
    axes[0].set_ylim(0,105);axes[0].set_xlim(0,x[-1]);axes[0].set_ylabel('A 비중 (%)')
    axes[0].set_title('전체 저장 구간: 첫 응답·준비 부하·측정·드레인 포함',loc='left',fontsize=12,pad=10)
    axes[0].set_xlabel('첫 저장 표본 이후 경과시간 (초)');axes[0].legend(loc='lower right',fontsize=10,frameon=False)
    z=[r['elapsed_s'] for r in zoom]
    for name,color,marker in [('A',A_COLOR,'o'),('B',B_COLOR,'s')]:
        axes[1].plot(z,[r[f'{name}_credit_ms'] for r in zoom],color=color,marker=marker,markersize=3,lw=1.3,label=name)
        axes[2].plot(z,[r[f'{name}_inflight'] for r in zoom],color=color,marker=marker,markersize=4,lw=1,label=name)
    axes[1].axhline(0,color='#768c99',ls='--',lw=1)
    axes[1].axhline(1000,color='#c59d4b',ls=':',lw=1,label='잔액 상한 1,000 ms')
    axes[1].set_ylabel('크레딧 잔액 (ms)')
    axes[1].set_title('확대 구간: 시간 크레딧 잔액(μs 원자료를 ms로 환산)',loc='left',fontsize=12,pad=10)
    axes[1].legend(loc='lower right',fontsize=10,ncol=3,frameon=False)
    axes[2].set_title('같은 구간: 진행 중 generate 호출 수 — PE 점유 개수가 아님',loc='left',fontsize=12,pad=10)
    axes[2].set_ylabel('진행 중 요청 수');axes[2].set_ylim(-.15,2.25);axes[2].set_yticks([0,1,2])
    axes[2].set_xlabel('첫 저장 표본 이후 경과시간 (초)');axes[2].legend(loc='lower right',ncol=2,frameon=False,fontsize=10)
    for ax in axes:
        ax.grid(color='#e4edf2',lw=.7);ax.set_axisbelow(True)
    for ax in axes[1:]:ax.set_xlim(25,32)
    fig.text(.055,.115,'실제 제어 단위: μs 실행시간 크레딧  /  이 조건의 명목 지급: 100 ms마다 A 210 ms, B 90 ms',fontsize=11)
    fig.text(.055,.078,'완료된 요청의 경과시간을 차감하므로 잔액은 음수가 될 수 있음. 실행 중 요청을 중간에 끊는 방식이 아님.',fontsize=10,color='#647c8b')
    fig.text(.055,.047,'점은 저장된 표본, 선은 가독성을 위한 연결. 표본 사이 변화·정확한 커널 실행 구간·NPU 코어 점유를 복원한 그림이 아님.',fontsize=9.5,color='#647c8b')
    fig.text(.055,.018,'아래 확대 구간은 고정된 25–32초 예시이며 7:3 정밀도 평가 구간이 아님. 분배 결과는 첫 번째 그래프의 3회 측정을 참조.',fontsize=9.5,color='#647c8b')
    for suffix in ['png','pdf','svg']:
        fig.savefig(output/f'02_npu_time_credit_trace.{suffix}',dpi=200,facecolor='white')
    plt.close(fig)
    artifact = dict(measurement_date='2026-10-16',graph_date='2026-10-21',new_hardware_experiment=False,
        source_directory=str(source),source_files={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [source/'measurements.json',source/'summary.json',trace_path]},
        means={name:dict(A=statistics.mean(v),B=100-statistics.mean(v),repetitions=v) for name,v in values.items()},
        trace=dict(samples=len(full),span_s=full[-1]['elapsed_s'],zoom_s=[25,32],zoom_samples=len(zoom),
                   includes_warmup=True,exact_phase_boundaries_available=False,last_sample_unmodified=True),
        units=dict(credit='microseconds in raw data; milliseconds in graph',ratio='summed generate-call elapsed-time share excluding WC extras',
                   inflight='in-progress gate requests, not physical PE occupancy'),
        note='All conditions use the gate. Nominal denominator is an experimental control, not a deployed production fix.')
    (output/'graph_data.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(artifact,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');a=p.parse_args();main(a.source,a.output)
