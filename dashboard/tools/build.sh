#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$TASK_ROOT"
TAG=${1:-20261014-gpu-state-v3}
CONTEXT="$TASK_ROOT/.build/dashboard-image"
mkdir -p "$CONTEXT/backend" "$CONTEXT/frontend" "$CONTEXT/archive-bundles"
npm --prefix dashboard/frontend run build
cp -r dashboard/backend/ketris_dashboard "$CONTEXT/backend/"
cp dashboard/backend/pyproject.toml "$CONTEXT/backend/"
cp -r dashboard/frontend/dist/. "$CONTEXT/frontend/"
"$TASK_ROOT/.venv/bin/python" - "$CONTEXT" <<'PY'
import hashlib,json,subprocess,sys
from pathlib import Path
from ketris_dashboard.archive import parse_experiment,DEFAULT_ROOTS
context=Path(sys.argv[1]);roots=DEFAULT_ROOTS+[Path('reports/ketris_mig_runtime_20261013_full').resolve()]
for root in roots:
 if not (root/'summary.json').is_file():continue
 b=parse_experiment(root,allowed_roots=roots)
 (context/'archive-bundles'/(b.experiment_id+'.json')).write_text(json.dumps(b.to_dict(),ensure_ascii=False,allow_nan=False))
record={'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'source_dirty':bool(subprocess.check_output(['git','status','--porcelain'],text=True)),'bundles':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (context/'archive-bundles').glob('*.json')}}
(context/'build-record.json').write_text(json.dumps(record,indent=2))
PY
docker build -f dashboard/Dockerfile -t "ketris-dashboard:$TAG" "$CONTEXT"
docker save "ketris-dashboard:$TAG" | ctr -n k8s.io images import -
docker image inspect "ketris-dashboard:$TAG" --format '{{.Id}}' > "$CONTEXT/image-id.txt"
