from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
import argparse,logging,signal,threading,time
from .store import BridgeStore
from .grpc_service import make_grpc_server
from .controller import BridgeController
from .runtime_evidence import ExportReader
from .kube import KubeAdapter
@dataclass(frozen=True)
class BridgeSettings:
 image:str='ketris-institution-gemm:20261015-v1';db_path:str='/data/bridge.sqlite';token_path:str='/auth/token';bind:str='[::]:50051';namespace:str='ketris-integration-workloads';queue:str='ketris-integration-mig';flavor:str='ketris-integration-devmaster';node:str='devmaster';bindings_dir:str='/exports/bindings';partitions_dir:str='/exports/partitions';poll:float=1.0

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--db',default='/data/bridge.sqlite');parser.add_argument('--token-file',default='/auth/token');parser.add_argument('--bind',default='[::]:50051');parser.add_argument('--image',default=BridgeSettings.image);args=parser.parse_args()
 settings=BridgeSettings(image=args.image,db_path=args.db,token_path=args.token_file,bind=args.bind)
 from kubernetes import client,config
 config.load_incluster_config();kube=KubeAdapter(client.ApiClient());store=BridgeStore(settings.db_path);exports=ExportReader(settings.bindings_dir,settings.partitions_dir)
 logging.basicConfig(level=logging.INFO,format='%(message)s');stop=threading.Event()
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
 server=make_grpc_server(store,Path(settings.token_path).read_text().strip());server.add_insecure_port(settings.bind);server.start();controller=BridgeController(store,kube,exports,settings)
 try:
  while not stop.is_set():
   try:kube.activate_check();controller.reconcile_once(datetime.now(timezone.utc))
   except Exception as e:logging.warning('control cycle unavailable: %s',type(e).__name__)
   stop.wait(settings.poll)
 finally:server.stop(3).wait();store.close()
if __name__=='__main__':main()
