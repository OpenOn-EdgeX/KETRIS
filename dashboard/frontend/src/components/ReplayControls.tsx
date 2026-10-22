import {windowLabel} from '../presentation';
import type {Frame} from '../types';
export function ReplayControls({frames,index,setIndex,playing,setPlaying,speed,setSpeed}:{frames:Frame[];index:number;setIndex:(i:number)=>void;playing:boolean;setPlaying:(v:boolean)=>void;speed:number;setSpeed:(v:number)=>void}){
 return <section className="replay-controls time-selection" aria-label="실험 측정 창 선택">
  <div className="time-heading"><h2>시간 선택</h2><span className="window-badge">실제 측정 창 기반</span><strong>{frames[index]?windowLabel(frames[index].interval):'기록 없음'}</strong></div>
  <div className="playback"><button aria-label="이전 측정 창" disabled={index===0} onClick={()=>{setPlaying(false);setIndex(Math.max(0,index-1))}}>‹</button><input type="range" aria-label="측정 창 위치" min={0} max={Math.max(0,frames.length-1)} step={1} value={index} disabled={!frames.length} onChange={e=>{setPlaying(false);setIndex(Number(e.target.value))}}/><button aria-label="다음 측정 창" disabled={index>=frames.length-1} onClick={()=>{setPlaying(false);setIndex(index+1)}}>›</button><button className="play-button" aria-label={playing?'일시정지':'재생 시작'} disabled={!frames.length} onClick={()=>setPlaying(!playing)}>{playing?'Ⅱ 일시정지':'▶ 재생'}</button><span>{frames.length?index+1:0} / {frames.length}</span><select aria-label="재생 속도" value={speed} onChange={e=>setSpeed(Number(e.target.value))}>{[1,2,4].map(v=><option key={v} value={v}>{v}×</option>)}</select></div>
  <p className="caption">시점을 옮기면 위 자원판이 갱신됩니다. 다음 기록까지 측정값을 유지하며 창 사이의 가상 실행 상태는 생성하지 않습니다.</p>
 </section>
}
