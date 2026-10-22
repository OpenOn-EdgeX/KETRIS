import type {ObservedValue,Quality,Workload,Frame,Experiment,Snapshot,Partition} from './types';
export function gpuConnectionsKnown(s:Snapshot){
 const runtime=s.source_status.runtime;
 return s.mode==='live'&&['gpu','kubernetes','checkpoint'].every(k=>s.source_status[k]?.status==='ok')&&
  (runtime?.status==='ok'||(runtime?.status==='unavailable'&&runtime.reason==='no active MIG runtime export (fresh lookup)'));
}
export function assignedPe(w:Workload,partition?:Partition):ObservedValue|undefined{
 if(w.applied.pe_indices)return w.applied.pe_indices;
 if(partition?.historical&&partition.pe_indices.length)return {value:partition.pe_indices.join(','),source:'archive:partition.pe_indices',quality:'configured',reason:'원본 실험의 PE 배정이며 드라이버 점유 측정은 아닙니다.'};
}
export function cudaVerification(w:Workload,selected?:ObservedValue){
 const actual=w.applied.actual_cuda_uuid;
 if(actual?.quality==='stale'||selected?.quality==='stale'||w.state==='stale')return {verified:false,label:'UUID 관측 오래됨'};
 if(actual?.quality==='ambiguous'||selected?.quality==='ambiguous')return {verified:false,label:'CUDA UUID 불명확'};
 if(actual?.quality!=='observed'||actual.value==null)return {verified:false,label:'실제 CUDA UUID 미관측'};
 if(selected?.value==null||!['observed','configured'].includes(selected.quality))return {verified:false,label:'선택 MIG UUID 미관측'};
 const verified=actual.value===selected.value;
 return {verified,label:verified?'일치 확인':'UUID 불일치'};
}
export const palette=['#0085B3','#C83F86','#398A6F','#8263B7','#D99321','#516A82'];
export function workloadColor(w:Pick<Workload,'id'|'name'>){
 if(w.id.startsWith('archive:')&&/^[A-D]$/.test(w.name))return palette[w.name.charCodeAt(0)-65];
 const key=w.id.includes(':process:')?w.id:w.id.split(':').slice(0,3).join(':')+(w.id.includes(':gate:')?':'+w.id.split(':gate:')[1]:'');
 let hash=0;for(const c of key)hash=(hash*31+c.charCodeAt(0))>>>0;return palette[hash%palette.length];
}
export function valueLabel(q:Quality){return {observed:'실측',configured:'설정',derived:'계산',stale:'오래된 값',unavailable:'미관측',ambiguous:'불명확'}[q]}
export function formatValue(v?:ObservedValue,digits=3){
 if(v?.value==null)return '—';if(typeof v.value==='boolean')return v.value?'예':'아니오';if(typeof v.value==='string')return v.value;
 const n=v.unit==='fraction'?v.value*100:v.value;const unit=v.unit==='fraction'?'%':v.unit==='%'?'%':v.unit?' '+v.unit:'';
 return (Number.isInteger(n)?String(n):n.toFixed(digits))+unit;
}
export function windowLabel(v:Frame['interval']){return `반복 ${v.repeat??'—'} · ${(v.start??0).toFixed(1)}–${(v.end??0).toFixed(1)}s`}
export function experimentLabel(e:Experiment){const date=e.name.match(/(20\d{6})/)?.[1];return `${e.kind.toUpperCase()} · ${date?date.slice(0,4)+'.'+date.slice(4,6)+'.'+date.slice(6):'과거 실험'}`}
export const conditionLabels:Record<string,string>={solo100:'단독 · MPS 100%',solo25:'단독 · MPS 25%',solo50:'단독 · MPS 50%',solo75:'단독 · MPS 75%',space50_50:'공간 · 50:50',space75_25:'공간 · 75:25',time1_1:'시간 · 1:1',time3_1:'시간 · 3:1',combined50_50_time3_1:'공간 50:50 + 시간 3:1',space_only:'RNGD 공간 · 4PE + 4PE',spacetime_70_30:'RNGD 4PE 내부 시간 · 70:30',time_only_70_30:'RNGD 8PE 시간 · 70:30'};
export function kst(at?:string|null){return at?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(at))+' KST':'원본 절대 시각 미기록'}
export function stateLabel(state:string){return ({Pending:'대기 중',Running:'Pod 실행 중',allocated:'할당 대기',bound:'장치 확인',armed:'예산 적용',failed:'검증 실패',completed:'종료됨',measured:'측정 완료'} as Record<string,string>)[state]||state}
export function markStale(snapshot:Snapshot):Snapshot{const s:Snapshot=JSON.parse(JSON.stringify(snapshot));for(const entity of [...s.devices,...s.partitions,...s.workloads])for(const key of ['metrics','requested','applied','observed'])for(const value of Object.values((entity as any)[key]||{}) as ObservedValue[])if(value.value!==null){value.quality='stale';value.reason='viewer could not refresh; last successful value'};for(const status of Object.values(s.source_status))status.status='stale';return s}
