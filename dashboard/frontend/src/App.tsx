import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {get} from './api';
import type {Detail,Experiment,Frame,NodeInfo,Snapshot} from './types';
import {conditionLabels,experimentLabel,kst,markStale,windowLabel,gpuConnectionsKnown} from './presentation';
import {DeviceMap} from './components/DeviceMap';
import {WorkloadTable} from './components/WorkloadTable';
import {TimeSeries} from './components/TimeSeries';
import {ReplayControls} from './components/ReplayControls';
import {HistoryControls} from './components/HistoryControls';
import type {HistoryPoint} from './components/HistoryControls';
import {WorkloadDetail} from './components/WorkloadDetail';
import {EvidenceDrawer} from './components/EvidenceDrawer';
import './resource-view.css';

export function App(){
 const [mode,setMode]=useState<'live'|'replay'>('live');
 const [nodes,setNodes]=useState<NodeInfo[]>([]),[node,setNode]=useState('');
 const [live,setLive]=useState<Snapshot|null>(null),[history,setHistory]=useState<HistoryPoint[]>([]),[pinned,setPinned]=useState<HistoryPoint|null>(null);
 const [error,setError]=useState(''),[historyError,setHistoryError]=useState('');
 const [listError,setListError]=useState(''),[listLoading,setListLoading]=useState(true),[listRetry,setListRetry]=useState(0);
 const [experiments,setExperiments]=useState<Experiment[]>([]),[experiment,setExperiment]=useState('');
 const [detail,setDetail]=useState<Detail|null>(null),[allFrames,setFrames]=useState<Frame[]>([]);
 const [condition,setCondition]=useState(''),[index,setIndex]=useState(0),[playing,setPlaying]=useState(false),[speed,setSpeed]=useState(1);
 const [evidence,setEvidence]=useState<unknown|null>(null),[selectedId,setSelectedId]=useState<string|null>(null);
 const [selectedPartitionId,setSelectedPartitionId]=useState<string|null>(null);
 const [timeLoading,setTimeLoading]=useState(false),[timeError,setTimeError]=useState('');
 const timeRequest=useRef(0),timeContext=useRef('');timeContext.current=JSON.stringify([mode,node]);
 useEffect(()=>{timeRequest.current++;setTimeLoading(false);setTimeError('');setSelectedPartitionId(null)},[mode,node]);
 const evidenceRequest=useRef(0),evidenceContext=useRef('');
 const context=JSON.stringify([mode,experiment,condition,index]);evidenceContext.current=context;
 useEffect(()=>{evidenceRequest.current++;setEvidence(null)},[mode,experiment,condition,index]);
 useEffect(()=>{
  let active=true;setListLoading(true);setListError('');
  get<Experiment[]>('experiments').then(data=>{if(!active)return;setExperiments(data);const chosen=data.find(e=>e.kind==='mig')||data[0];setExperiment(previous=>data.some(e=>e.id===previous)?previous:chosen?.id||'');setListLoading(false)}).catch(()=>{if(active){setListLoading(false);setListError('실험 목록을 가져오지 못했습니다.')}});
  return()=>{active=false};
 },[mode,listRetry]);
 useEffect(()=>{
  if(mode!=='live')return;
  let active=true;
  const poll=async()=>{try{
   const data=await get<NodeInfo[]>('nodes');if(!active)return;setNodes(data);
   const current=node||data[0]?.name;
   if(!current){setLive(null);setError('관측 노드 연결 대기 중');return}
   if(!node)setNode(current);
   const s=await get<Snapshot>('nodes/'+encodeURIComponent(current)+'/snapshot');
   if(active){setLive(previous=>previous?.node===s.node&&previous.captured_at&&s.captured_at&&Date.parse(previous.captured_at)>Date.parse(s.captured_at)?previous:s);setError('')}
  }catch{if(active){setLive(v=>v?markStale(v):null);setError('실시간 수집 연결이 끊겼습니다. 마지막 값의 시각과 출처를 확인하세요.')}}};
  poll();const timer=setInterval(poll,2000);return()=>{active=false;clearInterval(timer)};
 },[mode,node]);
 useEffect(()=>{
  if(mode!=='live'||!node)return;
  let active=true,request=0,accepted=0;
  const poll=async()=>{const seq=++request;try{
   const rows=await get<{id:number;snapshot:Snapshot}[]>('nodes/'+encodeURIComponent(node)+'/history?recent=true&limit=500');
   if(active&&seq>=accepted){accepted=seq;setHistory(rows.map(row=>({id:'history-'+row.id,snapshot:row.snapshot})));setHistoryError('')}
  }catch{if(active)setHistoryError('관측 기록을 갱신하지 못했습니다. 현재 장치 조회는 계속합니다.')}};
  poll();const timer=setInterval(poll,10000);return()=>{active=false;clearInterval(timer)};
 },[mode,node]);
 useEffect(()=>{
  if(!experiment||mode!=='replay')return;
  let active=true;setError('');setPlaying(false);setDetail(null);setFrames([]);setSelectedId(null);setSelectedPartitionId(null);
  get<Detail>('experiments/'+encodeURIComponent(experiment)).then(async d=>{
   const f=await get<Frame[]>('experiments/'+encodeURIComponent(experiment)+'/frames?limit=500');if(!active)return;
   setDetail(d);setFrames(f);setCondition(f.find(f=>f.interval.condition==='combined50_50_time3_1')?.interval.condition||f[0]?.interval.condition||'');setIndex(0);
  }).catch(()=>{if(active&&mode==='replay')setError('실험 증빙을 가져오지 못했습니다.')});
  return()=>{active=false};
 },[experiment,mode]);
 const conditions=useMemo(()=>[...new Set(allFrames.map(f=>f.interval.condition||''))],[allFrames]);
 const frames=useMemo(()=>allFrames.filter(f=>f.interval.condition===condition),[allFrames,condition]);
 useEffect(()=>{
  if(!playing||mode!=='replay'||!frames.length)return;
  const frame=frames[index];if(!frame){setPlaying(false);setIndex(0);return}
  const duration=Math.max(1,(frame.interval.end??20)-(frame.interval.start??0));
  const timer=setTimeout(()=>{if(index>=frames.length-1)setPlaying(false);else setIndex(index+1)},duration*1000/speed);
  return()=>clearTimeout(timer);
 },[playing,mode,frames,index,speed]);
 const points=useMemo(()=>{
  const out=history.filter(point=>point.snapshot.node===node);
  if(live?.node===node&&!out.some(p=>p.snapshot.captured_at===live.captured_at))out.push({id:'head-'+live.captured_at,snapshot:live});
  if(pinned&&!out.some(p=>p.id===pinned.id))out.push(pinned);
  return out.sort((a,b)=>Date.parse(a.snapshot.captured_at||'')-Date.parse(b.snapshot.captured_at||''));
 },[history,live,node,pinned]);
 const historyIndex=pinned?points.findIndex(p=>p.id===pinned.id):points.length-1;
 const snapshot=mode==='live'?(pinned?.snapshot||live):frames[index]?.snapshot||null;
 const selectedWorkload=snapshot?.workloads.find(w=>w.id===selectedId);
 const selectedPartition=snapshot?.partitions.find(p=>p.id===(selectedWorkload?.partition_id||selectedPartitionId));
 const selectedExperiment=experiments.find(e=>e.id===experiment),summary=detail?.summary[condition];
 const mean=summary?.charged_share_a_mean??summary?.workers?.A?.mean_charged_share??summary?.tenants?.A?.contract_time_share;
 const throughput=summary?.total_ips_mean??summary?.mean_total_rps??(summary?.workers?Object.values(summary.workers).reduce((sum:number,v:any)=>sum+(v.mean_iters_per_second||0),0):null);
 const selectionLabel=mode==='replay'?(frames[index]?windowLabel(frames[index].interval):'측정 창 선택'):(snapshot?kst(snapshot.captured_at):'관측 대기');
 function changeMode(next:'live'|'replay'){if(next===mode)return;setPlaying(false);setError('');setSelectedId(null);setPinned(null);setMode(next)}
 function changeNode(next:string){setNode(next);setLive(null);setHistory([]);setPinned(null);setSelectedId(null);setHistoryError('')}
 function followLatest(){timeRequest.current++;setTimeLoading(false);setTimeError('');setPinned(null);setSelectedId(null);setSelectedPartitionId(null)}
 async function chooseTime(atKst:string){
  const request=++timeRequest.current,selectedContext=timeContext.current;
  setTimeLoading(true);setTimeError('');
  try{
   const at=new Date(atKst+'+09:00').toISOString();
   const rows=await get<{id:number;snapshot:Snapshot}[]>('nodes/'+encodeURIComponent(node)+'/history?recent=true&limit=1&at='+encodeURIComponent(at));
   if(request!==timeRequest.current||selectedContext!==timeContext.current)return;
   if(!rows.length){setTimeError('지정 시각 이전의 보관된 관측 기록이 없습니다.');return}
   setPinned({id:'history-'+rows[0].id,snapshot:rows[0].snapshot});setSelectedId(null);setSelectedPartitionId(null);
  }catch{if(request===timeRequest.current&&selectedContext===timeContext.current)setTimeError('해당 시점의 기록을 조회하지 못했습니다.')}
  finally{if(request===timeRequest.current&&selectedContext===timeContext.current)setTimeLoading(false)}
 }
 const closeEvidence=useCallback(()=>{evidenceRequest.current++;setEvidence(null)},[]);
 async function openEvidence(){
  const request=++evidenceRequest.current,selectedContext=context;
  try{const ids=frames[index]?.evidence_ids||[],data=await Promise.all(ids.map(id=>get('evidence/'+encodeURIComponent(id))));if(request===evidenceRequest.current&&selectedContext===evidenceContext.current)setEvidence(data)}
  catch{if(request===evidenceRequest.current&&selectedContext===evidenceContext.current)setError('실험 증빙을 가져오지 못했습니다.')}
 }
 return <div className="app resource-app">
  <header className="topbar"><div className="brand">KETRIS<span>RESOURCE OBSERVATORY</span></div><div className="mode-switch"><button className={mode==='live'?'active':''} onClick={()=>changeMode('live')}>실시간 조회</button><button className={mode==='replay'?'active':''} onClick={()=>changeMode('replay')}>실험 재생</button></div><span className="read-only">조회 전용</span></header>
  <main><div className="page-heading"><div><p className="eyebrow">SPATIAL + TEMPORAL RESOURCE SHARING</p><h1>KETRIS 자원 관측</h1><p>한 시점의 GPU / MIG · NPU 자원 상태</p></div><div className="heading-status"><span className={'status-pill '+(mode==='replay'||pinned?'replay':'live')}><i/>{mode==='replay'?(playing?'재생 중':'재생 일시정지'):pinned?'관측 기록 선택':'최신 관측'}</span><p>{mode==='replay'?'실제 측정 창의 저장 기록':snapshot?kst(snapshot.captured_at):'관측 데이터 연결 중'}</p></div></div>
  <section className="toolbar">{mode==='live'?<label>노드<select aria-label="노드" value={node} onChange={e=>changeNode(e.target.value)}>{nodes.length?nodes.map(n=><option key={n.name} value={n.name}>{n.name}</option>):<option>연결 대기</option>}</select></label>:<>
   <label>실험<select aria-label="실험" value={experiment} onChange={e=>setExperiment(e.target.value)}>{experiments.map(e=><option key={e.id} value={e.id}>{experimentLabel(e)}</option>)}</select></label>
   <label>조건<select aria-label="조건" value={condition} onChange={e=>{setPlaying(false);setIndex(0);setSelectedId(null);setSelectedPartitionId(null);setCondition(e.target.value)}}>{conditions.map(c=><option key={c} value={c}>{conditionLabels[c]||c}</option>)}</select></label>
   <span className="origin-badge">{String(detail?.metadata.origin||'원본 실험 로딩 중')}</span>
  </>}{mode==='replay'&&<button className="evidence-button" aria-label="원본 증빙" onClick={openEvidence} disabled={!frames.length}>원본 증빙 ↗</button>}</section>
  {mode==='replay'&&listError&&<div className="error-banner" role="status">{listError}<button onClick={()=>setListRetry(v=>v+1)}>실험 목록 다시 시도</button></div>}
  {error&&<div className="error-banner" role="status">{error}</div>}
  {mode==='replay'&&selectedExperiment?.kind==='rngd'&&condition!=='space_only'&&<div className="verdict">70:30 목표 정밀도 재현 실패<span>원본 계상시간 비중을 표시하며 완료 지연을 TTFT와 구분합니다.</span></div>}
  {snapshot?<div className="resource-layout"><section className="resource-board" role="region" aria-label="선택한 시점의 자원 상태"><div className="resource-board-heading"><h2>선택한 시점의 자원 상태</h2><div><span className="selected-time">{selectionLabel}</span><span className="tag">{mode==='replay'?'측정 창 기록':pinned?'관측 시점 기록':'현재 관측'}</span></div></div>
   <DeviceMap snapshot={snapshot} selectedId={selectedId} selectedPartitionId={selectedPartitionId} onSelect={id=>{setSelectedId(id);setSelectedPartitionId(null)}} onPartitionSelect={id=>{setSelectedId(null);setSelectedPartitionId(id)}}/>
   <p className="board-caption">{mode==='replay'?'원본 측정 창의 결과입니다. 순간 커널 실행·대기 상태를 재구성하지 않습니다.':'Pod·예산 적용 상태와 드라이버 관측을 표시합니다. 순간 커널·요청 실행 여부는 별도 이벤트 측정이 필요합니다.'}</p>
  </section><div className="resource-sidebar"><WorkloadDetail workload={selectedWorkload} partition={selectedPartition} workloadCount={gpuConnectionsKnown(snapshot)?snapshot.workloads.filter(w=>w.partition_id===selectedPartition?.id).length:undefined} onClose={()=>{setSelectedId(null);setSelectedPartitionId(null)}}/>
   {mode==='replay'&&<section className="benchmark-panel"><h2>기존 실측 · 반복 평균</h2><div><span>A 계상시간 비중</span><strong>{typeof mean==='number'?(mean*100).toFixed(3)+'%':'—'}</strong></div><div><span>합산 처리량</span><strong>{typeof throughput==='number'?throughput.toFixed(3):'—'}<small>{selectedExperiment?.kind==='rngd'?'req/s':'iter/s'}</small></strong></div><p>현재 선택 창의 값과 반복 평균을 구분합니다.</p></section>}
  </div></div>:<section className="empty big"><h2>{mode==='live'?'실시간 관측 연결 대기':listError||error?'실험 기록 연결 실패':!listLoading&&!experiments.length?'등록된 실험 기록 없음':'실험 기록 로딩 중'}</h2><p>확인된 자원 데이터가 도착하면 해당 시점의 자원판을 표시합니다.</p></section>}
  {mode==='live'?<><HistoryControls points={points} index={historyIndex} pinned={!!pinned} onSelect={point=>{timeRequest.current++;setTimeLoading(false);setTimeError('');setPinned(point)}} onLatest={followLatest} onAt={chooseTime} busy={timeLoading}/>{timeError&&<p className="history-warning" role="status">{timeError}</p>}{historyError&&<p className="history-warning" role="status">{historyError}</p>}</>:<ReplayControls frames={frames} index={index} setIndex={setIndex} playing={playing} setPlaying={setPlaying} speed={speed} setSpeed={setSpeed}/>}
  {snapshot&&<details className="research-details"><summary>입출력 · 실측 비교 · 관측 출처</summary><div className="lower-grid"><TimeSeries frames={mode==='replay'?frames:[]} snapshot={snapshot} index={index}/><section className="panel provenance"><div className="panel-heading"><h2>관측 출처</h2></div>{Object.entries(snapshot.source_status).map(([source,status])=><div className="source-row" key={source}><i className={'source-dot '+status.status}/><div><strong>{source}</strong><p>{status.reason||kst(status.last_success)}</p></div><span>{status.status==='ok'?'관측됨':status.status==='stale'?'오래된 값':'미연결'}</span></div>)}</section></div><WorkloadTable snapshot={snapshot}/></details>}
  <footer><strong>KETRIS</strong><span>공간 상한 / PE 배정 · 선택한 시점의 관측</span><span>UTC 저장 / KST 표시</span></footer>
  </main>{evidence!==null&&<EvidenceDrawer data={evidence} onClose={closeEvidence}/>}
 </div>
}
