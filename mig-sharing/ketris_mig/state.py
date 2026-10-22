"""Atomic node records, with distinct owners for allocation and binding files."""
import json
import os
from pathlib import Path
import tempfile
from .contracts import strict_json

def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(strict_json(value));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        d=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def read_json(path):
    return json.loads(Path(path).read_text())

