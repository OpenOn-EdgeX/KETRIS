from pathlib import Path
from datetime import datetime
from fastapi import FastAPI,HTTPException,Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

def public_app(store,static_dir=None):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.get('/api/v1/health')
    def health():return {'ok':True,'schema_version':1,'mode':'read-only'}
    @app.get('/api/v1/nodes')
    def nodes():return store.nodes()
    @app.get('/api/v1/nodes/{node}/snapshot')
    def snapshot(node):
        s=store.snapshot(node,stale=True)
        if s is None:raise HTTPException(404,'node is not observed')
        return s
    @app.get('/api/v1/nodes/{node}/history')
    def history(node,cursor:int=Query(0,ge=0),limit:int=Query(500,ge=1,le=500),recent:bool=False,at:datetime|None=None):
        if at is not None and at.tzinfo is None:raise HTTPException(422,'history timestamp requires timezone')
        return store.history(node,cursor,limit,recent=recent,at=at.isoformat() if at else None)
    @app.get('/api/v1/experiments')
    def experiments():return store.experiments()
    @app.get('/api/v1/experiments/{id}')
    def experiment(id):
        data=store.experiment(id)
        if data is None:raise HTTPException(404,'experiment not found')
        return data
    @app.get('/api/v1/experiments/{id}/frames')
    def frames(id,cursor:int=Query(0,ge=0),limit:int=Query(500,ge=1,le=500)):
        return store.load_frames(id,cursor,limit)
    @app.get('/api/v1/evidence/{id}')
    def evidence(id):
        try:data=store.evidence(id)
        except ValueError:raise HTTPException(404,'evidence not found')
        if data is None:raise HTTPException(404,'evidence not found')
        return data
    if static_dir and (Path(static_dir)/'index.html').is_file():
        app.mount('/assets',StaticFiles(directory=Path(static_dir)/'assets'),name='assets')
        @app.get('/')
        def index():return FileResponse(Path(static_dir)/'index.html')
    return app
