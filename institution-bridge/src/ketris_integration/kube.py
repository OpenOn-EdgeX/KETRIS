"""Narrow Kubernetes transport; auth is configured by server composition."""
from .jobs import NAMESPACE
VERSION='v1beta2';CHECK='ketris-plan-validation'
class KubeAdapter:
 def __init__(self,api_client,namespace=NAMESPACE):self.api_client,self.namespace=api_client,namespace
 def api(self,method,path,body=None,ssa=False):
  return self.api_client.call_api(path,method,body=body,response_types_map={200:'object',201:'object',202:'object'},auth_settings=['BearerToken'],header_params={'Content-Type':'application/apply-patch+yaml' if ssa else 'application/json'},_return_http_data_only=True,_request_timeout=10)
 def get_job(self,name):
  try:return self.api('GET',f'/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}')
  except Exception as e:
   if getattr(e,'status',None)==404:return None
   raise
 def create_job(self,j):return self.api('POST',f'/apis/batch/v1/namespaces/{self.namespace}/jobs',j)
 def list_pods(self,job_uid):return [p for p in self.api('GET',f'/api/v1/namespaces/{self.namespace}/pods')['items'] if any(x.get('uid')==job_uid and x.get('kind')=='Job' for x in p['metadata'].get('ownerReferences',[]))]
 def list_workloads(self):return self.api('GET',f'/apis/kueue.x-k8s.io/{VERSION}/namespaces/{self.namespace}/workloads')['items']
 def list_nodes(self):return self.api('GET','/api/v1/nodes')['items']
 def get_flavor(self,name):return self.api('GET',f'/apis/kueue.x-k8s.io/{VERSION}/resourceflavors/{name}')
 def patch_check(self,workload,patch):
  # SSA force applies only fields inside our named map entry; never carries another entry.
  return self.api('PATCH',f'/apis/kueue.x-k8s.io/{VERSION}/namespaces/{self.namespace}/workloads/{workload["metadata"]["name"]}/status?fieldManager=ketris-plan-validation&force=true',patch,ssa=True)
 def activate_check(self):
  from datetime import datetime,timezone
  current=self.api('GET',f'/apis/kueue.x-k8s.io/{VERSION}/admissionchecks/{CHECK}')
  if any(c.get('type')=='Active' and c.get('status')=='True' for c in current.get('status',{}).get('conditions',[])):return current
  return self.api('PATCH',f'/apis/kueue.x-k8s.io/{VERSION}/admissionchecks/{CHECK}/status?fieldManager=ketris-plan-validation&force=true',dict(apiVersion='kueue.x-k8s.io/'+VERSION,kind='AdmissionCheck',metadata=dict(name=CHECK),status=dict(conditions=[dict(type='Active',status='True',reason='Active',message='KETRIS exact plan guard available',lastTransitionTime=datetime.now(timezone.utc).isoformat())])),ssa=True)
