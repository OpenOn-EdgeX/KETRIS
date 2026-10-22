"""Shared v1 contracts. Importing this module never queries or controls a device."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_uuid(value: str, kind: str = 'MIG') -> str:
    if not isinstance(value, str) or not re.fullmatch(kind + '-' + _UUID, value):
        raise ValueError(f'invalid {kind} UUID')
    return kind + '-' + value[4:].lower()


def bounded_int(value: Any, name: str, high: int) -> int:
    if type(value) is not int or not 1 <= value <= high:
        raise ValueError(f'{name} must be an integer in 1..{high}')
    return value


def resource_name(mig_uuid: str) -> str:
    digest = hashlib.sha256(device_uuid(mig_uuid).encode()).hexdigest()[:16]
    return f'keti.re.kr/mig-{digest}-share'


def strict_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class ShareRequest:
    mig_uuid: str
    space_percent: int
    time_weight: int = 1

    def __post_init__(self):
        object.__setattr__(self, 'mig_uuid', device_uuid(self.mig_uuid))
        bounded_int(self.space_percent, 'space_percent', 100)
        bounded_int(self.time_weight, 'time_weight', 1000)


def validate_request(value: dict) -> ShareRequest:
    if not isinstance(value, dict) or set(value) - {'mig_uuid', 'space_percent', 'time_weight'}:
        raise ValueError('unknown or malformed share request')
    try:
        return ShareRequest(**value)
    except TypeError as exc:
        raise ValueError('missing or invalid share request fields') from exc


@dataclass
class AllocationBindingV1:
    allocation_id: str
    node: str
    gpu_uuid: str
    mig_uuid: str
    generation: str
    resource_name: str
    device_ids: list[str]
    space_percent: int
    control_dir: str
    pod_uid: str | None = None
    container: str | None = None
    time_weight: int | None = None
    phase: str = 'allocated'
    container_id: str | None = None
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def __post_init__(self):
        self.gpu_uuid = device_uuid(self.gpu_uuid, 'GPU')
        self.mig_uuid = device_uuid(self.mig_uuid)
        if self.schema_version != 1 or self.resource_name != resource_name(self.mig_uuid):
            raise ValueError('allocation resource does not match the MIG identity/version')
        bounded_int(self.space_percent, 'space_percent', 100)
        if not self.device_ids or len(self.device_ids) != len(set(self.device_ids)):
            raise ValueError('device IDs must be nonempty and unique')
        if len(self.device_ids) != self.space_percent:
            raise ValueError('requested units must equal MPS space percentage')
        if not all(isinstance(v, str) and v for v in [self.allocation_id, self.node, self.generation, self.control_dir]):
            raise ValueError('allocation identity cannot be empty')
        if self.phase not in {'allocated', 'bound', 'armed', 'completed', 'failed'}:
            raise ValueError('invalid allocation phase')
        if self.time_weight is not None:
            bounded_int(self.time_weight, 'time_weight', 1000)
        if self.phase in {'bound', 'armed'} and not (self.pod_uid and self.container and self.time_weight):
            raise ValueError('bound allocation requires Pod/container/time contract')

    def to_dict(self) -> dict:
        result = asdict(self)
        strict_json(result)
        return result

    @classmethod
    def from_dict(cls, value: dict):
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError('malformed allocation binding') from exc


@dataclass
class PartitionStateV1:
    node: str
    gpu_uuid: str
    mig_uuid: str
    generation: str
    inventory: dict
    manager_state: str
    budget_domain: str
    tenants: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    observed_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def to_dict(self) -> dict:
        result = asdict(self)
        device_uuid(self.gpu_uuid, 'GPU')
        device_uuid(self.mig_uuid)
        strict_json(result)
        return result
