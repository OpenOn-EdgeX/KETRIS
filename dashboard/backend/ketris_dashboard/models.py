from datetime import datetime
import math
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)

class ObservedValue(StrictModel):
    value: float|str|bool|None=None
    unit: str|None=None
    source: Literal['kubernetes','gpu_driver','npu_driver','allocation','controller','runtime','archive']
    observed_at: str|None=None
    quality: Literal['observed','configured','derived','stale','unavailable','ambiguous']
    reason: str|None=None
    @field_validator('value')
    @classmethod
    def finite(cls,v):
        if isinstance(v,float) and not math.isfinite(v):raise ValueError('nonfinite observation')
        return v

class Device(StrictModel):
    id:str
    kind:Literal['gpu','npu']
    name:str
    node:str
    metrics:dict[str,ObservedValue]=Field(default_factory=dict)

class Partition(StrictModel):
    id:str
    device_id:str
    kind:Literal['mig','gpu','pe']
    name:str
    generation:str|None=None
    historical:bool=False
    pe_indices:list[int]=Field(default_factory=list)
    metrics:dict[str,ObservedValue]=Field(default_factory=dict)

class Workload(StrictModel):
    id:str
    name:str
    partition_id:str|None=None
    pod_uid:str|None=None
    container:str|None=None
    namespace:str|None=None
    origin:str='kubernetes'
    state:str='unavailable'
    requested:dict[str,ObservedValue]=Field(default_factory=dict)
    applied:dict[str,ObservedValue]=Field(default_factory=dict)
    observed:dict[str,ObservedValue]=Field(default_factory=dict)

class SourceStatus(StrictModel):
    status:Literal['ok','unavailable','stale','ambiguous']
    last_success:str|None=None
    reason:str|None=None

class ViewSnapshotV1(StrictModel):
    schema_version:Literal[1]=1
    node:str
    mode:Literal['live','replay']='live'
    captured_at:str|None
    devices:list[Device]=Field(default_factory=list)
    partitions:list[Partition]=Field(default_factory=list)
    workloads:list[Workload]=Field(default_factory=list)
    source_status:dict[str,SourceStatus]=Field(default_factory=dict)
    @field_validator('captured_at')
    @classmethod
    def utc_timestamp(cls,v):
        if v is None:return v
        d=datetime.fromisoformat(v.replace('Z','+00:00'))
        if d.tzinfo is None:raise ValueError('timestamp must include timezone')
        return v
    @model_validator(mode='after')
    def live_requires_timestamp(self):
        if self.mode=='live' and self.captured_at is None:raise ValueError('live timestamp missing')
        return self

class ReplayFrame(StrictModel):
    index:int=Field(ge=0)
    interval:dict[str,float|str|None]
    axis_kind:Literal['measurement_window','elapsed','utc']
    snapshot:ViewSnapshotV1
    evidence_ids:list[str]=Field(default_factory=list)

def observed(value,unit,source,at=None,quality='observed',reason=None):
    return ObservedValue(value=value,unit=unit,source=source,observed_at=at,
        quality=quality if value is not None else 'unavailable',reason=reason if value is not None else reason or 'source has no value')
