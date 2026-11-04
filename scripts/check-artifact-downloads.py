#!/usr/bin/env python3
"""Run download integration fixtures with network restricted to each fixture's own loopback port."""
import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'py'),str(ROOT/'setup')]
import test_artifact_downloads as tests


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();denied=[]
    def audit(event,values):
        allowed=True
        if event=='socket.bind':allowed=values[1]==('127.0.0.1',0)
        elif event=='socket.connect':allowed=isinstance(values[1],tuple) and values[1][0]=='127.0.0.1' and values[1][1] in tests.ALLOWED_PORTS
        elif event=='socket.getaddrinfo':allowed=values[0]=='127.0.0.1' and int(values[1]) in tests.ALLOWED_PORTS
        elif event=='subprocess.Popen':allowed=False
        if not allowed:
            denied.append(event);raise RuntimeError('only registered loopback test fixtures are allowed')
    sys.addaudithook(audit)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(tests))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(dict(tests=result.testsRun,success=result.wasSuccessful() and not denied,
        failures=len(result.failures),errors=len(result.errors),denied_operations=denied,
        generation_calls=0,max_http_workers_per_fixture=1,fixture_ports_closed=not tests.ALLOWED_PORTS),indent=2)+'\n')
    return 0 if result.wasSuccessful() and not denied else 1


if __name__=='__main__':sys.exit(main())
