#!/usr/bin/env python3
"""Generate a standalone evidence report from completed real NPU controls."""
import argparse
import csv
import hashlib
import html
import json
from pathlib import Path
import statistics


def main(directory):
    root = Path(directory).resolve()
    rows = json.loads((root/'measurements.json').read_text())
    names = ['original', 'denominator-only', 'budget-only']
    labels = ['Original', 'Denominator only', 'Budget only']
    grouped = {name:[r for r in rows if r['label'].startswith(name+'-r')] for name in names}
    if len(rows) != 9 or any(len(values) != 3 for values in grouped.values()):
        raise ValueError('requires all three repetitions of all three conditions')
    summaries = {}; flat = []
    for name, values in grouped.items():
        samples = {key:[r['summary']['A'][key]*100 for r in values]
            for key in ['requested_share','accepted_share','denominator_share','contract_share','total_accounted_share','completion_share']}
        samples['total_rps'] = [sum(t['rps_drain_inclusive'] for t in r['load']['tenants'].values()) for r in values]
        samples['wc_extra_percent'] = [100*sum(t['deltas']['charged_extra_us'] for t in r['summary'].values()) /
            sum(t['deltas']['charged_us']+t['deltas']['charged_extra_us'] for t in r['summary'].values()) for r in values]
        summaries[name] = {key:dict(mean=statistics.mean(v), stdev=statistics.stdev(v), min=min(v), max=max(v), values=v)
                           for key,v in samples.items()}
        for r in values:
            a = r['summary']['A']; d = a['deltas']
            flat.append(dict(condition=name, repeat=r['repeat'], contract_A_pct=a['contract_share']*100,
                total_A_pct=a['total_accounted_share']*100, completed_A_pct=a['completion_share']*100,
                nominal_grant_A_pct=a['requested_share']*100, accepted_grant_A_pct=a['accepted_share']*100,
                selection_denominator_A_pct=a['denominator_share']*100, A_requested_us=d['diag_requested_us'],
                A_clipped_us=d['diag_clipped_us'], A_accepted_us=d['accepted_us'], elapsed_s=r['elapsed_s']))
    signatures = {}; parallel = {}
    for r in rows:
        texts = set(); selected = 0; already_inflight = 0
        for line in (root/r['label']/'events.jsonl').open():
            event = json.loads(line)
            if event['kind']=='completed' and event.get('generated_text_sha256'):
                texts.add(event['generated_text_sha256'])
            if event['kind']=='selected':
                selected += 1
                already_inflight += event['states'][event['tenant']]['inflight'] > 1
        signatures[r['label']] = sorted(texts)
        parallel[r['label']] = dict(selected=selected, selected_with_same_tenant_already_inflight=already_inflight,
                                    scope='whole case including probe and warmup')
    union = set().union(*(set(v) for v in signatures.values()))
    before = json.loads((root/'before.json').read_text()); after = json.loads((root/'after.json').read_text())
    original_mean=summaries['original']['contract_share']['mean']
    control_mean=summaries['denominator-only']['contract_share']['mean']
    evidence = dict(measurement='REAL RNGD, 4PE, Qwen2.5-0.5B-Instruct, three independent fresh contexts per arm',
        summaries=summaries, effect_A_contract_percentage_points=control_mean-original_mean,
        absolute_target_error_original_pp=abs(70-original_mean), absolute_target_error_denominator_control_pp=abs(70-control_mean),
        response_text_signatures=signatures, unique_response_texts_across_cases=len(union),
        parallel_selection=parallel,
        metric_definitions=dict(denominator_share='measurement-window delta granted_us share; actual selection uses cumulative charged_us / granted_us',
                                accepted_share='measurement-window requested-minus-clipped credit share',
                                contract_share='measurement-window base charged_us share, excludes WC extras'),
        operational=dict(gpu_inventory_unchanged=before['gpu']==after['gpu'],
                         allocatable_unchanged=before['allocatable']==after['allocatable'],
                         npu_process_table_before=before['processes'], npu_process_table_after=after['processes'],
                         production_configs_modified=after['production_configs_modified']),
        limits=['request generate-call elapsed-time accounting, including runtime wait; not exclusive NPU busy time',
                '40-second offered-load windows include drain; n=3, descriptive results, no population significance claim',
                'local containerd diagnostic; current K8s NPU resource advertising is unavailable; no K8s integration certification',
                'nominal denominator is a causal control, not a production fix: idle-return entitlement regression is not addressed',
                '8PE hardware controls not rerun; original8PE WC extras are historical evidence only'])
    (root/'summary.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
    with (root/'measurements.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig, axes=plt.subplots(1,2,figsize=(12,4.8),layout='constrained')
    x=np.arange(3)
    for offset,key,label,color in [(-.18,'contract_share','Contract-accounted','#007fa8'),(.18,'total_accounted_share','Total incl. WC','#64b7ce')]:
        means=[summaries[n][key]['mean'] for n in names]; sd=[summaries[n][key]['stdev'] for n in names]
        axes[0].bar(x+offset,means,.34,yerr=sd,capsize=4,label=label,color=color)
        for j,n in enumerate(names):
            vals=summaries[n][key]['values'];axes[0].scatter([j+offset-.07,j+offset,j+offset+.07],vals,s=20,color='#163f55',zorder=5)
    for offset,key,label,color in [(-.18,'accepted_share','Accepted credit','#93c6b1'),(.18,'denominator_share','Denom. increment (window)','#32719b')]:
        axes[1].bar(x+offset,[summaries[n][key]['mean'] for n in names],.34,label=label,color=color)
    for ax in axes:
        ax.axhline(70,color='#c54f50',linestyle='--',linewidth=1.5,label='Requested A share: 70%')
        ax.set_ylim(0,100);ax.set_xticks(x,labels);ax.set_ylabel('A share (%)');ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True)
        ax.legend(fontsize=8,loc='lower right')
    axes[0].set_title('Actual LLM execution accounting\nmean ± sample SD; dots = 3 repetitions')
    axes[1].set_title('Where the requested ratio is lost\nsame source, image, input and 4PE block')
    fig.suptitle('KETRIS NPU time-share diagnosis — real RNGD controls',fontsize=14,fontweight='bold')
    fig.savefig(root/'comparison.png',dpi=180);fig.savefig(root/'comparison.pdf');plt.close(fig)
    table=''.join('<tr><td>'+html.escape(n)+'</td>'+''.join(f'<td>{summaries[n][k]["mean"]:.2f}</td>' for k in
                  ['requested_share','accepted_share','denominator_share','contract_share','total_accounted_share','wc_extra_percent','total_rps'])+'</tr>' for n in names)
    page=f'''<!doctype html><meta charset="utf-8"><title>KETRIS NPU 시간 제어 원인 검증</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;color:#173f56;line-height:1.7}}h1{{border-bottom:5px solid #0085b2}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:10px;border:1px solid #d6e3e9}}th{{background:#e9f5fa}}img{{width:100%}}code{{background:#eef4f6}}.note{{background:#fff3d9;padding:16px}}</style>
<h1>NPU 시간 분할 비율 오류: 실제 RNGD 원인 검증</h1>
<p>Qwen2.5-0.5B-Instruct · 4PE · 워커 2개 · A/B 동시 요청 각 4개 · max_tokens=64 · 동일 입력 · 조건별 새 컨텍스트 3회 · 준비 20초 + 부하 40초와 드레인.</p>
<p>원본의 A 계약 계상시간 비중은 <b>{original_mean:.2f}%</b>, 분모만 변경한 대조군은 <b>{control_mean:.2f}%</b>였다. 변경 효과는 <b>{control_mean-original_mean:+.2f}%p</b>다. 정확한 반복별 수치와 소스 해시는 연결된 원본 파일에 있다.</p>
<img src="comparison.png" alt="Actual experimental comparison">
<table><tr><th>조건</th><th>요청 크레딧 A%</th><th>수락 크레딧 A%</th><th>분모 증가분 A%</th><th>계약 계상 A%</th><th>총 계상 A%</th><th>WC 추가시간%</th><th>총 req/s</th></tr>{table}</table>
<p>분모 증가분은 측정 창의 Δgranted 비율이다. 실제 선택은 누적 charged/granted로 계산한다. 처리량은 참고값이며 이 실험으로 처리량 개선 효과를 주장하지 않는다.</p>
<h2>코드 경로</h2><p><code>time_add → credit = min(credit + add, cap) → granted += 실제 수락량 → min(charged / granted)</code></p>
<p>원본은 잔액 상한에서 버린 크레딧을 선택 분모에서도 제거한다. 높은 몫을 요청한 A의 크레딧이 더 많이 버려지면 70:30 의도가 분모에서 약해진다. 초당 요청 실행시간 예산은 3초분인데 워커 2개의 전체 용량은 최대 2초분이며, 실행 중 비용은 완료 전까지 차감되지 않는다. 이번 대조군은 분모만 전체 지급량으로 계산했고, 잔액 상한과 WC 회계는 그대로 유지했다.</p>
<div class="note">분모 대조군은 원인을 분리하기 위한 실험이다. 운영 수정으로 배포하지 않았다. 유휴 테넌트의 과거 지급량 누적으로 복귀 후 독식하는 기존 문제가 있어 별도 정책 검증이 필요하다. 이 수치는 겹쳐 실행되는 generate 호출의 경과시간 회계이며 NPU의 독점 busy time이 아니다. 이번 실제 장치 대조는 4PE에 한정된다.</div>
<p>운영 설정 변경 없음. 실험 전후 GPU 인벤토리 일치: {evidence['operational']['gpu_inventory_unchanged']}. 노드 자원 광고량 일치: {evidence['operational']['allocatable_unchanged']}.</p>
<p><a href="summary.json">집계와 한계 JSON</a> · <a href="measurements.csv">반복별 CSV</a> · <a href="measurements.json">원본 측정 JSON</a> · <a href="source-hashes.json">소스 해시</a> · <a href="protocol.json">실험 조건</a></p>'''
    (root/'report.html').write_text(page)
    manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*-r*/measurement.json')}
    manifest['summary.json']=hashlib.sha256((root/'summary.json').read_bytes()).hexdigest()
    (root/'evidence-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(dict(summary=summaries,operational=evidence['operational']),ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory');main(parser.parse_args().directory)
