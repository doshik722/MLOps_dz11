"""Фактические версии, место и размер выделенного стенда; Docker опрашивается с тайм-аутом."""
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent

def run(args,timeout=20):
    try:
        result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
        return {'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
    except subprocess.TimeoutExpired:
        return {'exit_code':None,'error':f'timeout {timeout}s'}

def main():
    results=ROOT/'results';results.mkdir(exist_ok=True)
    info={'recorded_unix':time.time(),'platform':platform.platform(),'python':platform.python_version(),
          'logical_cpu':os.cpu_count(),'disk':{drive:shutil.disk_usage(drive)._asdict() for drive in ('C:/','D:/')},
          'limits':{'wsl_memory_gb':6,'wsl_processors':4,'wsl_swap_mb':512,
                    'api_memory_limit_mib':192,'api_cpu_limit_cores':.6,'prometheus_retention_hours':2,
                    'prometheus_retention_mb':128,'prometheus_volume_limit_mib':256,'grafana_volume_limit_mib':128}}
    for name,args in {'docker':['docker','info','--format','{{json .}}'],
      'kind':[str(ROOT/'tools/kind.exe'),'version'],
      'helm':[str(ROOT/'tools/windows-amd64/helm.exe'),'version','--short'],
      'kubectl':[str(ROOT/'tools/kubectl.exe'),'version','--client','-o','json']}.items():
        info[name]=run(args)
    if info['docker'].get('exit_code')==0:
        info['images']=run(['docker','images','--format','{{json .}}'])
        info['docker_disk']=run(['docker','system','df'])
        info['kind_container']=run(['docker','inspect','hw11-control-plane'])
        info['kubernetes']=run([str(ROOT/'tools/kubectl.exe'),'--kubeconfig',str(ROOT/'.kubeconfig'),'version','-o','json'])
    (results/'environment.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    files=['app.py','legacy/service.py','legacy/release.py','pyproject.toml','uv.lock','monitoring/metrics-server.yaml']
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files if (ROOT/name).exists()}
    (results/'source-hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    print(json.dumps({'docker_ready':info['docker'].get('exit_code')==0,'saved':'results/environment.json'},ensure_ascii=False))
    if '--require-docker' in sys.argv and info['docker'].get('exit_code')!=0:
        raise SystemExit('Linux Engine Docker не готов. Kubernetes-опыты не запускались.')

if __name__=='__main__':main()
