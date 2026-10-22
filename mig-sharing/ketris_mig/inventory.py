"""NVML inventory for independently controlled, one-CI-per-GI MIG instances."""
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
from .contracts import device_uuid, resource_name


def _text(v):
    return v.decode().rstrip('\0') if isinstance(v, bytes) else str(v)


@dataclass(frozen=True)
class MigInventoryEntry:
    gpu_uuid: str
    gpu_index: int
    mig_uuid: str
    gi_id: int
    ci_id: int
    profile: str | None
    memory_mib: int | None
    profile_sm: int | None
    generation: str

    @property
    def resource_name(self): return resource_name(self.mig_uuid)

    def to_dict(self): return {**asdict(self), 'resource_name': self.resource_name}


class NvmlAdapter:
    def scan(self) -> list[dict]:
        import pynvml as n
        n.nvmlInit()
        rows = []
        try:
            for i in range(n.nvmlDeviceGetCount()):
                parent = n.nvmlDeviceGetHandleByIndex(i)
                try: mode, _ = n.nvmlDeviceGetMigMode(parent)
                except n.NVMLError_NotSupported: continue
                if mode != n.NVML_DEVICE_MIG_ENABLE: continue
                parent_uuid = _text(n.nvmlDeviceGetUUID(parent))
                profiles = {}
                for p in range(getattr(n, 'NVML_GPU_INSTANCE_PROFILE_COUNT', 32)):
                    try: info = n.nvmlDeviceGetGpuInstanceProfileInfo(parent, p)
                    except (n.NVMLError_InvalidArgument, n.NVMLError_NotSupported): continue
                    profiles[info.id] = info
                for slot in range(n.nvmlDeviceGetMaxMigDeviceCount(parent)):
                    try: child = n.nvmlDeviceGetMigDeviceHandleByIndex(parent, slot)
                    except n.NVMLError_NotFound: continue
                    gi, ci = n.nvmlDeviceGetGpuInstanceId(child), n.nvmlDeviceGetComputeInstanceId(child)
                    gi_info = n.nvmlGpuInstanceGetInfo(n.nvmlDeviceGetGpuInstanceById(parent, gi))
                    profile = profiles.get(gi_info.profileId)
                    name = getattr(profile, 'name', None)
                    rows.append({'gpu_uuid': parent_uuid, 'gpu_index': i,
                                 'mig_uuid': _text(n.nvmlDeviceGetUUID(child)), 'gi_id': gi, 'ci_id': ci,
                                 'profile': _text(name) if name else None,
                                 'profile_sm': int(profile.multiprocessorCount) if profile else None,
                                 'memory_mib': int(n.nvmlDeviceGetMemoryInfo(child).total // (1024*1024))})
            return rows
        finally: n.nvmlShutdown()


def read_inventory(adapter: NvmlAdapter | None = None) -> list[MigInventoryEntry]:
    raw = (adapter or NvmlAdapter()).scan()
    groups = Counter((r['gpu_uuid'], r['gi_id']) for r in raw)
    seen, names, entries = set(), {}, []
    for row in raw:
        uuid, parent = device_uuid(row['mig_uuid']), device_uuid(row['gpu_uuid'], 'GPU')
        if uuid in seen: raise ValueError('duplicate MIG identity')
        seen.add(uuid)
        if groups[(row['gpu_uuid'], row['gi_id'])] != 1: continue
        name = resource_name(uuid)
        if name in names and names[name] != uuid: raise ValueError('MIG resource hash collision')
        names[name] = uuid
        generation = hashlib.sha256(f'{parent}/{uuid}/{row["gi_id"]}/{row["ci_id"]}'.encode()).hexdigest()[:24]
        entries.append(MigInventoryEntry(parent, row['gpu_index'], uuid, row['gi_id'], row['ci_id'],
                      row.get('profile'), row.get('memory_mib'), row.get('profile_sm'), generation))
    return entries


def resolve_mig(uuid: str, inventory: list[MigInventoryEntry]) -> MigInventoryEntry:
    uuid = device_uuid(uuid)
    for item in inventory:
        if item.mig_uuid == uuid: return item
    raise ValueError('MIG does not exist or its geometry is not supported')
