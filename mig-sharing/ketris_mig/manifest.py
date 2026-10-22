"""Generate an exact resource-bound Pod manifest; never starts a workload."""
import argparse
import json
from .admission import ANNOTATION, CANONICAL, validate_pod
from .contracts import ShareRequest
from .inventory import read_inventory, resolve_mig


def build_manifest(request: ShareRequest, image: str, namespace: str, command: list[str], inventory=None) -> dict:
    inventory=read_inventory() if inventory is None else inventory
    entry=resolve_mig(request.mig_uuid,inventory)
    if not image or not namespace or not command: raise ValueError('image, namespace and command are required')
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'ketris-mig-workload','namespace':namespace,
        'annotations':{ANNOTATION+'mig-uuid':entry.mig_uuid,ANNOTATION+'time-weight':str(request.time_weight)}},
        'spec':{'restartPolicy':'Never','containers':[{'name':'workload','image':image,
        'command':command,'env':[{'name':k,'value':v} for k,v in {**CANONICAL,'KETRIS_TIME_WEIGHT':str(request.time_weight)}.items()],
        'resources':{'limits':{entry.resource_name:request.space_percent},'requests':{entry.resource_name:request.space_percent}}}]}}
    validate_pod(pod,inventory)
    return pod


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mig-uuid',required=True)
    parser.add_argument('--space-percent',type=int,required=True);parser.add_argument('--time-weight',type=int,default=1)
    parser.add_argument('--image',required=True);parser.add_argument('--namespace',default='default')
    parser.add_argument('command',nargs=argparse.REMAINDER);a=parser.parse_args()
    command=a.command[1:] if a.command[:1]==['--'] else a.command
    print(json.dumps(build_manifest(ShareRequest(a.mig_uuid,a.space_percent,a.time_weight),a.image,a.namespace,command),indent=2))

if __name__=='__main__': main()
