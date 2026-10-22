#!/usr/bin/env python3
import argparse,json,subprocess
from ketris_mig.admission import mig_admission_manifest
def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
    legacy=json.loads(subprocess.check_output(['kubectl','get','mutatingwebhookconfiguration','ketris-env-webhook','-o','json'],text=True))
    args=['kubectl','apply','-f','-']+([] if a.apply else ['--dry-run=server'])
    subprocess.run(args,input=json.dumps(mig_admission_manifest(legacy)),text=True,check=True)
if __name__=='__main__':main()
