import type {Workload,Partition} from '../types';
import {Value} from './Value';
import {workloadColor,stateLabel,assignedPe} from '../presentation';

export function WorkloadDetail({workload,partition,workloadCount,onClose}:{workload?:Workload;partition?:Partition;workloadCount?:number;onClose:()=>void}){
 return <aside className="selection-detail" aria-label="워크로드 상세">
  <div className="detail-heading"><h2>{!workload&&partition?'MIG 자원 상세':'워크로드 상세'}</h2>{(workload||partition)&&<button onClick={onClose} aria-label="워크로드 상세 닫기">×</button>}</div>
  {!workload?partition?<><div className="detail-identity"><h3>{partition.name}</h3></div><dl className="detail-metrics"><dt>연결된 워크로드</dt><dd>{workloadCount===undefined?'— 미관측':workloadCount+'개'}</dd><dt>SM 용량</dt><dd><Value value={partition.metrics.profile_sm} quality/></dd><dt>메모리 용량</dt><dd><Value value={partition.metrics.memory_mib} quality/></dd></dl><div className="partition-identity"><span>MIG UUID</span><Value value={partition.metrics.mig_uuid} quality/></div><p className="caption">선택한 시점의 파티션 기록입니다. 워크로드 연결이 없다는 사실만으로 장치가 유휴 상태라고 판단하지 않습니다.</p></>:<div className="detail-empty"><span className="detail-symbol">⌖</span><strong>자원판에서 워크로드를 선택하세요</strong><p>MIG 제목을 누르면 비어 있는 파티션의 용량도 확인할 수 있습니다.</p></div>:<>
   <div className="detail-identity"><i className="dot" style={{background:workloadColor(workload)}}/><h3>{workload.name}</h3><span className="state-badge">{stateLabel(workload.state)}</span></div>
   <dl className="detail-metrics">
    <dt>공간 요청</dt><dd><Value value={workload.requested.space_percent||workload.requested.pe_count||workload.requested.lsu} quality/></dd>
    <dt>적용된 상한 / PE 배정</dt><dd><Value value={workload.applied.mps_percent||assignedPe(workload,partition)} quality/></dd>
    <dt>CUDA 보고 SM</dt><dd><Value value={workload.applied.reported_sm_count} quality/></dd>
    <dt>시간 weight</dt><dd><Value value={workload.requested.time_weight} quality/></dd>
    <dt>시간 설정 비중</dt><dd><Value value={workload.applied.time_share||workload.requested.time_ratio} quality/></dd>
    <dt>10ms당 지급 예산</dt><dd><Value value={workload.applied.budget_us} quality/></dd>
    <dt>크레딧 잔액</dt><dd><Value value={workload.observed.credit_us} quality/></dd>
    <dt>실측 계상시간 비중</dt><dd><Value value={workload.observed.charged_share} quality/></dd>
    <dt>커널 / 완료 요청</dt><dd><Value value={workload.observed.kernels||workload.observed.requests_done} quality/></dd>
    <dt>계상시간</dt><dd><Value value={workload.observed.charged_us||workload.observed.charged_total_us} quality/></dd>
   </dl>
   {workload.observed.p95_completion_ms&&<div className="detail-extra">완료 지연 p95 <Value value={workload.observed.p95_completion_ms}/><small>완료 지연이며 TTFT 측정값이 아닙니다.</small></div>}
   <details className="identity-details"><summary>식별 정보 · 관측 출처</summary><dl><dt>Pod UID</dt><dd>{workload.pod_uid||'원본 기록 없음'}</dd><dt>컨테이너</dt><dd>{workload.container||'원본 기록 없음'}</dd><dt>선택 MIG</dt><dd>{String(partition?.metrics.mig_uuid?.value||'해당 없음')}</dd><dt>실제 CUDA UUID</dt><dd><Value value={workload.applied.actual_cuda_uuid} quality/></dd><dt>데이터 출처</dt><dd>{workload.origin}</dd></dl></details>
   <p className="caption">계상시간은 커널·요청 구간의 경과 시간입니다. 장치 busy time과 구분합니다.</p>
  </>}
 </aside>
}
