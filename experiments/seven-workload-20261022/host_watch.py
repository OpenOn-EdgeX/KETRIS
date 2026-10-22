"""Read-only owner guard. Existing system MPS may remain only with zero clients."""
import json,os,pathlib,subprocess,time
P=pathlib.Path(__file__).resolve().parent
def save(name,x):
 t=P/(name+'.tmp');t.write_text(json.dumps(x));t.replace(P/name)
def query(args,**kwargs):return subprocess.check_output(args,text=True,timeout=10,**kwargs)
pods=json.loads(query(['kubectl','get','pods','-n','ketris-seven-20261022','-o','json']))
uid=next(x['metadata']['uid'] for x in pods['items'] if x['metadata']['name']=='seven-lab')
system_pid='13025'
system_start=pathlib.Path('/proc',system_pid,'stat').read_text().split()[21]
save('watch-identity.json',{'pod_uid':uid,'system_idle_mps_pid':system_pid,'system_mps_starttime':system_start})
while not (P/'stop-watch').exists():
 try:
  rows=[]
  for line in query(['nvidia-smi','--query-compute-apps=pid,process_name,used_memory','--format=csv,noheader']).splitlines():
   pid=line.split(',')[0].strip()
   try:cg=pathlib.Path('/proc',pid,'cgroup').read_text()
   except FileNotFoundError:continue
   owned=uid in cg or uid.replace('-','_') in cg
   idle_system=False
   if pid==system_pid and cg.strip().endswith('/system.slice/nvidia-mps.service'):
    if pathlib.Path('/proc',pid,'stat').read_text().split()[21]!=system_start:raise RuntimeError('system MPS process replaced')
    control=subprocess.run(['nvidia-cuda-mps-control'],input='get_client_list '+pid+'\n',text=True,capture_output=True,timeout=5,env={**os.environ,'CUDA_MPS_PIPE_DIRECTORY':'/tmp/nvidia-mps'})
    if control.returncode or control.stdout.strip() or control.stderr.strip():raise RuntimeError('operating MPS clients or query error: '+control.stdout+control.stderr)
    idle_system=True
   rows.append({'pid':pid,'cgroup':cg,'owned':owned,'allowed_idle_system_mps':idle_system,'smi':line})
   if not owned and not idle_system:raise RuntimeError('foreign GPU owner '+pid)
  x={'epoch_ns':time.time_ns(),'processes':rows}
  with (P/'ownership.jsonl').open('a') as f:f.write(json.dumps(x)+'\n')
  save('host-heartbeat.json',x)
 except BaseException as e:
  save('foreign-owner.json',{'epoch_ns':time.time_ns(),'error':repr(e)})
  raise
 time.sleep(2)
