"""Opt-in full-GPU phase experiment; never loaded by the operating daemon."""
import ctypes, json, mmap, os, pathlib, queue, struct, threading, time
from ketris_mig.phase_policy import PhasePolicy
P=pathlib.Path(os.environ['PHASE_DIR']);role=os.environ['PHASE_ROLE'];cfg=json.loads((P/'config.json').read_text())
policy=cfg['policy'];spatial=policy in ('spatial','combined');bounded=policy in ('submission','combined')
fd=os.open(str(P/'phase.bin'),os.O_RDWR);state=mmap.mmap(fd,32);os.close(fd)
phase_codes={'idle':0,'prefill':1,'decode':2};names={v:k for k,v in phase_codes.items()}
phases=[];applied=[];sequence=0
planner=PhasePolicy(94,cfg['prefill_tpcs'],cfg['decode_tpcs'],cfg['backgrounds'],1_000_000_000,share_background=cfg.get('share_background',False))
def publish(phase):
 global sequence
 sequence+=2;now=time.monotonic_ns();struct.pack_into('Q',state,0,sequence-1)
 struct.pack_into('QQ',state,8,phase_codes[phase],now);struct.pack_into('Q',state,0,sequence)
 phases.append({'phase':phase,'at_ns':now,'epoch_ns':time.time_ns(),'sequence':sequence})
def read_phase():
 seq=struct.unpack_from('Q',state,0)[0];code,stamp=struct.unpack_from('QQ',state,8)
 if seq%2 or seq!=struct.unpack_from('Q',state,0)[0]:return 'invalid',0
 return names.get(code,'invalid'),stamp

def save(name,obj):
 t=P/(name+'.tmp-'+role);t.write_text(json.dumps(obj,allow_nan=False));t.replace(P/name)
def wait_file(name,timeout=900):
 end=time.monotonic()+timeout
 while not (P/name).exists():
  if (P/'stop').exists() or time.monotonic()>end:raise TimeoutError(name)
  time.sleep(.01)
 return json.loads((P/name).read_text())

import torch, transformers
from transformers import AutoTokenizer,AutoModelForCausalLM
torch.set_num_threads(1);torch.set_num_interop_threads(1);torch.cuda.set_device(0)
lib=None
if policy!='native':
 lib=ctypes.CDLL('/lab/libbless.so');lib.ketris_qmd_launches.restype=ctypes.c_ulonglong
 lib.ketris_qmd_range.argtypes=[ctypes.c_uint,ctypes.c_uint];lib.ketris_qmd_range.restype=ctypes.c_int
model=AutoModelForCausalLM.from_pretrained('/shared/model',local_files_only=True,dtype=torch.bfloat16,attn_implementation='eager').eval().to('cuda')
tok=AutoTokenizer.from_pretrained('/shared/model',local_files_only=True)
context='This is an engineering test about GPU scheduling, inference latency, and fair background processing. '
prompts=['Explain why sharing one GPU needs both a resource limit and a scheduling policy.','Give two practical examples of low latency AI services at the network edge.','Explain time credits for GPU inference in clear simple language.','What measurements should an engineer record when comparing inference schedulers?']
a_inputs=[]
for s in prompts:
 text=tok.apply_chat_template([{'role':'user','content':context*20+s}],tokenize=False,add_generation_prompt=True)
 a_inputs.append(tok(text,return_tensors='pt')['input_ids'][:,-512:].to('cuda'))
bg=tok(context*400,return_tensors='pt')['input_ids'][:,:cfg['bg_length']].repeat(cfg['bg_batch'],1).to('cuda')
torch.cuda.synchronize()
last_range=None;hooks_on=False;wait_ns=0

def apply_range(pair,phase):
 global last_range
 if pair==last_range:return
 torch.cuda.current_stream().synchronize()
 began=time.monotonic_ns();observed=lib.ketris_qmd_range(*pair)
 if observed!=2*pair[1]:raise RuntimeError('QMD actuator rejected range')
 applied.append({'at_ns':began,'epoch_ns':time.time_ns(),'phase':phase,'start_tpc_index':pair[0],'tpcs':pair[1],'reported_applied_sms':observed,'call_ns':time.monotonic_ns()-began})
 last_range=pair

def background_boundary(*unused):
 global wait_ns
 if not hooks_on:return
 phase,stamp=read_phase();decision=planner.decide(phase,stamp,time.monotonic_ns())
 if bounded:
  decision=planner.after_drain(read_phase,torch.cuda.current_stream().synchronize,time.monotonic_ns)
  began=time.monotonic_ns()
  while decision['pause_background']:
   if (P/'stop').exists():raise RuntimeError('stopped')
   time.sleep(.0001);phase,stamp=read_phase();decision=planner.decide(phase,stamp,time.monotonic_ns())
  wait_ns+=time.monotonic_ns()-began
 elif spatial and decision['ranges'][role]!=last_range:
  decision=planner.after_drain(read_phase,torch.cuda.current_stream().synchronize,time.monotonic_ns)
 if spatial:apply_range(decision['ranges'][role],decision['phase'])

handles=[]
if role!='A' and (spatial or bounded):
 for layer in model.model.layers:handles.append(layer.register_forward_pre_hook(background_boundary))

@torch.inference_mode()
def generate(tokens,n,signal=True):
 cache=None;ids=tokens;outputs=[];first=None
 if signal:publish('prefill')
 for i in range(n):
  if signal and i==1:publish('decode')
  if spatial and signal:apply_range((0,cfg['prefill_tpcs'] if i==0 else cfg['decode_tpcs']),'prefill' if i==0 else 'decode')
  output=model(input_ids=ids,past_key_values=cache,use_cache=True,logits_to_keep=1)
  ids=torch.argmax(output.logits[:,-1,:],dim=-1,keepdim=True);cache=output.past_key_values
  torch.cuda.synchronize()
  if first is None:first=time.monotonic_ns()
  outputs.append(ids.item())
 done=time.monotonic_ns()
 if signal:publish('idle')
 return first,done,outputs
@torch.inference_mode()
def batch():
 output=model(input_ids=bg,use_cache=False,logits_to_keep=1);torch.cuda.synchronize()
 return float(output.logits[0,-1,0].item())
if role=='A':generate(a_inputs[0],16,False)
else:
 for _ in range(3):batch()
props=torch.cuda.get_device_properties(0)
identity={'uuid':str(props.uuid),'reported_sms':props.multi_processor_count,'model_parameters':sum(p.numel() for p in model.parameters()),'dtype':'bfloat16','torch':torch.__version__,'transformers':transformers.__version__,'pid':os.getpid(),'policy':policy,'mps_cap':os.environ['CUDA_MPS_ACTIVE_THREAD_PERCENTAGE'],'qmd_callbacks':lib.ketris_qmd_launches() if lib else 0}
assert identity['uuid']=='4a42bf67-df51-7582-9e81-b9dddd4bcea2'
if spatial:assert identity['qmd_callbacks']>0,'LLM launches did not reach QMD callback'
save('ready-'+role+'.json',identity)
go=wait_file('go.json');start=go['start_ns'];end=go['end_ns']
while time.monotonic_ns()<start:time.sleep(.0002)
hooks_on=True;rows=[]
if role=='A':
 incoming=queue.Queue()
 def producer():
  for i,offset in enumerate(go['arrivals_ns']):
   release=start+offset
   while time.monotonic_ns()<release:time.sleep(.0001)
   incoming.put((i,release,time.monotonic_ns()))
 t=threading.Thread(target=producer);t.start()
 for _ in go['arrivals_ns']:
  i,release,enqueue=incoming.get(timeout=90);began=time.monotonic_ns()
  first,done,outputs=generate(a_inputs[i%4],go['output_tokens'])
  rows.append({'request_id':i,'release_ns':release,'enqueue_ns':enqueue,'start_ns':began,'first_token_ns':first,'completion_ns':done,'generated_tokens':len(outputs),'token_ids':outputs,'input_tokens':int(a_inputs[i%4].numel())})
 t.join()
else:
 while time.monotonic_ns()<end or not (P/'result-A.json').exists():
  if (P/'stop').exists():raise RuntimeError('stopped')
  began=time.monotonic_ns();checksum=batch();done=time.monotonic_ns()
  rows.append({'start_ns':began,'end_ns':done,'prompts':cfg['bg_batch'],'input_tokens':int(bg.numel()),'checksum':checksum})
save('result-'+role+'.json',{'role':role,'requests' if role=='A' else 'batches':rows,'identity':identity,'phase_events':phases,'applied':applied,'wait_ns':wait_ns,'qmd_callbacks_after':lib.ketris_qmd_launches() if lib else 0,'finished_epoch_ns':time.time_ns()})
print(json.dumps({'role':role,'done':len(rows),'applied_changes':len(applied),'qmd_callbacks':lib.ketris_qmd_launches() if lib else 0}),flush=True)
