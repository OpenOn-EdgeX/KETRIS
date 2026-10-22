"""Isolated, fixed seven-worker experiment. Never changes operating controllers."""
import hashlib,json,os,pathlib,re,socket,subprocess,sys,threading,time
from policy import credit_grants,whole_makespan
P=pathlib.Path(__file__).resolve().parent
PROTOCOL=json.loads((P/'PROTOCOL.json').read_text())
WORKERS=PROTOCOL['workers'];IDS=[w['id'] for w in WORKERS]
UUID=PROTOCOL['gpu_uuid']
R=P/'runs';R.mkdir(exist_ok=True)
def save(path,obj):
 t=path.with_suffix(path.suffix+'.tmp');t.write_text(json.dumps(obj,indent=2,allow_nan=False));t.replace(path)
def guard():
 if (P/'foreign-owner.json').exists() or (P/'stop-all').exists():raise RuntimeError('ownership/stop flag')
 if time.time()-(P/'host-heartbeat.json').stat().st_mtime>15:raise RuntimeError('host observation stale')
def wait(seconds):
 until=time.monotonic()+seconds
 while time.monotonic()<until:guard();time.sleep(min(.05,max(0,until-time.monotonic())))
def cmd(args,**kw):return subprocess.check_output(args,text=True,timeout=20,**kw)
def env():
 v={k:v for k,v in os.environ.items() if k!='LD_PRELOAD' and not k.startswith('BLESS_')}
 v.update(CUDA_VISIBLE_DEVICES=UUID,CUDA_MPS_PIPE_DIRECTORY=str(P/'mps-pipe'),CUDA_MPS_LOG_DIRECTORY=str(P/'mps-log'),
          LD_LIBRARY_PATH='/opt/conda/lib/python3.11/site-packages/nvidia/cuda_runtime/lib',PYTHONPATH='/shared/pythonlibs:'+str(P),
          HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',TOKENIZERS_PARALLELISM='false',CUDA_MPS_CLIENT_PRIORITY='0')
 return v
def send(path,msg):
 with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s:s.settimeout(1);s.sendto(msg.encode(),str(path))
def stats(d,w,sock):
 f=d/f'feed-{w}.log';offset=f.stat().st_size if f.exists() else 0
 send(sock,'time_stats');send(sock,'gate_stats');deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  guard();raw=f.read_text()[offset:] if f.exists() else ''
  a=re.findall(r'total=(\d+) kernels=(\d+) credit=(-?\d+)',raw)
  b=re.findall(r'gate_count=(\d+) gate_total_us=(\d+) gate_max_us=(\d+)',raw)
  c=re.findall(r'refill_last_sequence=(\d+) accepted=(\d+) rejected=(\d+) active_cap_hits=(\d+) granted_us=(\d+) trimmed_us=(\d+)',raw)
  if a and b and c:return dict(zip(('charged_us','kernels','credit_us','gate_count','gate_total_us','gate_max_us','refill_sequence','refill_accepted','refill_rejected','cap_hits','granted_us','trimmed_us'),map(int,(*a[-1],*b[-1],*c[-1]))))
  time.sleep(.005)
 raise RuntimeError('runtime statistics timeout '+w)
def cool(d):
 rows=[];deadline=time.monotonic()+150
 while True:
  guard();raw=cmd(['nvidia-smi','-i',UUID,'--query-gpu=temperature.gpu,utilization.gpu,memory.used','--format=csv,noheader,nounits']).strip()
  values=[float(x) for x in raw.split(',')];rows.append({'epoch_ns':time.time_ns(),'values':values})
  if values[0]<=50 and values[1]<=5:break
  if time.monotonic()>deadline:raise RuntimeError('cooling timeout')
  wait(2)
 save(d/'cooling.json',rows)
def headroom():
 mem={line.split(':')[0]:int(line.split()[1])*1024 for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')}
 if mem['MemAvailable']<2*1024**3:raise RuntimeError('host memory headroom')
 raw=cmd(['nvidia-smi','-i',UUID,'--query-gpu=memory.used,memory.total','--format=csv,noheader,nounits'])
 used,total=map(float,raw.strip().split(','))
 if used/total>.90:raise RuntimeError('GPU memory headroom')
 return {'epoch_ns':time.time_ns(),'host_available':mem['MemAvailable'],'gpu_used_mib':used,'gpu_total_mib':total}
def trial(label,cap,policy,arm,category,repeat=0):
 guard()
 d=R/label;d.mkdir()
 cfg={'workers':WORKERS,'caps':[cap]*7,'policy':policy,'arm':arm,'category':category,'repeat':repeat,'gpu_uuid':UUID,'credit_bank_us':PROTOCOL['credit_bank_us']}
 save(d/'config.json',cfg)
 procs={};logs=[];sockets={};monitor=None;feeder=None;stop=threading.Event();events=[];errors=[];result=None
 print('START '+label,flush=True)
 try:
  cool(d);memory=[]
  for w in IDS:
   guard();memory.append(headroom());save(d/'prelaunch-memory.json',memory)
   v=env();v.update(RUN_DIR=str(d),WORKER_ID=w,CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=str(cap))
   if policy!='native':v.update(LD_PRELOAD=str(P/'libbless.so'),BLESS_SPACE_MODE='mps_env',BLESS_LIMIT_PCT=str(cap),BLESS_TIME_START_GATED='1',BLESS_GATE_METRICS='1',BLESS_TIME_WC='0',BLESS_STATS_LOG=str(d/f'feed-{w}.log'))
   f=(d/f'{w}.log').open('w');logs.append(f)
   procs[w]=subprocess.Popen([sys.executable,'-u',str(P/'worker.py')],env=v,stdout=f,stderr=subprocess.STDOUT)
   deadline=time.monotonic()+120
   while not (d/f'ready-{w}.json').exists():
    guard();s=pathlib.Path('/tmp')/f'bless-{procs[w].pid}.sock'
    if policy!='native' and w not in sockets and s.exists():sockets[w]=s;send(s,'time_unlimited')
    if procs[w].poll() is not None:raise RuntimeError(w+' startup failed: '+(d/f'{w}.log').read_text()[-1800:])
    if time.monotonic()>deadline:raise RuntimeError(w+' startup timeout')
    wait(.025)
   identity=json.loads((d/f'ready-{w}.json').read_text())
   assert identity['uuid'].removeprefix('GPU-')==UUID.removeprefix('GPU-')
   assert identity['reported_sms']=={100:188,75:140,50:94}[cap]
   assert identity['mps_cap']==str(cap) and identity['mps_priority']=='0'
   assert identity['qmd_callbacks']==0
  wait(2)
  before={}
  for w,s in sockets.items():
   send(s,'time_mode 1');send(s,'time_credit 0');before[w]=stats(d,w,s)
   assert before[w]['credit_us']==0
  save(d/'runtime-before.json',before)
  m0=time.monotonic_ns();wall=time.time_ns();m1=time.monotonic_ns();mono=(m0+m1)//2
  start=mono+1_000_000_000
  go={'start_ns':start,'start_epoch_ns':wall+start-mono,'clock_uncertainty_ns':(m1-m0)//2}
  if sockets:
   def feed():
    previous=None;next_at=start;last_stats=0
    try:
     while not stop.is_set():
      guard();now=time.monotonic_ns()
      if now<next_at:stop.wait(min(.001,(next_at-now)/1e9));continue
      phases={w:json.loads((d/f'phase-{w}.json').read_text())['phase'] for w in IDS}
      for w in IDS:
       if (d/f'result-{w}.json').exists():phases[w]='done'
      if all(p=='done' for p in phases.values()):break
      at=time.monotonic_ns();elapsed=10000 if previous is None else (at-previous)//1000
      grants=credit_grants(phases,elapsed,policy)
      caps={w:0 if phases[w]=='done' else cfg['credit_bank_us'] for w in IDS}
      for w,s in sockets.items():send(s,f'time_refill {at} {grants[w]} {caps[w]}')
      events.append({'at_ns':at,'elapsed_us':elapsed,'phases':phases,'grants_us':grants,'caps_us':caps})
      if at-last_stats>200_000_000:
       for s in sockets.values():send(s,'time_stats');send(s,'gate_stats')
       last_stats=at
      previous=at;next_at=at+10_000_000
    except BaseException as e:errors.append(repr(e));stop.set()
   feeder=threading.Thread(target=feed);feeder.start()
  f=(d/'nvml.csv').open('w');logs.append(f)
  monitor=subprocess.Popen(['nvidia-smi','-i',UUID,'--query-gpu=timestamp,utilization.gpu,utilization.memory,power.draw,temperature.gpu,clocks.current.sm,clocks.current.memory,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_power_brake_slowdown','--format=csv,noheader,nounits','-lms','200'],stdout=f,stderr=subprocess.STDOUT)
  save(d/'go.json',go);deadline=time.monotonic()+180
  while not all((d/f'result-{w}.json').exists() for w in IDS):
   guard()
   if errors:raise RuntimeError('feeder failed '+str(errors))
   if any(p.poll() is not None for p in procs.values()):raise RuntimeError('worker exited early')
   if time.monotonic()>deadline:raise RuntimeError('whole workload deadline')
   wait(.025)
  stop.set()
  if feeder:feeder.join(3)
  if errors:raise RuntimeError('feeder failed '+str(errors))
  if feeder and feeder.is_alive():raise RuntimeError('feeder stop failure')
  after={w:stats(d,w,s) for w,s in sockets.items()};save(d/'runtime-after.json',after)
  for w,b in before.items():
   a=after[w]
   assert a['refill_accepted']-b['refill_accepted']==len(events)
   assert a['refill_rejected']==b['refill_rejected']
   assert a['credit_us']-b['credit_us']==a['granted_us']-b['granted_us']-(a['trimmed_us']-b['trimmed_us'])-(a['charged_us']-b['charged_us'])
   assert a['granted_us']-b['granted_us']==sum(e['grants_us'][w] for e in events)
  results={w:json.loads((d/f'result-{w}.json').read_text()) for w in IDS}
  for work in WORKERS:
   data=results[work['id']];assert len(data['requests'])==work['requests']
   assert data['qmd_callbacks_after']==0
   for r in data['requests']:assert r['input_tokens']==work['input_tokens'] and len(r['token_ids'])==work['output_tokens']
  completions={w:r['completion_ns'] for w,r in results.items()}
  seconds=whole_makespan(start,completions)/1e9
  wait(.8)
  result={'label':label,'config':cfg,'makespan_s':seconds,'completed_requests':56,'input_tokens':28672,'generated_tokens':4480,'requests_s':56/seconds,'generated_tokens_s':4480/seconds,'runtime_before':before,'runtime_after':after,'feeder_ticks':len(events),'end_epoch_ns':go['start_epoch_ns']+max(completions.values())-start}
  print('DONE '+json.dumps({'label':label,'seconds':seconds,'requests_s':56/seconds,'gate_us':sum(a['gate_total_us']-before[w]['gate_total_us'] for w,a in after.items())}),flush=True)
 except BaseException as e:
  save(d/'error.json',{'error':repr(e),'epoch_ns':time.time_ns()});raise
 finally:
  stop.set()
  if feeder:feeder.join(3)
  save(d/'feeder-ticks.json',events)
  for s in sockets.values():
   try:send(s,'time_unlimited')
   except OSError:pass
  (d/'stop').touch()
  if monitor and monitor.poll() is None:monitor.terminate()
  exits={}
  for w,p in {**procs,**({'nvml':monitor} if monitor else {})}.items():
   try:rc=p.wait(timeout=15)
   except subprocess.TimeoutExpired:
    p.terminate()
    try:rc=p.wait(timeout=5)
    except subprocess.TimeoutExpired:p.kill();rc=p.wait(timeout=5)
   exits[w]={'pid':p.pid,'returncode':rc}
  for f in logs:f.close()
  save(d/'cleanup-complete.json',{'children':exits,'no_live_workers':all(p.poll() is not None for p in procs.values()),'completed_epoch_ns':time.time_ns()})
  if result is not None and any(exits[w]['returncode']!=0 for w in IDS):
   save(d/'error.json',{'error':'abnormal successful worker exit'});result=None
 if result is None:raise RuntimeError('run did not complete')
 save(d/'summary.json',result)
 return result

def main():
 guard();save(P/'run-start.json',{'epoch_ns':time.time_ns(),'source_hashes':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in P.glob('*.py')}})
 for name in ('mps-pipe','mps-log'):(P/name).mkdir(exist_ok=True)
 subprocess.run(['nvidia-cuda-mps-control','-d'],env=env(),check=True)
 try:
  pilots=[]
  for cap in (100,75,50):pilots.append(trial(f'pilot-space{cap}',cap,'native','native' if cap==100 else 'spatial','pilot'))
  best=min(pilots,key=lambda r:(r['makespan_s'],-r['config']['caps'][0]));cap=best['config']['caps'][0]
  credits=[trial(f'pilot-{p}',cap,p,'ketris','pilot') for p in ('phase100','phase90','phase75')]
  selected=min(credits[1:],key=lambda r:(r['makespan_s'],0 if r['config']['policy']=='phase90' else 1))
  policy=selected['config']['policy']
  # Fixed rotation and reverse rotations: preserve every run, never select a favourable holdout.
  orders=[['native','spatial','ketris'],['spatial','ketris','native'],['ketris','native','spatial'],['ketris','spatial','native'],['native','ketris','spatial']]
  planned=[{'label':f'holdout-r{i+1}-{a}','arm':a,'repeat':i+1,'cap':100 if a=='native' else cap,'policy':policy if a=='ketris' else 'native'} for i,order in enumerate(orders) for a in order]
  save(P/'frozen.json',{'epoch_ns':time.time_ns(),'selected_cap':cap,'selected_policy':policy,'pilots':pilots+credits,'holdouts':planned,'workers':WORKERS})
  print('FROZEN '+json.dumps({'cap':cap,'policy':policy,'runs':len(planned)}),flush=True)
  for item in planned:
   if time.time()>PROTOCOL['execution_deadline_epoch']:raise RuntimeError('time budget: stopped before new trial; partial results retained')
   trial(item['label'],item['cap'],item['policy'],item['arm'],'holdout',item['repeat'])
  save(P/'study-complete.json',{'epoch_ns':time.time_ns(),'holdouts':15,'pilots':6})
 finally:
  # This pipe belongs exclusively to this study; operating system MPS is untouched.
  r=subprocess.run(['nvidia-cuda-mps-control'],input='quit\n',text=True,capture_output=True,env=env(),timeout=20)
  save(P/'private-mps-stop.json',{'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
if __name__=='__main__':main()
