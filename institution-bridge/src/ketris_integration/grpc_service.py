"""Authenticated real protobuf RPCs; receive receipts never imply approval."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime,timezone
import grpc,hmac,json,logging
from google.protobuf.json_format import ParseDict
from .contracts import *
from .rpc import institution_bridge_pb2 as pb,institution_bridge_pb2_grpc as wire
log=logging.getLogger('ketris.integration')
class Service(wire.ProfileIngestServiceServicer,wire.PlanAdmissionServiceServicer,wire.IntegrationStatusServiceServicer):
 def __init__(self,store,token):self.store,self.token=store,token
 def call(self,context,fn):
  token=dict(context.invocation_metadata()).get('authorization','')
  if not hmac.compare_digest(token,'Bearer '+self.token):context.abort(grpc.StatusCode.UNAUTHENTICATED,'authentication required')
  try:return fn()
  except ContractError as e:context.abort(getattr(grpc.StatusCode,e.code,grpc.StatusCode.INVALID_ARGUMENT),e.reason)
 def receive(self,request,kind,context):
  digest=payload_digest(request);receipt=self.store.find_receipt(request.meta.request_id,digest)
  if receipt is None:
   spec=(parse_profile if kind=='profile' else parse_plan)(request,datetime.now(timezone.utc))
   receipt=(self.store.receive_profile if kind=='profile' else self.store.receive_plan)(spec,digest)
  spec=request.meta
  log.info(canonical(dict(event='rpc_receipt',rpc=kind,peer=context.peer(),trace_id=spec.trace_id,request_id=spec.request_id,workload_id=spec.workload_id,payload_sha256=digest,receipt=asdict(receipt))))
  cls=pb.ProfileReceipt if kind=='profile' else pb.PlanReceipt;d={k:getattr(receipt,k) for k in ('request_id','receipt_id','workload_id','reason','payload_sha256')};d['state']=getattr(pb,receipt.state)
  d.update({'profile_id':receipt.object_id,'version':receipt.version} if kind=='profile' else {'plan_id':receipt.object_id,'revision':receipt.version})
  return cls(**d)
 def PushProfile(self,r,c):return self.call(c,lambda:self.receive(r,'profile',c))
 def SubmitPartitionPlan(self,r,c):return self.call(c,lambda:self.receive(r,'plan',c))
 def GetProfile(self,r,c):
  def run():
   p=self.store.get_profile(r.profile_id,r.version)
   if p is None:raise ContractError('NOT_FOUND','profile id/version not found')
   message=ParseDict(p.data,pb.PushProfileRequest());return pb.ProfileResult(profile=message,payload_sha256=payload_digest(message))
  return self.call(c,run)
 def GetWorkloadState(self,r,c):
  def run():
   record=self.store.get_execution(r.workload_id)
   if not record:raise ContractError('NOT_FOUND','workload not found')
   p,q=record.profile,record.plan;e=json.loads(record.evidence_json)
   out=pb.WorkloadState(workload_id=record.workload_id,state=record.state,reason=record.reason,job_name=record.job_name,job_uid=record.job_uid,profile_id=p.profile_id if p else '',profile_version=p.version if p else 0,plan_id=q.plan_id if q else '',plan_revision=q.revision if q else 0,config_digest=record.digest,keti_decision_id=record.keti_decision_id,guard_decision_id=record.guard_decision_id,admission_epoch=record.epoch,detail_json=canonical(asdict(record)))
   if e:
    for key in ('approved_config_digest','config_version','job_uid','workload_uid','pod_uid','container_id','allocation_id','runtime_generation','actual_cuda_uuid'):setattr(out.applied,key,e.get(key,''))
    out.applied.evidence_json=record.evidence_json
   return out
  return self.call(c,run)
 def ListEvents(self,r,c):
  def run():
   rows=self.store.events(r.trace_id,r.after_seq,r.limit,r.workload_id)
   return pb.EventPage(events=[pb.EventRecord(**x) for x in rows],next_seq=rows[-1]['seq'] if rows else r.after_seq)
  return self.call(c,run)
def make_grpc_server(store,token)->grpc.Server:
 require(bool(token),'empty auth token forbidden')
 server=grpc.server(ThreadPoolExecutor(max_workers=8),options=[('grpc.max_receive_message_length',1048576),('grpc.max_send_message_length',4194304)])
 service=Service(store,token)
 wire.add_ProfileIngestServiceServicer_to_server(service,server);wire.add_PlanAdmissionServiceServicer_to_server(service,server);wire.add_IntegrationStatusServiceServicer_to_server(service,server)
 return server
