"""Build fixed executable requests and recover interrupted Job creation."""
import copy
from .contracts import *
NAMESPACE='ketris-integration-workloads';QUEUE='ketris-integration-mig';OWNER='ketris-institution-bridge'
PREFIX='ketris.integration/'
def build_job(record:ExecutionRecord,image:str)->dict:
 p,q=record.profile,record.plan
 require(bool(p and q and p.executable and q.executable),'not executable')
 digest=config_digest(p,q)
 refs={'workload-id':record.workload_id,'profile-id':p.profile_id,'profile-version':str(p.version),'plan-id':q.plan_id,'plan-revision':str(q.revision),'trace-id':q.trace_id,'config-digest':digest,'inventory-generation':q.target.inventory_generation}
 ann={PREFIX+k:v for k,v in refs.items()};ann.update({'ketris.re.kr/mig-uuid':q.target.mig_uuid,'ketris.re.kr/time-weight':str(q.time_weight)})
 labels={'app.kubernetes.io/managed-by':OWNER,'kueue.x-k8s.io/queue-name':QUEUE}
 env=[dict(name='EXPECTED_MIG',value=q.target.mig_uuid),dict(name='WORKLOAD_ID',value=record.workload_id),dict(name='POD_UID',valueFrom={'fieldRef':{'fieldPath':'metadata.uid'}})]
 pod=dict(metadata=dict(labels=dict(labels),annotations=dict(ann)),spec=dict(restartPolicy='Never',nodeSelector={'kubernetes.io/hostname':q.target.node},containers=[dict(name='workload',image=image,imagePullPolicy='Never',command=['python3','/app/worker.py'],env=env,resources=dict(requests={'cpu':'200m','memory':'128Mi',q.target.resource_name:q.space_percent},limits={'cpu':'1','memory':'512Mi',q.target.resource_name:q.space_percent}))]))
 return dict(apiVersion='batch/v1',kind='Job',metadata=dict(name='ki-'+sha(canonical([record.workload_id,q.plan_id,q.revision]))[:20],namespace=NAMESPACE,labels=labels,annotations=ann),spec=dict(suspend=True,backoffLimit=0,activeDeadlineSeconds=360,template=pod))
def project_execution_contract(obj:dict)->dict:
 pod=obj['spec']['template'] if obj.get('kind')=='Job' or 'template' in obj.get('spec',{}) else obj
 md,sp=pod['metadata'],pod['spec'];containers=[]
 for c in sp.get('containers',[]):
  env=copy.deepcopy([x for x in c.get('env',[]) if x['name'] in ('EXPECTED_MIG','WORKLOAD_ID','POD_UID')])
  for e in env:
   if 'valueFrom' in e:e['valueFrom'].get('fieldRef',{}).pop('apiVersion',None)
  resources={side:{k:str(v) for k,v in values.items()} for side,values in c.get('resources',{}).items()}
  containers.append({k:c.get(k) for k in ('name','image','command','args')}|{'resources':resources,'env':env})
 selector=dict(sp.get('nodeSelector',{}))
 if selector.get('keti.re.kr/node-role')=='edge':selector.pop('keti.re.kr/node-role')
 return dict(annotations={k:v for k,v in md.get('annotations',{}).items() if k.startswith(PREFIX) or k in ('ketris.re.kr/mig-uuid','ketris.re.kr/time-weight')},owner=md.get('labels',{}).get('app.kubernetes.io/managed-by'),queue=md.get('labels',{}).get('kueue.x-k8s.io/queue-name'),nodeSelector=selector,nodeName=sp.get('nodeName'),containers=containers,initContainers=sp.get('initContainers',[]),volumes=sp.get('volumes',[]))
def ensure_job(record,store,kube,image='ketris-institution-gemm:20261015-v1')->dict:
 desired=build_job(record,image);name=desired['metadata']['name'];existing=kube.get_job(name)
 if existing is None:
  try:existing=kube.create_job(desired)
  except Exception as exc:
   if getattr(exc,'status',None)!=409:raise
   existing=kube.get_job(name)
 if not existing or project_execution_contract(existing)!=project_execution_contract(desired):raise ContractError('FAILED_PRECONDITION','Job controlled contract mismatch')
 uid=existing['metadata']['uid']
 if record.job_uid and uid!=record.job_uid:raise ContractError('FAILED_PRECONDITION','Job UID changed')
 store.record_job(record.workload_id,uid,name)
 return existing
