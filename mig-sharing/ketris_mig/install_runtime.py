"""Never truncate a shared object currently mapped in running workloads."""
import argparse,os,shutil,tempfile
from pathlib import Path
def install_library(source,destination):
    source=Path(source);destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_file() and source.read_bytes()==destination.read_bytes():return False
    fd,tmp=tempfile.mkstemp(prefix='.library-',dir=destination.parent)
    try:
        with os.fdopen(fd,'wb') as out,source.open('rb') as src:
            shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
        os.chmod(tmp,0o644);os.replace(tmp,destination)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return True
def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('destination');a=p.parse_args();install_library(a.source,a.destination)
if __name__=='__main__':main()
