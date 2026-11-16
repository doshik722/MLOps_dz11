"""Последовательный запуск стенда, опытов, проверок и отчёта с сохранением кодов возврата."""
import json
import argparse
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent

def run(args,name):
    print('\n'+name,flush=True)
    folder=ROOT/'logs';folder.mkdir(exist_ok=True)
    with (folder/(name+'.log')).open('w',encoding='utf-8') as stream:
        process=subprocess.Popen(args,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        for line in process.stdout:
            print(line,end='',flush=True);stream.write(line);stream.flush()
        code=process.wait()
    with (ROOT/'results/pipeline.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'unix':time.time(),'stage':name,'exit_code':code,'args':args})+'\n')
    if name=='kubernetes-checks':
        (ROOT/'results/check-status.json').write_text(json.dumps({'exit_code':code,'log':'logs/kubernetes-checks.log'}),encoding='utf-8')
    if code:raise RuntimeError(f'{name}: exit {code}; см. logs/{name}.log')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--from-phase',choices=['a','balance','c','verification'])
    args=parser.parse_args()
    try:
        if args.from_phase is None:
            run(['pwsh','-NoProfile','-File','stand.ps1','build'],'build')
            run(['pwsh','-NoProfile','-File','stand.ps1','up'],'up')
        # Наблюдатели и четыре клиента должны быть готовы до воспроизведения дефекта.
        import experiments as lab
        lab.kub('wait','pod','-l','app=client','--for=condition=Ready','--timeout=180s',timeout=190)
        for name in ('prometheus','grafana','state'):
            lab.kub('rollout','status','deployment/'+name,'--timeout=360s',timeout=370)
        phases=['a','balance','c']
        if args.from_phase=='verification':phases=[]
        elif args.from_phase:phases=phases[phases.index(args.from_phase):]
        for phase in phases:run([sys.executable,'-u','experiments.py',phase],phase)
        run([sys.executable,'verify_runs.py'],'repeat-verification')
        run([sys.executable,'-u','verify_metrics.py'],'histogram-comparison')
        run([sys.executable,'-m','unittest','discover','-s','tests','-v'],'kubernetes-checks')
        run([sys.executable,'environment.py'],'environment')
        run([sys.executable,'report.py'],'report')
    except Exception:
        subprocess.run([sys.executable,'report.py'],cwd=ROOT)
        raise

if __name__=='__main__':main()
