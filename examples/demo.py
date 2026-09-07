#!/usr/bin/env python3
"""Run an entirely offline wait/collector demonstration in a temporary DB."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

# Also works from an unpacked source tree, without installing anything.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from boardmail.providers import collect_all
from boardmail.store import Store
from examples.fixtures import FixtureClient, settings


def run_cli(db, *args, wait=False):
    command = [sys.executable,"-m","boardmail","--db",str(db),*args]
    if wait:
        return subprocess.Popen(command,stdout=subprocess.PIPE,text=True,cwd=Path(__file__).resolve().parents[1])
    result = subprocess.run(command,capture_output=True,text=True,cwd=Path(__file__).resolve().parents[1],check=False)
    return result.returncode,json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect",type=Path)
    parser.add_argument("--outage",action="store_true")
    args = parser.parse_args()
    if args.collect:
        def factory(source,config):
            client = FixtureClient(source,config)
            client.fail = args.outage and source=="the-colony"
            return client
        print(json.dumps(collect_all(Store(args.collect),settings(),client_factory=factory)))
        return
    with tempfile.TemporaryDirectory(prefix="boardmail-demo-") as directory:
        db = Path(directory)/"mail.sqlite3"
        assert run_cli(db,"init")[0]==0
        waiter = run_cli(db,"wait","--after","0","--timeout","10",wait=True)
        try:
            time.sleep(0.2)
            collected = subprocess.run([sys.executable,__file__,"--collect",str(db)],capture_output=True,text=True,check=True)
            assert json.loads(collected.stdout)["added"]>0
            result = json.loads(waiter.communicate(timeout=12)[0])
            assert waiter.returncode==0 and result["messages"]
            after = str(result['next_after'])
            print(json.dumps({"step":"arrival","event":result['event'],"messages":len(result['messages']),"next_after":int(after)}))
            code,result = run_cli(db,"wait","--after",after,"--timeout","0")
            assert code==3 and result['event']=='timeout'
            print(json.dumps({"step":"no new arrival","event":result['event'],"next_after":result['next_after']}))
            subprocess.run([sys.executable,__file__,"--collect",str(db),"--outage"],capture_output=True,check=True)
            code,result = run_cli(db,"wait","--after",after,"--timeout","0")
            assert code==3 and any(s['status']=='error' for s in result['sources'])
            print(json.dumps({"step":"outage","event":result['event'],"sources":[
                {k:s[k] for k in ('source','status','last_ok','error')} for s in result['sources']]}))
        finally:
            if waiter.poll() is None:
                waiter.terminate()
                waiter.communicate(timeout=5)


if __name__=="__main__":
    main()
