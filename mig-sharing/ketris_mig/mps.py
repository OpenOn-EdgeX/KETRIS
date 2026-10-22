"""Only manage servers bearing our own node ownership record.

Transient host systemd units outlive the runtime Pod. No global MPS quit is used.
"""
import os
from pathlib import Path
import subprocess
import time
from .state import atomic_json,read_json

def run(args,**kw):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=10,**kw).stdout

def alive(pid):
    try: os.kill(pid,0);return True
    except ProcessLookupError: return False

class MpsManager:
    def __init__(self,entry,root,runner=run,pid_alive=alive):
        self.entry=entry;self.runner=runner;self.pid_alive=pid_alive
        suffix=entry.resource_name.split('mig-')[1].split('-')[0]+'-'+entry.generation[:8]
        self.unit='ketris-mps-'+suffix+'.service'
        self.base=Path(root)/'mps'/suffix;self.pipe=self.base/'pipe';self.log=self.base/'log'
        self.marker=self.base/'owner.json';self.pid=None

    def ensure(self):
        if self.marker.exists():
            mark=read_json(self.marker)
            if mark.get('mig_uuid')!=self.entry.mig_uuid or mark.get('generation')!=self.entry.generation or mark.get('unit')!=self.unit:
                raise RuntimeError('MPS ownership identity mismatch')
        else:
            if self.base.exists() and any(self.base.iterdir()):
                raise RuntimeError('refusing to adopt an external MPS directory')
            # Also reject an existing unit without our durable ownership marker.
            if self.runner(['systemctl','show',self.unit,'--property=LoadState','--value']).strip() not in ('','not-found'):
                raise RuntimeError('refusing to adopt an external systemd unit')
            atomic_json(self.marker,{'mig_uuid':self.entry.mig_uuid,'generation':self.entry.generation,'unit':self.unit})
        pidfile=self.pipe/'nvidia-cuda-mps-control.pid'
        if pidfile.exists():
            pid=int(pidfile.read_text().strip())
            if self.pid_alive(pid): self.pid=pid;return self
        self.pipe.mkdir(parents=True,exist_ok=True);self.log.mkdir(parents=True,exist_ok=True)
        load=self.runner(['systemctl','show',self.unit,'--property=LoadState','--value']).strip()
        if load not in ('','not-found'):
            self.runner(['systemctl','stop',self.unit])
            self.runner(['systemctl','reset-failed',self.unit])
        self.runner(['systemd-run','--unit='+self.unit,'--property=Type=forking',
                     '--setenv=CUDA_VISIBLE_DEVICES='+self.entry.mig_uuid,
                     '--setenv=CUDA_MPS_PIPE_DIRECTORY='+str(self.pipe),
                     '--setenv=CUDA_MPS_LOG_DIRECTORY='+str(self.log),
                     '/usr/bin/nvidia-cuda-mps-control','-d'])
        for _ in range(40):
            if pidfile.exists():
                pid=int(pidfile.read_text().strip())
                if self.pid_alive(pid):
                    self.pid=pid;atomic_json(self.marker,{'mig_uuid':self.entry.mig_uuid,'generation':self.entry.generation,'unit':self.unit,'pid':pid});return self
            time.sleep(.05)
        raise RuntimeError('owned MPS server failed to become ready')

    def stop(self,tenants):
        if tenants or not self.marker.exists(): return
        mark=read_json(self.marker)
        if mark.get('unit')!=self.unit or mark.get('mig_uuid')!=self.entry.mig_uuid: return
        self.runner(['systemctl','stop',self.unit]);self.pid=None

