import type {CSSProperties} from 'react';
import type {Snapshot,Device,Workload,ObservedValue} from '../types';
import {workloadColor,stateLabel,assignedPe,cudaVerification,gpuConnectionsKnown} from '../presentation';
import {Value} from './Value';

function peIds(value:unknown){return typeof value==='string'&&value.length?value.split(',').map(Number).filter(Number.isInteger):[]}
function WorkloadBlock({w,selected,onSelect,selectedUuid,pe}:{w:Workload;selected:boolean;onSelect?:(id:string)=>void;selectedUuid?:ObservedValue;pe?:ObservedValue}){
 const color=workloadColor(w),cap=w.applied.mps_percent;
 const percent=typeof cap?.value==='number'?Math.max(0,Math.min(100,cap.value)):null;
 const actual=w.applied.actual_cuda_uuid,{verified,label}=cudaVerification(w,selectedUuid);
 return <button className={'workload-block'+(selected?' selected':'')+(w.state==='stale'?' stale':'')} style={{'--workload-color':color} as CSSProperties} onClick={()=>onSelect?.(w.id)} aria-label={w.name+' 상세 보기'} aria-pressed={selected}>
  <span className="block-heading"><strong><i className="dot" style={{background:color}}/>{w.name}</strong><span className="block-state">{stateLabel(w.state)}</span></span>
  <span className="block-facts"><span>{cap?'공간 상한':'PE 배정'} <Value value={cap||pe} quality/></span>{w.applied.reported_sm_count&&<span>CUDA 보고 <Value value={w.applied.reported_sm_count}/></span>}</span>
  {cap&&<><span className="cap-rail"><span style={{width:percent===null?'0':percent+'%',background:color}}/></span><span className="cap-scale"><span>0%</span><span>100%</span></span></>}
  <span className="block-time"><span>시간 설정 <Value value={w.applied.time_share||w.requested.time_ratio}/></span><span>계상 비중 <Value value={w.observed.charged_share}/></span></span>
  {cap&&<span className={'verification block-verification '+(verified?'verified':'unknown')} title={String(actual?.value||'CUDA API 관측 없음')}>{label}</span>}
 </button>
}
function GpuCard({d,snapshot,selectedId,selectedPartitionId,onSelect,onPartitionSelect}:{d:Device;snapshot:Snapshot;selectedId?:string|null;selectedPartitionId?:string|null;onSelect?:(id:string)=>void;onPartitionSelect?:(id:string)=>void}){
 const parts=snapshot.partitions.filter(p=>p.device_id===d.id);
 const selectedPartition=snapshot.workloads.find(w=>w.id===selectedId)?.partition_id||selectedPartitionId;
 const historical=snapshot.mode==='replay',source=snapshot.source_status.gpu?.status;
 const membershipKnown=gpuConnectionsKnown(snapshot);
 const scope=historical?'부분 기록 · 다른 MIG 상태 미기록':source==='ok'?'선택 시점 드라이버 목록':source==='stale'?'MIG 목록 오래됨':'MIG 목록 출처 미확인';
 const memoryKnown=parts.length>0&&parts.every(p=>typeof p.metrics.memory_mib?.value==='number'&&Number(p.metrics.memory_mib.value)>0);
 const minimum=memoryKnown?Math.min(...parts.map(p=>Number(p.metrics.memory_mib.value))):1;
 return <article className="resource-device gpu-device whole-gpu"><header className="device-head"><div className="device-symbol">GPU</div><div><h3>{d.name}</h3><p>{d.node}</p></div><span className="gpu-inventory-scope">{scope}</span></header>
  <div className="device-facts"><span>전체 VRAM <Value value={d.metrics.memory_used_mib}/> / <Value value={d.metrics.memory_mib}/></span><span>MIG <Value value={d.metrics.mig_mode}/></span><span>기록된 파티션 {parts.length}개</span></div>
  <div className="gpu-map-heading"><strong>GPU 전체 자원판</strong><span>모든 기록된 MIG · 워크로드 배치</span></div>
  <div className="gpu-partition-map" role="group" aria-label="GPU 전체 파티션 지도">{parts.map((part,index)=>{
   const workloads=snapshot.workloads.filter(w=>w.partition_id===part.id);
   const stale=source==='stale'||Object.values(part.metrics).some(v=>v.quality==='stale');
   const weight=memoryKnown?Number(part.metrics.memory_mib.value)/minimum:1;
   return <section key={part.id} className={'gpu-partition-tile'+(!workloads.length?(membershipKnown?' no-workloads':' unknown-connections'):'')+(selectedPartition===part.id?' selected-partition':'')+(stale?' stale':'')} style={{'--partition-weight':weight} as CSSProperties} role="region" aria-label={part.name+' · 파티션 '+(index+1)}>
    <header className="mig-block-heading"><button onClick={()=>onPartitionSelect?.(part.id)} aria-label={part.name+' 자원 상세 · '+(index+1)} aria-pressed={selectedPartition===part.id}><strong>{part.name}</strong><small>파티션 {index+1} · {part.kind==='mig'?'MIG 내부 공유':'전체 GPU'} · {membershipKnown?'연결 '+workloads.length+'개':workloads.length?'기록된 연결 '+workloads.length+'개':'연결 수 미관측'}</small></button>{part.historical&&<span className="tag historical">과거 파티션</span>}</header>
    {part.metrics.mig_uuid&&<p className="uuid" title={String(part.metrics.mig_uuid.value)}>{part.metrics.mig_uuid.value}</p>}
    <div className="partition-spec"><span>용량 <Value value={part.metrics.profile_sm}/></span><span><Value value={part.metrics.memory_mib}/></span></div>
    <div className="geometry-workloads">{workloads.map(w=><WorkloadBlock key={w.id} w={w} selected={selectedId===w.id} onSelect={onSelect} selectedUuid={part.metrics.mig_uuid}/>)}</div>
    {!workloads.length&&<div className="empty-mig-state"><strong>{membershipKnown?'연결된 워크로드 없음':'워크로드 연결 미관측'}</strong><span>{membershipKnown?'KETRIS 연결 기준 · 장치 실행 점유는 별도 관측':historical?'전체 연결 목록 미기록':'연결 출처 미연결 / 오래됨'}</span></div>}
   </section>
  })}
  {historical&&<div className="gpu-unknown-region"><strong>전체 GPU 구성 미기록</strong><span>다른 MIG의 존재·배정 여부를 원본 기록으로 확인할 수 없습니다.</span></div>}
  {!parts.length&&<div className="empty-mig-state"><strong>기록된 MIG 없음</strong><span>{source==='ok'?'선택 시점 드라이버 목록 기준':'목록 미관측 · 비어 있다고 판단하지 않습니다.'}</span></div>}
  </div>
  <div className="gpu-map-legend"><span><i className="legend-assigned"/>워크로드 연결</span><span><i className="legend-empty"/>연결 없음</span><span><i className="legend-unknown"/>미기록 / 오래됨</span></div>
  <p className="caption gpu-map-caption">블록은 논리 자원 배치도입니다.{memoryKnown?' MIG 크기는 메모리 용량에 따른 상대 배치입니다.':' 용량이 미기록된 블록은 같은 크기로 배치합니다.'} MPS 상한은 전용 SM 소유권이 아니며, 상한의 나머지가 빈 자원이라는 뜻도 아닙니다.</p>
 </article>
}
function NpuCard({d,snapshot,selectedId,onSelect}:{d:Device;snapshot:Snapshot;selectedId?:string|null;onSelect?:(id:string)=>void}){
 const parts=snapshot.partitions.filter(p=>p.device_id===d.id),workloads=snapshot.workloads.filter(w=>parts.some(p=>p.id===w.partition_id));
 const count=typeof d.metrics.pe_count?.value==='number'?d.metrics.pe_count.value:Math.max(0,...parts.flatMap(p=>p.pe_indices.map(v=>v+1)));
 const historical=snapshot.mode==='replay';
 const known=snapshot.source_status.npu?.status==='ok'&&!workloads.some(w=>w.observed.occupied_pe&&w.observed.occupied_pe.quality!=='observed');
 return <article className="resource-device npu-device"><header className="device-head"><div className="device-symbol npu">NPU</div><div><h3>{d.name}</h3><p>{d.node}</p></div></header>
  <div className="device-facts"><span>장치 용량 <Value value={d.metrics.pe_count} quality/></span><span>{historical?'과거 PE 배치':'PE 배정 / 드라이버 프로세스 점유'}</span></div>
  <div className="pe-map" role="group" aria-label="NPU PE 지도">{Array.from({length:count},(_,i)=>{
   const assigned=workloads.filter(w=>{const ids=peIds(w.applied.pe_indices?.value),part=parts.find(p=>p.id===w.partition_id);return (ids.length?ids:part?.historical?part.pe_indices:[]).includes(i)});
   const occupants=historical||!known?[]:workloads.filter(w=>w.observed.occupied_pe?.quality==='observed'&&peIds(w.observed.occupied_pe.value).includes(i));
   const colors=[...new Set((historical?assigned:occupants).map(workloadColor))];
   const assignment=assigned.length?(historical?'과거 배정: ':'배정: ')+assigned.map(w=>w.name+(w.applied.pe_indices?.quality==='stale'?' (오래됨)':'')).join(' / '):'배정 기록 없음';
   const occupancy=occupants.length?'점유: '+occupants.map(w=>w.name).join(' / '):known?'점유 없음':'점유 미확인';
   return <div key={i} className={'pe'+(assigned.length?' assigned':'')+(occupants.length?' occupied':'')} style={{borderColor:colors[0],background:colors.length?'linear-gradient(135deg,'+colors[0]+'18,'+colors.at(-1)+'28)':undefined}} title={assignment+(historical?'':' · '+occupancy)}><b>PE {i}</b><small>{assignment}</small>{!historical&&<small>{occupancy}</small>}</div>
  })}</div>
  {!count&&<div className="empty compact">PE 개수 미관측</div>}
  <div className="npu-workloads">{workloads.map(w=><WorkloadBlock key={w.id} w={w} selected={selectedId===w.id} onSelect={onSelect} pe={assignedPe(w,parts.find(p=>p.id===w.partition_id))}/>)}</div>
  <p className="caption">PE 배정과 프로세스 점유를 구분합니다. 점유는 PE 사용률이 아닙니다. 요청의 순간 실행·대기는 별도 이벤트 관측이 필요합니다.</p>
 </article>
}
export function DeviceMap({snapshot,selectedId,selectedPartitionId,onSelect,onPartitionSelect}:{snapshot:Snapshot;selectedId?:string|null;selectedPartitionId?:string|null;onSelect?:(id:string)=>void;onPartitionSelect?:(id:string)=>void}){
 return <div className={'device-grid resource-grid'+(snapshot.devices.length===1?' single':'')}>
  {snapshot.devices.map(d=>d.kind==='gpu'?<GpuCard key={d.id} d={d} snapshot={snapshot} selectedId={selectedId} selectedPartitionId={selectedPartitionId} onSelect={onSelect} onPartitionSelect={onPartitionSelect}/>:<NpuCard key={d.id} d={d} snapshot={snapshot} selectedId={selectedId} onSelect={onSelect}/>)}
  {!snapshot.devices.length&&<div className="empty">장치 관측 대기 중 · 드라이버 미연결 데이터는 임의 값으로 채우지 않습니다.</div>}
 </div>
}
