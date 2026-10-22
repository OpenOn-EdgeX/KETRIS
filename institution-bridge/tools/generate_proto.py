from pathlib import Path
from grpc_tools import protoc
root=Path(__file__).resolve().parents[1]
out=root/'src/ketris_integration/rpc'
out.mkdir(parents=True,exist_ok=True)
(out/'__init__.py').touch()
if protoc.main(['grpc_tools.protoc',f'-I{root / "proto"}',f'--python_out={out}',f'--grpc_python_out={out}',str(root/'proto/institution_bridge.proto')]):
 raise SystemExit('protoc failed')
p=out/'institution_bridge_pb2_grpc.py'
p.write_text(p.read_text().replace('import institution_bridge_pb2 as','from . import institution_bridge_pb2 as'))
