"""Only KETRIS-owned installation objects. No shared configuration patches."""
from dataclasses import asdict
from ketris_integration.contracts import LiveMigTarget,canonical,ContractError
OWNER='ketris-institution-bridge';NS='ketris-integration';WNS='ketris-integration-workloads';QUEUE='ketris-integration-mig';FLAVOR='ketris-integration-devmaster';CHECK='ketris-plan-validation'
def check_owned(existing):
 if existing and existing.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/managed-by')!=OWNER:raise ContractError('FAILED_PRECONDITION','refusing unowned resource')
def obj(kind,name,spec=None,namespace=None,api='v1',**extra):
 d=dict(apiVersion=api,kind=kind,metadata=dict(name=name,labels={'app.kubernetes.io/managed-by':OWNER}))
 if namespace:d['metadata']['namespace']=namespace
 if spec is not None:d['spec']=spec
 return d|extra
def render_installation(target:LiveMigTarget,images:dict)->list[dict]:
 ns=obj('Namespace',NS);wns=obj('Namespace',WNS);wns['metadata']['labels'].update({'keti.re.kr/exec-router':'enabled','ketris.integration/queue-scope':QUEUE})
 docs=[ns,wns,obj('ServiceAccount',OWNER,namespace=NS),obj('PersistentVolumeClaim','bridge-data',dict(accessModes=['ReadWriteOnce'],storageClassName='local-path',resources={'requests':{'storage':'4Gi'}}),NS),obj('ConfigMap','live-mig-target',namespace=NS,data={'target.json':canonical(asdict(target))})]
 rules=[dict(apiGroups=['batch'],resources=['jobs'],verbs=['get','list','create']),dict(apiGroups=[''],resources=['pods'],verbs=['get','list']),dict(apiGroups=['kueue.x-k8s.io'],resources=['workloads'],verbs=['get','list']),dict(apiGroups=['kueue.x-k8s.io'],resources=['workloads/status'],verbs=['get','patch'])]
 docs += [obj('Role',OWNER,namespace=WNS,api='rbac.authorization.k8s.io/v1',rules=rules),obj('RoleBinding',OWNER,namespace=WNS,api='rbac.authorization.k8s.io/v1',subjects=[dict(kind='ServiceAccount',name=OWNER,namespace=NS)],roleRef=dict(apiGroup='rbac.authorization.k8s.io',kind='Role',name=OWNER))]
 crules=[dict(apiGroups=[''],resources=['nodes'],verbs=['get','list']),dict(apiGroups=['kueue.x-k8s.io'],resources=['resourceflavors'],resourceNames=[FLAVOR],verbs=['get']),dict(apiGroups=['kueue.x-k8s.io'],resources=['admissionchecks'],resourceNames=[CHECK],verbs=['get']),dict(apiGroups=['kueue.x-k8s.io'],resources=['admissionchecks/status'],resourceNames=[CHECK],verbs=['get','patch'])]
 docs += [obj('ClusterRole',OWNER,api='rbac.authorization.k8s.io/v1',rules=crules),obj('ClusterRoleBinding',OWNER,api='rbac.authorization.k8s.io/v1',subjects=[dict(kind='ServiceAccount',name=OWNER,namespace=NS)],roleRef=dict(apiGroup='rbac.authorization.k8s.io',kind='ClusterRole',name=OWNER))]
 kapi='kueue.x-k8s.io/v1beta2'
 docs += [obj('AdmissionCheck',CHECK,dict(controllerName='ketris.re.kr/plan-validation'),api=kapi),obj('ResourceFlavor',FLAVOR,dict(nodeLabels={'kubernetes.io/hostname':target.node}),api=kapi)]
 resources=['cpu','memory',target.resource_name]
 cq=dict(namespaceSelector={'matchLabels':{'ketris.integration/queue-scope':QUEUE}},queueingStrategy='BestEffortFIFO',admissionChecksStrategy={'admissionChecks':[{'name':'keti-scheduler'},{'name':CHECK}]},resourceGroups=[dict(coveredResources=resources,flavors=[dict(name=FLAVOR,resources=[dict(name=k,nominalQuota=v) for k,v in zip(resources,['1','1Gi','0'])])])])
 docs += [obj('ClusterQueue',QUEUE,cq,api=kapi),obj('LocalQueue',QUEUE,dict(clusterQueue=QUEUE),WNS,api=kapi)]
 mounts=[dict(name='data',mountPath='/data'),dict(name='tmp',mountPath='/tmp'),dict(name='auth',mountPath='/auth',readOnly=True)]+[dict(name=n,mountPath='/exports/'+n,readOnly=True) for n in ('bindings','partitions')]
 volumes=[dict(name='data',persistentVolumeClaim={'claimName':'bridge-data'}),dict(name='tmp',emptyDir={}),dict(name='auth',secret={'secretName':'bridge-auth','defaultMode':256})]+[dict(name=n,hostPath={'path':'/var/lib/ketris-mig/'+n,'type':'Directory'}) for n in ('bindings','partitions')]
 container=dict(name='bridge',image=images['bridge'],imagePullPolicy='Never',command=['python3','-m','ketris_integration.server'],args=['--image',images['worker']],ports=[dict(containerPort=50051)],resources=dict(requests={'cpu':'100m','memory':'128Mi'},limits={'cpu':'1','memory':'512Mi'}),securityContext=dict(runAsUser=0,privileged=False,allowPrivilegeEscalation=False,readOnlyRootFilesystem=True,capabilities={'drop':['ALL']}),volumeMounts=mounts,readinessProbe=dict(tcpSocket={'port':50051},initialDelaySeconds=2,periodSeconds=2))
 dep=dict(replicas=1,strategy={'type':'Recreate'},selector={'matchLabels':{'app':OWNER}},template=dict(metadata=dict(labels={'app':OWNER}),spec=dict(serviceAccountName=OWNER,nodeSelector={'kubernetes.io/hostname':target.node},containers=[container],volumes=volumes)))
 docs += [obj('Deployment',OWNER,dep,NS,api='apps/v1'),obj('Service',OWNER,dict(type='ClusterIP',selector={'app':OWNER},ports=[dict(name='grpc',port=50051,targetPort=50051)]),NS)]
 return docs
