import {afterEach,it,expect,vi} from 'vitest';
import {render,screen,fireEvent,waitFor,cleanup,act,within} from '@testing-library/react';
import {App} from '../App';
import {DeviceMap} from '../components/DeviceMap';
import {WorkloadDetail} from '../components/WorkloadDetail';
import {WorkloadTable} from '../components/WorkloadTable';
import {get} from '../api';
import type {Snapshot,Frame} from '../types';
vi.mock('../api',()=>({get:vi.fn()}));

function snapshot(at:string,uid:string,name:string):Snapshot {
 const metric=(value:number|string|null,unit?:string)=>({value,unit,source:'runtime',quality:'observed' as const});
 return {schema_version:1,node:'n',mode:'live',captured_at:at,
  devices:[{id:'gpu',kind:'gpu',name:'GPU',node:'n',metrics:{}}],
  partitions:[{id:'mig',device_id:'gpu',name:'MIG 1g.24gb',kind:'mig',generation:'g',historical:false,pe_indices:[],metrics:{profile_sm:metric(46,'SM'),mig_uuid:metric('MIG-test')}}],
  workloads:[{id:'n:'+uid+':work',name,partition_id:'mig',pod_uid:uid,container:'work',origin:'kubernetes',state:'armed',requested:{space_percent:metric(50,'%'),time_weight:metric(3)},applied:{mps_percent:metric(50,'%'),reported_sm_count:metric(22,'SM'),time_share:metric(.75,'fraction')},observed:{kernels:metric(0,'kernels'),credit_us:metric(-5,'us')}}],
  source_status:{runtime:{status:'ok'},gpu:{status:'ok'},kubernetes:{status:'ok'},checkpoint:{status:'ok'}}};
}
afterEach(()=>{cleanup();vi.useRealTimers();vi.resetAllMocks()});
it('keeps one selected historical resource state while fresh live observations arrive',async()=>{
 const old=snapshot('2026-10-13T00:00:00Z','old','past-work');
 let head=snapshot('2026-10-13T00:00:10Z','now','current-work');
 let history=[{id:1,snapshot:old}];
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments')return [];
  if(path==='nodes')return [{name:'n',connected:true,received_at:head.captured_at}];
  if(path.endsWith('/snapshot'))return head;
  if(path.includes('/history?'))return history;
  throw new Error('unexpected request '+path);
 });
 render(<App/>);
 const board=await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 await waitFor(()=>expect(within(board).getByText('current-work')).toBeTruthy());
 await waitFor(()=>expect(screen.getByLabelText('관측 시점 위치').getAttribute('max')).toBe('1'));
 fireEvent.change(screen.getByLabelText('관측 시점 위치'),{target:{value:'0'}});
 expect(within(board).getByText('past-work')).toBeTruthy();
 expect(within(board).queryByText('current-work')).toBeNull();
 fireEvent.click(within(board).getByRole('button',{name:'past-work 상세 보기'}));
 const detail=screen.getByRole('complementary',{name:'워크로드 상세'});
 expect(within(detail).getByText('old')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'실시간 조회'}));
 expect(within(board).getByText('past-work')).toBeTruthy();
 vi.useFakeTimers();
 head=snapshot('2026-10-13T00:00:20Z','replacement','current-work');
 history=[{id:2,snapshot:snapshot('2026-10-13T00:00:15Z','replacement','current-work')}];
 await act(async()=>{await vi.advanceTimersByTimeAsync(10000)});
 expect(screen.getAllByRole('region',{name:'선택한 시점의 자원 상태'})).toHaveLength(1);
 expect(within(board).getByText('past-work')).toBeTruthy();
 expect(within(detail).getByText('old')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'최신 관측 따라가기'}));
 expect(within(board).getByText('current-work')).toBeTruthy();
 expect(screen.queryByText('old')).toBeNull();
});

it('shows both MIGs and selects partition detail without hiding either workload',async()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');
 s.partitions.push({...structuredClone(s.partitions[0]),id:'mig-two',name:'MIG two'});
 s.workloads.push({...structuredClone(s.workloads[0]),id:'n:b:work',name:'B',partition_id:'mig-two'});
 vi.mocked(get).mockImplementation(async path=>path==='experiments'?[]:path==='nodes'?[{name:'n'}]:path.endsWith('/snapshot')?s:[]);
 render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'A 상세 보기'}));
 expect(within(screen.getByRole('complementary',{name:'워크로드 상세'})).getByText('A')).toBeTruthy();
 expect(screen.getByRole('button',{name:'B 상세 보기'})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:/^MIG two 자원 상세/}));
 expect(screen.getByRole('button',{name:'B 상세 보기'})).toBeTruthy();
 expect(within(screen.getByRole('complementary',{name:'워크로드 상세'})).queryByText('A')).toBeNull();
 expect(screen.getByRole('button',{name:'A 상세 보기'})).toBeTruthy();
 expect(within(screen.getByRole('complementary',{name:'워크로드 상세'})).getByText('MIG two')).toBeTruthy();
});

it('renders an empty MIG alongside allocated MIGs and can inspect its capacity',()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');
 const empty={...structuredClone(s.partitions[0]),id:'empty',name:'MIG empty'};
 empty.metrics.mig_uuid.value='MIG-empty';s.partitions.push(empty);
 render(<DeviceMap snapshot={s}/>);
 const map=screen.getByRole('group',{name:'GPU 전체 파티션 지도'});
 expect(within(map).getByRole('button',{name:'A 상세 보기'})).toBeTruthy();
 expect(within(map).getByRole('button',{name:/^MIG empty 자원 상세/})).toBeTruthy();
 expect(within(map).getByText('연결된 워크로드 없음')).toBeTruthy();
});

it('does not mark a MIG empty when connection membership sources are unavailable',()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');s.workloads=[];
 s.source_status.kubernetes.status='unavailable';s.source_status.checkpoint.status='unavailable';s.source_status.runtime.status='unavailable';
 render(<DeviceMap snapshot={s}/>);
 expect(screen.queryByText('연결된 워크로드 없음')).toBeNull();
 expect(screen.getByText('워크로드 연결 미관측')).toBeTruthy();
 expect(screen.queryByText('연결 0개')).toBeNull();
});

it('does not invent empty MIGs outside a partial archive inventory',()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');s.mode='replay';s.partitions[0].historical=true;
 render(<DeviceMap snapshot={s}/>);
 expect(screen.getByText('부분 기록 · 다른 MIG 상태 미기록')).toBeTruthy();
 expect(within(screen.getByRole('group',{name:'GPU 전체 파티션 지도'})).getAllByRole('region')).toHaveLength(1);
});

it('rewinds the whole GPU inventory to a chosen KST time beyond the recent slider',async()=>{
 const old=snapshot('2026-10-13T00:00:10Z','a','A'),head=snapshot('2026-10-14T00:00:10Z','b','B');
 old.partitions.push({...structuredClone(old.partitions[0]),id:'empty-old',name:'Old empty MIG'});
 head.partitions[0].id='new-mig';head.partitions[0].name='New MIG';head.workloads[0].partition_id='new-mig';
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments')return [];
  if(path==='nodes')return [{name:'n'}];
  if(path.endsWith('/snapshot'))return head;
  if(path.includes('at='))return [{id:1,snapshot:old}];
  if(path.includes('/history?'))return [];
  throw new Error('unexpected '+path);
 });
 render(<App/>);const board=await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 fireEvent.change(screen.getByLabelText('GPU 상태 시점 (KST)'),{target:{value:'2026-10-13T09:00:15'}});
 fireEvent.click(screen.getByRole('button',{name:'해당 시점 조회'}));
 await waitFor(()=>expect(within(board).getByText('Old empty MIG')).toBeTruthy());
 expect(within(board).getByRole('button',{name:'A 상세 보기'})).toBeTruthy();
 expect(within(board).queryByText('New MIG')).toBeNull();
 expect(within(board).queryByRole('button',{name:'B 상세 보기'})).toBeNull();
 fireEvent.click(screen.getByRole('button',{name:'최신 관측 따라가기'}));
 expect(within(board).getByText('New MIG')).toBeTruthy();
 expect(within(board).queryByText('Old empty MIG')).toBeNull();
});

it('shows known archived PE assignment consistently without treating live capacity as assignment',()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');s.mode='replay';
 s.devices[0].kind='npu';s.devices[0].metrics.pe_count={value:8,source:'archive',quality:'configured'};
 const p=s.partitions[0];p.kind='pe';p.historical=true;p.pe_indices=[0,1,2,3];p.metrics={};
 const w=s.workloads[0];w.applied={};
 render(<><DeviceMap snapshot={s}/><WorkloadDetail workload={w} partition={p} onClose={()=>{}}/></>);
 expect(within(screen.getByRole('button',{name:'A 상세 보기'})).getByText('0,1,2,3')).toBeTruthy();
 expect(within(screen.getByRole('complementary',{name:'워크로드 상세'})).getByText('0,1,2,3')).toBeTruthy();
 expect(screen.queryByText('점유: A')).toBeNull();
 cleanup();s.mode='live';p.historical=false;
 render(<DeviceMap snapshot={s}/>);
 expect(within(screen.getByRole('button',{name:'A 상세 보기'})).queryByText('0,1,2,3')).toBeNull();
});

it.each(['ambiguous','configured','stale'] as const)('does not verify %s actual CUDA UUID in board or table',quality=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');
 s.workloads[0].applied.actual_cuda_uuid={value:'MIG-test',source:'runtime',quality};
 render(<><DeviceMap snapshot={s}/><WorkloadTable snapshot={s}/></>);
 expect(screen.queryByText('일치 확인')).toBeNull();
});

it('recovers a failed experiment list without live polling hiding the failure',async()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');let available=false;
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments'){if(!available)throw new Error('offline');return [{id:'e',name:'ketris_mig_20261013',kind:'mig',origin:'MOCK'}]}
  if(path==='nodes')return [{name:'n'}];
  if(path.endsWith('/snapshot'))return s;
  if(path.includes('/history?'))return [];
  if(path==='experiments/e')return {id:'e',metadata:{},summary:{},source_manifest:[],frame_count:1};
  if(path.startsWith('experiments/e/frames'))return [{index:0,axis_kind:'measurement_window',interval:{condition:'space50_50',repeat:1,start:0,end:20},snapshot:{...s,mode:'replay'},evidence_ids:[]}];
  throw new Error('unexpected '+path);
 });
 render(<App/>);await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 fireEvent.click(screen.getByRole('button',{name:'실험 재생'}));
 expect(await screen.findByText('실험 목록을 가져오지 못했습니다.')).toBeTruthy();
 available=true;fireEvent.click(screen.getByRole('button',{name:'실험 목록 다시 시도'}));
 expect(await screen.findByRole('region',{name:'선택한 시점의 자원 상태'})).toBeTruthy();
 expect(screen.queryByText('실험 목록을 가져오지 못했습니다.')).toBeNull();
});

it('selects one recorded window without inventing instant execution or gate waiting',async()=>{
 const one=snapshot('2026-10-13T00:00:00Z','a','A');
 one.mode='replay';one.captured_at=null;one.workloads[0].state='measured';
 one.workloads[0].observed.kernels.value=933;
 const two=structuredClone(one);two.workloads[0].applied.reported_sm_count.value=46;
 const frames:Frame[]=[one,two].map((s,index)=>({index,axis_kind:'measurement_window',interval:{condition:'combined50_50_time3_1',repeat:index+1,start:index*20,end:(index+1)*20},snapshot:s,evidence_ids:[]}));
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments')return [{id:'e',name:'ketris_mig_20261013',kind:'mig',origin:'MOCK'}];
  if(path==='nodes')return [];
  if(path==='experiments/e')return {id:'e',metadata:{origin:'MOCK'},summary:{},frame_count:2,source_manifest:[]};
  if(path.startsWith('experiments/e/frames'))return frames;
  throw new Error('unexpected request '+path);
 });
 render(<App/>);fireEvent.click(screen.getByRole('button',{name:'실험 재생'}));
 const board=await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 await waitFor(()=>expect(within(board).getByText('22 SM')).toBeTruthy());
 expect(within(board).queryByText('실행 중')).toBeNull();
 expect(within(board).queryByText('크레딧 대기')).toBeNull();
 fireEvent.change(screen.getByLabelText('측정 창 위치'),{target:{value:'1'}});
 expect(within(within(board).getByRole('button',{name:'A 상세 보기'})).getByText('46 SM')).toBeTruthy();
 expect(within(board).queryByText('22 SM')).toBeNull();
 expect(screen.getAllByRole('region',{name:'선택한 시점의 자원 상태'})).toHaveLength(1);
});

it('reports archive loading failure after switching from live to replay',async()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments')return [{id:'e',name:'ketris_mig_20261013',kind:'mig',origin:'MOCK'}];
  if(path==='nodes')return [{name:'n',connected:true,received_at:s.captured_at}];
  if(path.endsWith('/snapshot'))return s;
  if(path.includes('/history?'))return [];
  throw new Error('archive offline');
 });
 render(<App/>);
 await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 fireEvent.click(screen.getByRole('button',{name:'실험 재생'}));
 expect(await screen.findByText('실험 증빙을 가져오지 못했습니다.')).toBeTruthy();
});

it('discards delayed evidence after the selected measurement window changes',async()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');s.mode='replay';s.captured_at=null;
 let resolveEvidence!:(value:unknown)=>void;
 const delayed=new Promise(resolve=>{resolveEvidence=resolve});
 vi.mocked(get).mockImplementation(async path=>{
  if(path==='experiments')return [{id:'e',name:'ketris_mig_20261013',kind:'mig',origin:'MOCK'}];
  if(path==='nodes')return [];
  if(path==='experiments/e')return {id:'e',metadata:{},summary:{},frame_count:2,source_manifest:[]};
  if(path.startsWith('experiments/e/frames'))return [0,1].map(index=>({index,axis_kind:'measurement_window',interval:{condition:'space50_50',repeat:index+1,start:20*index,end:20*(index+1)},snapshot:s,evidence_ids:['old']}));
  if(path==='evidence/old')return delayed;
  throw new Error('unexpected '+path);
 });
 render(<App/>);fireEvent.click(screen.getByRole('button',{name:'실험 재생'}));
 await screen.findByRole('region',{name:'선택한 시점의 자원 상태'});
 fireEvent.click(screen.getByRole('button',{name:'원본 증빙'}));
 fireEvent.click(screen.getByRole('button',{name:'다음 측정 창'}));
 await act(async()=>{resolveEvidence({path:'old-window.json',sha256:'old'});await delayed});
 expect(screen.queryByRole('dialog')).toBeNull();
});

it('marks CUDA identity verified only when a fresh actual UUID matches selected MIG',()=>{
 const s=snapshot('2026-10-13T00:00:10Z','a','A');
 s.workloads[0].applied.actual_cuda_uuid={value:'MIG-test',source:'runtime',quality:'observed'};
 render(<DeviceMap snapshot={s}/>);
 expect(within(screen.getByRole('button',{name:'A 상세 보기'})).getByText('일치 확인')).toBeTruthy();
 cleanup();s.workloads[0].applied.actual_cuda_uuid.quality='stale';
 render(<DeviceMap snapshot={s}/>);
 expect(within(screen.getByRole('button',{name:'A 상세 보기'})).queryByText('일치 확인')).toBeNull();
 cleanup();s.workloads[0].applied.actual_cuda_uuid={value:'GPU-other',source:'runtime',quality:'observed'};
 render(<DeviceMap snapshot={s}/>);
 expect(within(screen.getByRole('button',{name:'A 상세 보기'})).getByText('UUID 불일치')).toBeTruthy();
});
