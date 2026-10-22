"""A budget domain for one MIG. Charged kernel wall time is not GPU busy time."""
import math
import socket
import threading
from .contracts import PartitionStateV1,bounded_int,utc_now

def split_budget(weights,total_us=10000):
    bounded_int(total_us,'total_us',1000000)
    for w in weights.values(): bounded_int(w,'time_weight',1000)
    if not weights: return {}
    denom=sum(weights.values());out={k:total_us*w//denom for k,w in weights.items()}
    order=sorted(weights,key=lambda k:(-(total_us*weights[k]%denom),k))
    for k in order[:total_us-sum(out.values())]: out[k]+=1
    return out

def send_command(path,command):
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s:
        s.settimeout(.005);s.sendto(command.encode(),path)

class PartitionController:
    def __init__(self,node,entry,sender=send_command):
        self.node=node;self.entry=entry;self.sender=sender;self.tenants={};self.samples={}
        self.lock=threading.RLock();self.events=[]

    def reconcile(self,bindings):
        with self.lock:
            wanted={b['allocation_id']:dict(b) for b in bindings if b.get('phase')=='bound'}
            for key,old in self.tenants.items():
                new=wanted.get(key)
                if not new or (new['socket'],new['runtime_generation'])!=(old['socket'],old['runtime_generation']):
                    try: self.sender(old['socket'],'time_credit 0')
                    except OSError: pass
                    self.samples.pop(key,None)
            active={}
            for key,new in wanted.items():
                old=self.tenants.get(key)
                same=old and (new['socket'],new['runtime_generation'])==(old['socket'],old['runtime_generation'])
                if not same:
                    try: self.sender(new['socket'],'time_credit 0')
                    except OSError: continue
                    self.events.append({'kind':'armed','allocation_id':key,'at':utc_now(),'runtime_generation':new['runtime_generation']})
                new.update({k:old.get(k) if same else None for k in ['observed_share','charged_total_us','kernels','credit_us']})
                new['phase']='armed';active[key]=new
            self.tenants=active
            budget=split_budget({k:v['time_weight'] for k,v in active.items()})
            for k,v in active.items():
                v['budget_us']=budget[k];v['configured_share']=budget[k]/10000
            self.events=self.events[-64:]

    def tick(self):
        with self.lock:
            for key,t in list(self.tenants.items()):
                try: self.sender(t['socket'],f'time_add {t["budget_us"]}')
                except OSError:
                    t['phase']='failed';self.tenants.pop(key);self.samples.pop(key,None)

    def observe(self,key,total,kernels,at,credit=None):
        with self.lock:
            if key not in self.tenants: return
            old=self.samples.get(key);delta=None
            if old and 0<at-old[2]<=30 and total>=old[0] and kernels>=old[1]: delta=total-old[0]
            self.samples[key]=(total,kernels,at,delta)
            self.tenants[key].update(charged_total_us=total,kernels=kernels,credit_us=credit)
            samples=[self.samples.get(k) for k in self.tenants]
            complete=all(s and s[3] is not None for s in samples)
            denom=sum(s[3] for s in samples) if complete else 0
            coherent=complete and max(s[2] for s in samples)-min(s[2] for s in samples)<.5
            for k,t in self.tenants.items():
                t['observed_share']=self.samples[k][3]/denom if coherent and denom>0 else None
                t['observation_quality']='observed' if t['observed_share'] is not None else 'unavailable'

    def export_state(self):
        with self.lock:
            return PartitionStateV1(self.node,self.entry.gpu_uuid,self.entry.mig_uuid,self.entry.generation,self.entry.to_dict(),
                'running',self.entry.mig_uuid+':'+self.entry.generation,[dict(t) for t in self.tenants.values()],list(self.events))
