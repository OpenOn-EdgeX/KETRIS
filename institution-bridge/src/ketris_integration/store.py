"""SQLite single-owner persistence; network handlers never share a connection."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,replace
from datetime import datetime,timezone
import json,sqlite3,uuid
from .contracts import *
TERMINAL={'COMPLETED','FAILED','REPLAN_REQUIRED'}
class BridgeStore:
 def __init__(self,path):
  self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='bridge-sqlite');self.closed=False
  def init():
   self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row
   self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,version INTEGER,body TEXT,PRIMARY KEY(kind,id,version));
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,hash TEXT,receipt TEXT);
CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,trace_id TEXT,workload_id TEXT,state TEXT,reason TEXT,at TEXT,detail_json TEXT);
''');self.db.commit()
  self.pool.submit(init).result()
 def _run(self,fn):return self.pool.submit(fn).result()
 def close(self):
  if not self.closed:self._run(self.db.close);self.pool.shutdown();self.closed=True
 def _get(self,wid):
  row=self.db.execute('SELECT body FROM executions WHERE id=?',(wid,)).fetchone()
  if not row:return None
  d=json.loads(row[0]);d['profile']=ProfileSpec(**d['profile']) if d['profile'] else None;d['plan']=PlanSpec(**d['plan']) if d['plan'] else None
  return ExecutionRecord(**d)
 def _save(self,r):self.db.execute('INSERT OR REPLACE INTO executions VALUES(?,?)',(r.workload_id,canonical(asdict(r))))
 def _event(self,r,detail=None):
  ref=r.plan or r.profile
  self.db.execute('INSERT INTO events(trace_id,workload_id,state,reason,at,detail_json) VALUES(?,?,?,?,?,?)',(ref.trace_id,r.workload_id,r.state,r.reason,datetime.now(timezone.utc).isoformat(),canonical(detail or {})))
 def _profile(self,oid,version):
  row=self.db.execute('SELECT body FROM objects WHERE kind=? AND id=? AND version=?',('profile',oid,version)).fetchone()
  return ProfileSpec(**json.loads(row[0])) if row else None
 def get_profile(self,oid,version):return self._run(lambda:self._profile(oid,version))
 def get_execution(self,wid):return self._run(lambda:self._get(wid))
 def list_executions(self):return self._run(lambda:[self._get(r[0]) for r in self.db.execute('SELECT id FROM executions').fetchall()])
 def find_receipt(self,request_id,payload_hash):
  def op():
   old=self.db.execute('SELECT hash,receipt FROM requests WHERE id=?',(request_id,)).fetchone()
   if old and old[0]!=payload_hash:raise ContractError('ALREADY_EXISTS','request ID payload conflict')
   return Receipt(**json.loads(old[1])) if old else None
  return self._run(op)
 def _receive(self,kind,obj,payload_hash):
  def op():
   with self.db:
    old=self.db.execute('SELECT hash,receipt FROM requests WHERE id=?',(obj.request_id,)).fetchone()
    if old:
     if old[0]!=payload_hash:raise ContractError('ALREADY_EXISTS','request ID payload conflict')
     return Receipt(**json.loads(old[1]))
    oid=obj.profile_id if kind=='profile' else obj.plan_id;ver=obj.version if kind=='profile' else obj.revision
    body=canonical(asdict(obj));prior=self.db.execute('SELECT body FROM objects WHERE kind=? AND id=? AND version=?',(kind,oid,ver)).fetchone()
    # Request IDs may differ but immutable logical content (including provenance) may not.
    if prior and json.loads(prior[0])['payload_json']!=obj.payload_json:raise ContractError('ALREADY_EXISTS','object version payload conflict')
    r=self._get(obj.workload_id) or ExecutionRecord(obj.workload_id)
    if kind=='profile' and r.job_uid and r.profile and r.profile.payload_json!=obj.payload_json:raise ContractError('FAILED_PRECONDITION','REPLAN_REQUIRED: active profile is immutable')
    if kind=='plan' and r.plan and r.plan.payload_json!=obj.payload_json:raise ContractError('FAILED_PRECONDITION','REPLAN_REQUIRED: existing immutable execution plan')
    self.db.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?)',(kind,oid,ver,body))
    p=obj if kind=='profile' else self._profile(obj.profile_id,obj.profile_version)
    q=obj if kind=='plan' else r.plan
    if q and kind=='profile':p=self._profile(q.profile_id,q.profile_version)
    if p and q:
     if p.workload_id!=q.workload_id or p.trace_id!=q.trace_id:raise ContractError('INVALID_ARGUMENT','profile/plan workload or trace mismatch')
    state='PLAN_VALIDATED' if p and q else ('PROFILE_WAIT' if q else 'PLAN_WAIT')
    new=replace(r,profile=p,plan=q,state=state if not r.job_uid and r.state not in TERMINAL else r.state,digest=config_digest(p,q) if p and q else '')
    self._save(new)
    receipt=Receipt(obj.request_id,'receipt-'+uuid.uuid4().hex,obj.workload_id,'RECEIVED' if obj.executable else 'PLAN_ONLY','stored; receipt is not approval',oid,ver,payload_hash)
    self.db.execute('INSERT INTO requests VALUES(?,?,?)',(obj.request_id,payload_hash,canonical(asdict(receipt))))
    self._event(new,dict(receipt_id=receipt.receipt_id,payload_sha256=payload_hash,request_id=obj.request_id))
    return receipt
  return self._run(op)
 def receive_profile(self,obj,payload_hash):return self._receive('profile',obj,payload_hash)
 def receive_plan(self,obj,payload_hash):return self._receive('plan',obj,payload_hash)
 def ready_to_submit(self,now):
  return [r for r in self.list_executions() if r.profile and r.plan and r.profile.executable and r.plan.executable and r.state not in TERMINAL and min(int(r.profile.expires_at_unix_s),int(r.plan.expires_at_unix_s))>now.timestamp()]
 def transition(self,wid,state,reason='',**updates):
  def op():
   with self.db:
    old=self._get(wid)
    if old is None:raise ContractError('NOT_FOUND','workload not found')
    if old.state in TERMINAL:return old
    r=replace(old,state=state,reason=reason,**updates);self._save(r)
    if (old.state,old.reason)!=(state,reason):self._event(r)
    return r
  return self._run(op)
 def record_job(self,wid,job_uid,name):
  old=self.get_execution(wid)
  if old.job_uid and old.job_uid!=job_uid:raise ContractError('ALREADY_EXISTS','job UID conflict')
  return self.transition(wid,'JOB_PENDING',job_uid=job_uid,job_name=name)
 def record_admission(self,wid,epoch,decision):
  old=self.get_execution(wid);reset={'evidence_json':'{}','sample_json':'{}'} if old.epoch!=epoch else {}
  return self.transition(wid,'APPROVED',epoch=epoch,keti_decision_id=decision,guard_decision_id='guard-'+sha(epoch)[:20],**reset)
 def record_assessment(self,wid,a):return self.transition(wid,a.state,a.reason,evidence_json=a.evidence_json,sample_json=a.sample_json)
 def events(self,trace_id='',after_seq=0,limit=100,workload_id=''):
  limit=max(1,min(int(limit or 100),500))
  return self._run(lambda:[dict(x) for x in self.db.execute('SELECT * FROM events WHERE seq>? AND (?="" OR trace_id=?) AND (?="" OR workload_id=?) ORDER BY seq LIMIT ?',(after_seq,trace_id,trace_id,workload_id,workload_id,limit)).fetchall()])
