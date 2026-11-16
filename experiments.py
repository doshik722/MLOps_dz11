"""Реальные опыты в изолированном kind-кластере; каждый HTTP-исход сохраняется."""
import argparse
import asyncio
import hashlib
import gzip
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import httpx
from loadgen import one

ROOT=Path(__file__).resolve().parent
RESULTS=ROOT/'results'
KUBECONFIG=ROOT/'.kubeconfig'
KUBECTL=str(ROOT/'tools/kubectl.exe')
HELM=str(ROOT/'tools/windows-amd64/helm.exe')
FORWARDS=[]
LOG_LOCK=threading.Lock()

def save(name,data):
    path=RESULTS/name;path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(data,ensure_ascii=False,indent=2)
    if len(text.encode())>4*1024*1024 and name.endswith('-server-logs.json'):
        with gzip.open(str(path)+'.gz','wt',encoding='utf-8') as stream:stream.write(text)
    else:path.write_text(text,encoding='utf-8')

def command(args,timeout=60,check=True):
    before=time.time()
    result=subprocess.run(list(map(str,args)),cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=timeout)
    recorded_stdout=result.stdout
    if ('get' in args or 'logs' in args) and args[0]==KUBECTL:
        recorded_stdout={'sha256':hashlib.sha256(result.stdout.encode()).hexdigest(),
                         'bytes':len(result.stdout.encode()),'note':'Поля наблюдения сохранены в timeline/snapshot'}
    with LOG_LOCK:
        path=RESULTS/'commands.jsonl';index=1
        while path.exists() and path.stat().st_size>4*1024*1024:
            path=RESULTS/f'commands-{index}.jsonl';index+=1
        with path.open('a',encoding='utf-8') as out:
            out.write(json.dumps({'unix':before,'args':list(map(str,args)),'exit':result.returncode,
                                  'stdout':recorded_stdout,'stderr':result.stderr},ensure_ascii=False)+'\n')
    if check and result.returncode:
        raise RuntimeError(f'{args}: {result.stderr[-1500:]}')
    return result.stdout

def kub(*args,**kwargs):
    return command([KUBECTL,'--kubeconfig',KUBECONFIG,'-n','hw11',*args],**kwargs)

def get(resource,*args):
    return json.loads(kub('get',resource,*args,'-o','json'))

def helm(**values):
    args=[HELM,'--kubeconfig',str(KUBECONFIG),'upgrade','--install','hw11',str(ROOT/'chart'),
          '--namespace','hw11','--create-namespace','--history-max','8']
    args+=['--reset-values']
    for key,value in values.items():
        args+=['--set',key+'='+str(value).lower() if isinstance(value,bool) else key+'='+str(value)]
    command(args,timeout=90)

def rollout(timeout=180):
    kub('rollout','status','deployment/api','--timeout='+str(timeout)+'s',timeout=timeout+15)

def ready_pods():
    return [pod for pod in get('pods','-l','app=api')['items']
            if not pod['metadata'].get('deletionTimestamp')
            and any(item['type']=='Ready' and item['status']=='True' for item in pod['status'].get('conditions',[]))]

def forward(target,port,remote):
    log=(ROOT/'logs');log.mkdir(exist_ok=True)
    stream=(log/('forward-'+str(port)+'.log')).open('w',encoding='utf-8')
    proc=subprocess.Popen([KUBECTL,'--kubeconfig',str(KUBECONFIG),'-n','hw11','port-forward',target,f'{port}:{remote}'],cwd=ROOT,stdout=stream,stderr=stream)
    FORWARDS.append((proc,stream))
    return proc

def wait_http(url,timeout=40):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        try:
            if httpx.get(url,timeout=2).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.5)
    raise TimeoutError(url)

class Watch:
    def __init__(self,name):
        self.name=name;self.rows=[];self.stop=threading.Event()
    def __enter__(self):
        def loop():
            while not self.stop.is_set():
                try:
                    pods=get('pods','-l','app=api')
                    for pod in pods['items']:
                        pod['metadata']={key:pod['metadata'][key] for key in ('name','uid','creationTimestamp','deletionTimestamp') if key in pod['metadata']}
                        pod.pop('spec',None)
                        pod['status']={key:pod['status'][key] for key in ('phase','podIP','conditions','containerStatuses') if key in pod['status']}
                    row={'unix':time.time(),'pods':pods,
                      'endpoints':get('endpointslices','-l','kubernetes.io/service-name=api'),
                      'hpa':get('hpa'), 'metrics':json.loads(kub('get','--raw','/apis/metrics.k8s.io/v1beta1/namespaces/hw11/pods',check=False) or '{}')}
                    try:
                        row['prom_runtime']=httpx.get('http://127.0.0.1:19090/api/v1/query',
                            params={'query':'{__name__=~"api_running|api_waiting|api_ready",job="api"}'},timeout=4).json()
                    except httpx.HTTPError as error:row['prom_runtime_error']=str(error)
                    self.rows.append(row)
                except Exception as error:self.rows.append({'unix':time.time(),'error':repr(error)})
                self.stop.wait(1)
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start();return self
    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=40);save(self.name+'-timeline.json',self.rows)

def logs(name):
    records={}
    for pod in get('pods','-l','app=api')['items']:
        records[pod['metadata']['name']]=kub('logs',pod['metadata']['name'],check=False)
    save(name+'-server-logs.json',records)

def reset_cache():
    for pod in ready_pods():
        kub('exec',pod['metadata']['name'],'--','python','-c',
            "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/reset-cache',method='POST')).read().decode())")

def start_agents(run_id,traffic='uniform',policy='api',duration=120,rate=5,chunks=4,interval=.05):
    start=time.time()+4
    for index in range(4):
        config={'run_id':run_id,'client':index,'traffic':traffic,'url':f'http://{policy}:8080',
                'duration':duration,'rate':rate,'start_unix':start,'chunks':chunks,'interval':interval}
        response=httpx.post(f'http://127.0.0.1:{18100+index}/run',json=config,timeout=5)
        response.raise_for_status()
    return start

def collect_agents(run_id,timeout=180):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        status=[httpx.get(f'http://127.0.0.1:{18100+i}/status',timeout=5).json() for i in range(4)]
        if all(not x['running'] for x in status):break
        time.sleep(2)
    else:raise TimeoutError('Генераторы не закончили')
    results=[httpx.get(f'http://127.0.0.1:{18100+i}/result',timeout=10).json() for i in range(4)]
    assert all(x['error'] is None and x['result'] is not None for x in results)
    save('runs/'+run_id+'.json',results)
    rows=[row for agent in results for row in agent['result']['rows']]
    counts={outcome:sum(row['outcome']==outcome for row in rows) for outcome in set(row['outcome'] for row in rows)}
    print(run_id,counts,flush=True)
    return rows

def setup_forwards():
    for index in range(4):
        forward('pod/client-'+str(index),18100+index,8090)
    forward('service/prometheus',19090,9090)
    forward('service/grafana',13000,3000)
    for index in range(4):wait_http(f'http://127.0.0.1:{18100+index}/status')
    wait_http('http://127.0.0.1:19090/-/ready')

def run_balance():
    helm();rollout();reset_cache()
    for repeat in range(1,4):
        for traffic in ('uniform','skew'):
            for policy in ('api-sticky','api'):
                run_id=f'b-{policy}-{traffic}-{repeat}'
                if (RESULTS/'runs'/f'{run_id}.json').exists():continue
                reset_cache()
                print('Начало',run_id,'120 секунд',flush=True)
                with Watch(run_id):
                    start_agents(run_id,traffic,policy)
                    collect_agents(run_id)
    # Повтор после выбранного исправления: отказ от ClientIP при горячем клиенте.
    for repeat in range(1,4):
        run_id=f'b-fixed-none-skew-{repeat}'
        if (RESULTS/'runs'/f'{run_id}.json').exists():continue
        reset_cache()
        with Watch(run_id):
            start_agents(run_id,'skew','api')
            collect_agents(run_id)
    logs('b-final')

def pod_request(pod,path,payload=None):
    encoded=json.dumps(payload,ensure_ascii=True) if payload is not None else None
    script="import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080"+path+"',data="+(repr(encoded.encode()) if encoded else 'None')+",headers={'Content-Type':'application/json'})); print(r.read().decode())"
    return kub('exec',pod,'--','python','-c',script)

async def long_request(url,identity,body=None):
    async with httpx.AsyncClient(timeout=90) as client:
        return await one(client,url,body or {'messages':[{'role':'user','content':'long-control'}],
          'session_id':'long','chunks':40,'interval_seconds':1},identity,time.perf_counter(),'a-long',0)

def run_a(skip_warmup=False):
    # Дефект сохранён в values, код backend в сравниваемых случаях одинаков.
    if not skip_warmup:
        helm(brokenProbes=True);time.sleep(1)
        with Watch('a-broken'):
            start_agents('a-broken',duration=28,rate=2)
            broken=collect_agents('a-broken',timeout=80)
        logs('a-broken')
        assert any(row['status']==503 for row in broken), 'Исходный сбой не проявился'
        with Watch('a-fixed-warmup'):
            start_agents('a-fixed-warmup',duration=60,rate=2)
            helm(brokenProbes=False)
            rollout()
            fixed=collect_agents('a-fixed-warmup',timeout=120)
        logs('a-fixed-warmup')
        assert all(row['outcome']=='completed' for row in fixed)
    else:
        helm(modelVersion='A');rollout()
    # Длинный ответ начинается до изменения Deployment и пересекает rollout.
    old=ready_pods()[0]['metadata']['name']
    forward('pod/'+old,18091,8080);wait_http('http://127.0.0.1:18091/ready')
    long_holder={}
    worker=threading.Thread(target=lambda:long_holder.update(row=asyncio.run(long_request('http://127.0.0.1:18091','rollout-crossing'))))
    worker.start();time.sleep(2)
    with Watch('a-rollout'):
        start_agents('a-rollout',duration=70,rate=.5)
        changed_at=time.time();helm(modelVersion='B');rollout()
        rows=collect_agents('a-rollout',timeout=130)
        worker.join(timeout=90)
    assert not worker.is_alive()
    save('a-long-stream.json',{'rollout_started_unix':changed_at,'row':long_holder['row']})
    assert long_holder['row']['outcome']=='completed'
    assert long_holder['row']['sent_unix']<changed_at<long_holder['row']['end_unix']
    assert all(row['outcome']=='completed' for row in rows)
    logs('a-rollout')
    # Неудачная версия остаётся вне endpoints; A возвращается управляемым Helm rollback.
    helm(modelVersion='A');rollout()
    history=json.loads(command([HELM,'--kubeconfig',KUBECONFIG,'history','hw11','-n','hw11','-o','json']))
    good_revision=history[-1]['revision']
    with Watch('a-bad-rollback'):
        start_agents('a-bad-rollback',duration=70,rate=1)
        helm(modelVersion='B',failLoad=True)
        time.sleep(28)
        before_rollback=get('pods','-l','app=api')
        save('a-bad-pods.json',before_rollback)
        command([HELM,'--kubeconfig',KUBECONFIG,'rollback','hw11',str(good_revision),'-n','hw11'],timeout=90)
        rollout()
        rollback_rows=collect_agents('a-bad-rollback',timeout=120)
    assert all(row['outcome']=='completed' and row['model_version']=='A' for row in rollback_rows)
    logs('a-rollback')
    # SIGKILL конкретного backend, а не штатный rolling update.
    target=ready_pods()[0]['metadata']['name']
    forward('pod/'+target,18092,8080);wait_http('http://127.0.0.1:18092/ready')
    killed={}
    worker=threading.Thread(target=lambda:killed.update(row=asyncio.run(long_request('http://127.0.0.1:18092','forced-kill'))))
    worker.start();time.sleep(2)
    with Watch('a-force'):
        started=time.time()
        # SIGKILL посылается из родительского PID namespace узла: PID 1 внутри
        # собственного namespace защищён от неподдерживаемых сигналов изнутри.
        node=get('pod',target)['spec']['nodeName']
        assert node=='hw11-control-plane'
        containers=json.loads(command(['docker','exec',node,'crictl','ps','-o','json']))['containers']
        selected=[item for item in containers if item.get('labels',{}).get('io.kubernetes.pod.name')==target
                  and item.get('labels',{}).get('io.kubernetes.pod.namespace')=='hw11']
        assert len(selected)==1
        container_id=selected[0]['id']
        inspected=json.loads(command(['docker','exec',node,'crictl','inspect',container_id]))
        pid=inspected['info']['pid'];assert int(pid)>1
        cmdline=command(['docker','exec',node,'cat',f'/proc/{pid}/cmdline'])
        assert 'uvicorn' in cmdline and 'app:app' in cmdline
        command(['docker','exec',node,'kill','-9',str(pid)])
        kub('wait','pod',target,'--for=condition=Ready=false','--timeout=30s',timeout=40)
        worker.join(timeout=95)
        kub('wait','pod',target,'--for=condition=Ready','--timeout=90s',timeout=100)
        rollout()
        time.sleep(3)
        recovery=pod_request(target,'/ready')
    save('a-forced-kill.json',{'kill_unix':started,'container_id':container_id,'node_pid':pid,
                             'row':killed.get('row'),'recovered':json.loads(recovery)})
    assert killed['row']['outcome'] in ('aborted','transport_error','timeout')

def prom(query):
    response=httpx.get('http://127.0.0.1:19090/api/v1/query',params={'query':query},timeout=10)
    response.raise_for_status();return response.json()

def run_c(only_hpa=False):
    helm(**{'hpa.enabled':True});rollout();time.sleep(25)
    for _ in range(90):
        hpa=get('hpa','api').get('status',{})
        if len(ready_pods())==2 and hpa.get('currentReplicas')==2 and hpa.get('desiredReplicas')==2:break
        time.sleep(2)
    else:raise AssertionError('Перед нагрузкой нужны 2 Ready Pod и сошедшийся HPA')
    reset_cache()
    save('c-hpa-config.json',get('hpa','api'))
    baseline_uids=[pod['metadata']['uid'] for pod in ready_pods()]
    rows=[]
    with Watch('c-hpa'):
        start=start_agents('c-hpa-load',duration=120,rate=12)
        seen_three=False
        for _ in range(65):
            data=get('hpa','api');rows.append({'unix':time.time(),'hpa':data})
            seen_three |= data.get('status',{}).get('currentReplicas',0)==3
            time.sleep(2)
        load_rows=collect_agents('c-hpa-load',timeout=60)
        down_started=max(row['sent_unix'] for row in load_rows)
        last_completed=max(row['end_unix'] for row in load_rows)
        for _ in range(120):
            data=get('hpa','api');rows.append({'unix':time.time(),'hpa':data})
            if data.get('status',{}).get('currentReplicas')==2 and data['status'].get('desiredReplicas')==2:break
            time.sleep(2)
        else:raise AssertionError('HPA не вернулся к 2 репликам')
    save('c-hpa.json',{'load_started':start,'load_stopped':down_started,'down_seconds':time.time()-down_started,
                      'load_planned_stop':start+120,'last_response_unix':last_completed,
                      'baseline_uids':baseline_uids,'down_observed_unix':time.time(),
                      'seen_three':seen_three,'observations':rows})
    assert seen_three
    if only_hpa:return
    # Повтор горячего клиента при включённом HPA и ClientIP.
    with Watch('c-hot'):
        start_agents('c-hot-sticky','skew','api-sticky',duration=120,rate=12)
        collect_agents('c-hot-sticky',timeout=200)
    # Управляемое нарушение обоих порогов; сохраняются все состояния алертов.
    alerts=[]
    for _ in range(45):
        item={'unix':time.time(),'rules':httpx.get('http://127.0.0.1:19090/api/v1/rules',timeout=10).json()}
        alerts.append(item)
        states=[rule['state'] for group in item['rules']['data']['groups'] for rule in group['rules'] if rule.get('type')=='alerting']
        if states and all(state=='inactive' for state in states):break
        time.sleep(2)
    start_agents('c-alerts','skew','api-sticky',duration=90,rate=30)
    for _ in range(50):
        alerts.append({'unix':time.time(),'rules':httpx.get('http://127.0.0.1:19090/api/v1/rules',timeout=10).json()})
        time.sleep(2)
    collect_agents('c-alerts',timeout=60)
    for _ in range(50):
        item={'unix':time.time(),'rules':httpx.get('http://127.0.0.1:19090/api/v1/rules',timeout=10).json()}
        alerts.append(item)
        states=[rule['state'] for group in item['rules']['data']['groups'] for rule in group['rules'] if rule.get('type')=='alerting']
        if states and all(state=='inactive' for state in states):break
        time.sleep(2)
    else:raise AssertionError('Алерты не вернулись в норму после снятия нагрузки')
    save('c-alerts.json',alerts)
    for name in ('HighTTFC','HighRejectionRatio'):
        states={rule['state'] for item in alerts for group in item['rules']['data']['groups'] for rule in group['rules'] if rule.get('name')==name}
        assert {'pending','firing','inactive'}<=states,(name,states)
    logs('c-final')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['a','balance','c'])
    parser.add_argument('--skip-warmup',action='store_true')
    parser.add_argument('--hpa-only',action='store_true')
    args=parser.parse_args()
    RESULTS.mkdir(exist_ok=True)
    try:
        setup_forwards()
        if args.phase=='a':run_a(args.skip_warmup)
        elif args.phase=='balance':run_balance()
        elif args.phase=='c':run_c(args.hpa_only)
    finally:
        for process,stream in FORWARDS:
            process.terminate();stream.close()

if __name__=='__main__':main()
