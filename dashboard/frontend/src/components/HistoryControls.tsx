import {useState} from 'react';
import type {Snapshot} from '../types';
import {kst} from '../presentation';
export interface HistoryPoint{id:string;snapshot:Snapshot}
export function HistoryControls({points,index,pinned,onSelect,onLatest,onAt,busy=false}:{points:HistoryPoint[];index:number;pinned:boolean;onSelect:(point:HistoryPoint)=>void;onLatest:()=>void;onAt?:(value:string)=>void;busy?:boolean}){
 const [at,setAt]=useState('');
 const choose=(next:number)=>{if(points[next])onSelect(points[next])};
 return <section className="time-selection" aria-label="관측 시점 선택"><div className="time-heading"><h2>시간 선택</h2><span>{pinned?'과거 관측 · 선택 고정':'최신 관측 · 자동 갱신'}</span><button onClick={onLatest} className={pinned?'':'following'} aria-label="최신 관측 따라가기">최신 관측 따라가기</button></div>
  {onAt&&<form className="history-time-picker" onSubmit={e=>{e.preventDefault();if(at)onAt(at)}}><label>GPU 상태 시점 (KST)<input aria-label="GPU 상태 시점 (KST)" type="datetime-local" step={1} value={at} onChange={e=>setAt(e.target.value)} required/></label><button type="submit" disabled={busy}>{busy?'시점 조회 중':'해당 시점 조회'}</button><small>지정 시각 이전의 가장 가까운 저장 기록 · 최대 7일 보관</small></form>}
  <div className="time-playback"><button onClick={()=>choose(index-1)} disabled={index<=0} aria-label="이전 관측 시점">‹</button><input aria-label="관측 시점 위치" type="range" min={0} max={Math.max(0,points.length-1)} step={1} value={Math.max(0,index)} disabled={!points.length} onChange={e=>choose(Number(e.target.value))}/><button onClick={()=>choose(index+1)} disabled={index>=points.length-1} aria-label="다음 관측 시점">›</button></div>
  <div className="time-range"><span>{kst(points[0]?.snapshot.captured_at)}</span><strong>{kst(points[index]?.snapshot.captured_at)}</strong><span>{kst(points.at(-1)?.snapshot.captured_at)}</span></div>
  <p className="caption">저장된 관측 시점을 선택합니다. 기록 사이의 실행·대기 상태는 보간하지 않습니다.</p>
 </section>
}
