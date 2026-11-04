"""Real managed startup/retrieval verification with provider construction and remote traffic forbidden."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

ROOT=Path.cwd().resolve()
STORE=Path(os.environ.get('FINFILINGS_VERIFY_STORE', str(ROOT/'artifacts'))).resolve()
sys.path.insert(0,str(ROOT/'py'))
denied=[]

def audit(event,args):
    if event=='socket.connect':
        address=args[1]
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1'):
            denied.append(event);raise RuntimeError('remote/model traffic forbidden')
    elif event=='socket.getaddrinfo':
        if args[0] not in ('127.0.0.1','::1','localhost'):
            denied.append(event);raise RuntimeError('remote/model resolution forbidden')
    elif event=='subprocess.Popen':
        executable=Path(os.fsdecode(args[0]))
        command=args[1]
        allowed=(executable.name=='go' and command[1]=='build') or (str(executable)==sys.executable and command[1:3]==['-u','py/service.py'])
        allowed=allowed or (executable.name=='bridge' and executable.is_relative_to(STORE/'runs'))
        if not allowed:
            denied.append('subprocess:'+executable.name);raise RuntimeError('unexpected subprocess forbidden')
sys.addaudithook(audit)

def forbidden(*args,**kwargs):
    denied.append('provider/runtime construction');raise RuntimeError('generation/runtime construction forbidden')

spec=importlib.util.spec_from_file_location('managed_start',ROOT/'scripts/start.py')
start=importlib.util.module_from_spec(spec);spec.loader.exec_module(start)
mode=sys.argv[1] if len(sys.argv)>1 else '--verify-retrieval'
sys.argv=['start.py','--release-store',str(STORE),mode,'--startup-timeout','240']

def launch_in_guarded_process(command,cwd,env):
    assert command[0]==sys.executable and Path(command[1])==ROOT/'scripts/serve-audit-ui.py'
    sys.argv=command[1:]
    os.environ.update(env)
    try:
        runpy.run_path(command[1],run_name='__main__')
    except SystemExit as exc:
        return exc.code or 0
    return 0

with patch('agent.provider.ClaudeJSON',side_effect=forbidden), \
     patch('audit_web.runtime.build_runtime',side_effect=forbidden),patch('subprocess.call',side_effect=launch_in_guarded_process):
    status=start.main()
print(json.dumps(dict(guard='passed' if not denied else 'failed',denied_operations=denied,generation_calls=0,exit_code=status)))
raise SystemExit(status)
