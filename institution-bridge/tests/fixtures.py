from datetime import datetime, timezone
NOW = datetime(2026,10,15,10,0,tzinfo=timezone.utc)
UUID = 'MIG-bb051536-d0bd-581a-940d-3ec3054836c8'
KEY = 'keti.re.kr/mig-e2493d0ebc3c0f31-share'
def meta(request='r-profile', workload='work-a'):
 return dict(schema_version='v1',request_id=request,trace_id='trace-1',workload_id=workload,provider='yonsei-mock',origin='SYNTHETIC',execution_mode='LIVE_EXECUTION',created_at_unix_s=int(NOW.timestamp()),expires_at_unix_s=int(NOW.timestamp())+3600)
def profile_dict():
 return dict(meta=meta(),profile_id='profile-a',version=1,model='cuda-gemm-2048-fp32-v1',workload_type='benchmark',input_shape=[2048,2048],compute_dtype='fp32',input_dtype='fp32',batch=1,gpu_memory_value=1,gpu_memory_unit='GiB',gpu_memory_source='SYNTHETIC')
def plan_dict():
 return dict(meta={**meta('r-plan'),'provider':'skku-mock'},plan_id='plan-a',revision=1,profile_id='profile-a',profile_version=1,assignment_id='assignment-a',target=dict(node='devmaster',gpu_uuid='GPU-4a42bf67-df51-7582-9e81-b9dddd4bcea2',mig_uuid=UUID,resource_name=KEY,inventory_generation='generation-1'),space_percent=50,time_weight=3,policy_source='SYNTHETIC_TEST_POLICY',snapshot_ref='operator-inventory-1',execution_template='cuda-gemm-v1')
