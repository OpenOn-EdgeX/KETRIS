"""Versioned wire validation. No Kubernetes or device access on import."""
from dataclasses import dataclass
from datetime import datetime
import hashlib, json, math, re
from google.protobuf.json_format import MessageToDict

class ContractError(ValueError):
 def __init__(self,code,reason): self.code,self.reason=code,reason;super().__init__(reason)
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def sha(value): return hashlib.sha256(value.encode()).hexdigest()
def wire_dict(pb): return MessageToDict(pb,preserving_proto_field_name=True)
def payload_digest(pb): return sha(canonical(wire_dict(pb)))
def mig_resource_name(mig_uuid):
 if not re.fullmatch(r'MIG-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',mig_uuid): raise ContractError('INVALID_ARGUMENT','invalid canonical MIG UUID')
 return 'keti.re.kr/mig-'+sha(mig_uuid)[:16]+'-share'
def require(ok,reason):
 if not ok: raise ContractError('INVALID_ARGUMENT',reason)
@dataclass(frozen=True)
class LiveMigTarget:
 node:str;gpu_uuid:str;mig_uuid:str;resource_name:str;inventory_generation:str
@dataclass(frozen=True)
class ProfileSpec:
 payload_json:str;gpu_memory_bytes:int;executable:bool
 @property
 def data(self): return json.loads(self.payload_json)
 def __getattr__(self,key):
  d=self.data
  if key in d:return d[key]
  if key in d['meta']:return d['meta'][key]
  raise AttributeError(key)
@dataclass(frozen=True)
class PlanSpec:
 payload_json:str;executable:bool
 @property
 def data(self): return json.loads(self.payload_json)
 @property
 def target(self): return LiveMigTarget(**self.data['target'])
 def __getattr__(self,key):
  d=self.data
  if key in d:return d[key]
  if key in d['meta']:return d['meta'][key]
  raise AttributeError(key)
@dataclass(frozen=True)
class Receipt:
 request_id:str;receipt_id:str;workload_id:str;state:str;reason:str;object_id:str;version:int;payload_sha256:str
@dataclass(frozen=True)
class ExecutionRecord:
 workload_id:str;profile:ProfileSpec|None=None;plan:PlanSpec|None=None;state:str='PROFILE_WAIT';reason:str='';digest:str='';job_name:str='';job_uid:str='';epoch:str='';keti_decision_id:str='';guard_decision_id:str='';evidence_json:str='{}';sample_json:str='{}'
@dataclass(frozen=True)
class TargetObservation:
 available:bool;generation:str='';reason:str='';inventory_json:str='{}';manager_state:str='';observed_at:str=''
@dataclass(frozen=True)
class DomainSnapshot:
 partition_json:str;bindings_json:str;observed_at:str
 @property
 def partition(self):return json.loads(self.partition_json)
 @property
 def bindings(self):return json.loads(self.bindings_json)
@dataclass(frozen=True)
class RuntimeSample:
 sample_json:str
@dataclass(frozen=True)
class ApplyAssessment:
 state:str;reason:str='';evidence_json:str='{}';sample_json:str='{}'
@dataclass(frozen=True)
class AdmissionVerdict:
 state:str;reason:str;epoch:str='';keti_decision_id:str='';guard_decision_id:str=''

def check_meta(meta,now):
 require(meta.schema_version=='v1','unsupported schema_version')
 for name in ('request_id','trace_id','workload_id','provider'):
  require(bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}',getattr(meta,name))),f'invalid {name}')
 require(meta.origin in ('SYNTHETIC','MEASURED'),'explicit origin required')
 require(meta.execution_mode in ('LIVE_EXECUTION','CONTRACT_ONLY'),'explicit execution_mode required')
 require(0<meta.created_at_unix_s<=now.timestamp()+60,'created_at invalid')
 require(meta.expires_at_unix_s>now.timestamp() and meta.expires_at_unix_s>meta.created_at_unix_s,'input expired')
def parse_profile(pb,now:datetime)->ProfileSpec:
 check_meta(pb.meta,now)
 require(bool(pb.profile_id) and pb.version>0,'profile id/version required')
 require(pb.model=='cuda-gemm-2048-fp32-v1' and pb.workload_type=='benchmark','unsupported execution workload')
 require(list(pb.input_shape)==[2048,2048] and pb.compute_dtype=='fp32' and pb.input_dtype=='fp32' and pb.batch==1,'unsupported GEMM shape/dtype/batch')
 require(pb.gpu_memory_source in ('SYNTHETIC','MEASURED'),'memory provenance required')
 value=pb.gpu_memory_value
 require(math.isfinite(value) and value>0,'memory demand must be finite and positive')
 units={'B':1,'MiB':1048576,'GiB':1073741824}
 require(pb.gpu_memory_unit in units,'unknown GPU memory unit')
 b=value*units[pb.gpu_memory_unit]
 require(b.is_integer() and b<=2**53,'memory demand must be integral bytes')
 if pb.HasField('predicted_latency_ms'): require(math.isfinite(pb.predicted_latency_ms) and pb.predicted_latency_ms>0,'invalid prediction')
 return ProfileSpec(canonical(wire_dict(pb)),int(b),pb.meta.execution_mode=='LIVE_EXECUTION')
def parse_plan(pb,now:datetime)->PlanSpec:
 check_meta(pb.meta,now)
 require(bool(pb.plan_id) and pb.revision>0 and bool(pb.profile_id) and pb.profile_version>0 and bool(pb.assignment_id),'plan refs required')
 require(1<=pb.space_percent<=100 and 1<=pb.time_weight<=1000,'invalid cap/weight')
 require(pb.execution_template=='cuda-gemm-v1','unsupported execution template')
 require(bool(pb.policy_source) and bool(pb.snapshot_ref),'policy source and snapshot required')
 t=pb.target
 require(bool(t.node) and bool(t.inventory_generation),'live inventory reference required')
 require(bool(re.fullmatch(r'GPU-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',t.gpu_uuid)),'invalid GPU UUID')
 require(t.resource_name==mig_resource_name(t.mig_uuid),'MIG resource hash mismatch')
 return PlanSpec(canonical(wire_dict(pb)),pb.meta.execution_mode=='LIVE_EXECUTION')
def config_digest(profile:ProfileSpec,plan:PlanSpec)->str:
 return sha(canonical(dict(workload_id=plan.workload_id,profile_id=profile.profile_id,profile_version=profile.version,plan_id=plan.plan_id,revision=plan.revision,assignment_id=plan.assignment_id,target=plan.data['target'],space_percent=plan.space_percent,time_weight=plan.time_weight,policy_source=plan.policy_source,execution_template=plan.execution_template,profile_payload=sha(profile.payload_json))))
