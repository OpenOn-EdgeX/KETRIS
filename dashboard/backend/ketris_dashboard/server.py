import argparse,json,logging,os,threading
from pathlib import Path
import uvicorn
from .api import public_app
from .ingest import ingest_app
from .store import ExperimentStore
from .archive import DEFAULT_ROOTS,ExperimentBundle,parse_experiment
from .models import ReplayFrame

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',default='/data/ketris.sqlite');p.add_argument('--token-file',default='/var/run/ketris-auth/token')
    p.add_argument('--static',default='/opt/ketris/frontend');p.add_argument('--bundles',default='/opt/ketris/archive-bundles')
    p.add_argument('--host',default='0.0.0.0');p.add_argument('--port',type=int,default=8080);p.add_argument('--ingest-port',type=int,default=8081);a=p.parse_args()
    Path(a.db).parent.mkdir(parents=True,exist_ok=True);token=Path(a.token_file).read_text().strip();store=ExperimentStore(a.db)
    try:
        for path in DEFAULT_ROOTS:
            if path.is_dir():store.import_bundle(parse_experiment(path))
        for file in Path(a.bundles).glob('*.json'):
            data=json.loads(file.read_text());data['frames']=[ReplayFrame.model_validate(f) for f in data['frames']]
            store.import_bundle(ExperimentBundle(**data))
        private=uvicorn.Server(uvicorn.Config(ingest_app(store,token),host=a.host,port=a.ingest_port,access_log=False,log_level='warning'))
        thread=threading.Thread(target=private.run,daemon=True);thread.start()
        uvicorn.run(public_app(store,a.static),host=a.host,port=a.port,access_log=False,log_level='warning')
    finally:
        if 'private' in locals():private.should_exit=True;thread.join(timeout=5)
        store.close()
if __name__=='__main__':main()
