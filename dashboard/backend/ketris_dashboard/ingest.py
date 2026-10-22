import json,secrets
from fastapi import FastAPI,HTTPException,Request
from pydantic import Field,ValidationError
from .models import StrictModel,ViewSnapshotV1
class Envelope(StrictModel):
    session:str=Field(min_length=1,max_length=128)
    sequence:int=Field(ge=0,strict=True)
    snapshot:ViewSnapshotV1

def ingest_app(store,token):
    if not token or len(token)<8:raise ValueError('ingest token required')
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.get('/internal/v1/health')
    def health():return {'ok':True}
    @app.post('/internal/v1/snapshots',status_code=202)
    async def ingest(request:Request):
        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token):raise HTTPException(401,'authentication required')
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>1048576:raise HTTPException(413,'payload too large')
        try:data=Envelope.model_validate_json(body)
        except ValidationError:raise HTTPException(422,'invalid snapshot schema')
        try:store.accept(data.session,data.sequence,data.snapshot)
        except ValueError as e:raise HTTPException(409,str(e))
        return {'accepted':True}
    return app
