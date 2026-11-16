"""Снимки реально применённой конфигурации, дашборда, метрик и размеров стенда."""
import json
import time
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path

import httpx
import experiments as lab

def analyze_runtime():
    results={}
    for path in sorted((lab.RESULTS/'runs').glob('b-*.json')):
        name=path.stem
        agents=json.loads(path.read_text(encoding='utf-8'))
        rows=[row for agent in agents for row in agent['result']['rows']]
        trace=defaultdict(Counter)
        for row in rows:
            if row.get('server_client_ip'):trace[row['server_client_ip']][row['pod']]+=1
        timeline_path=lab.RESULTS/(name+'-timeline.json')
        timeline=json.loads(timeline_path.read_text(encoding='utf-8')) if timeline_path.exists() else []
        historical_path=lab.RESULTS/(name+'-prom-runtime.json')
        if historical_path.exists():
            historical=json.loads(historical_path.read_text(encoding='utf-8'))
            by_time=defaultdict(list)
            for series in historical['response']['data']['result']:
                for timestamp,value in series['values']:
                    by_time[timestamp].append({'metric':series['metric'],'value':[timestamp,value]})
            timeline=[{'unix':timestamp,'pods':next((point['pods'] for point in reversed(timeline) if point['unix']<=timestamp and 'pods' in point),{}),
                       'prom_runtime':{'data':{'result':metrics}}} for timestamp,metrics in sorted(by_time.items())]
        samples=[];observed=0
        for point in timeline:
            ready={pod['metadata']['name'] for pod in point.get('pods',{}).get('items',[])
                   if not pod['metadata'].get('deletionTimestamp') and any(c['type']=='Ready' and c['status']=='True' for c in pod['status'].get('conditions',[]))}
            metrics=point.get('prom_runtime',{}).get('data',{}).get('result',[])
            gauges=defaultdict(dict)
            for metric in metrics:
                gauges[metric['metric'].get('pod')][metric['metric']['__name__']]=float(metric['value'][1])
            if not ready or not all(all(key in gauges[pod] for key in ('api_running','api_waiting','api_ready')) for pod in ready):continue
            observed+=1
            hot=[pod for pod in ready if gauges[pod].get('api_waiting',0)>0]
            idle=[pod for pod in ready if gauges[pod].get('api_running',0)==0 and gauges[pod].get('api_waiting',0)==0]
            if hot and idle:samples.append({'unix':point['unix'],'hot':hot,'idle':idle,'gauges':dict(gauges)})
        results[name]={'client_ip_to_pod':{ip:dict(pods) for ip,pods in trace.items()},
                       'runtime_samples':observed,'hot_and_idle_samples':len(samples),'examples':samples[:5]}
    lab.save('routing-analysis.json',results)
    return results

def fetch_runtime():
    selector='{__name__=~"api_running|api_waiting|api_ready",job="api"}'
    for path in sorted(lab.RESULTS.glob('b-*-timeline.json')):
        timeline=json.loads(path.read_text(encoding='utf-8'))
        if not timeline:continue
        params={'query':selector,'start':timeline[0]['unix'],'end':timeline[-1]['unix'],'step':2}
        response=httpx.get('http://127.0.0.1:19090/api/v1/query_range',params=params,timeout=20)
        response.raise_for_status();body=response.json();assert body['status']=='success'
        lab.save(path.name.replace('-timeline.json','-prom-runtime.json'),{'request':params,'response':body})

def main():
    try:
        lab.setup_forwards()
        objects={resource:lab.get(resource) for resource in ('deployments','services','configmaps','hpa')}
        lab.save('applied-objects.json',objects)
        lab.save('client-pods.json',lab.get('pods','-l','app=client'))
        lab.save('nodes.json',lab.get('nodes'))
        proxy=lab.kub('get','--raw','/api/v1/namespaces/kube-system/configmaps/kube-proxy')
        lab.save('kube-proxy-config.json',json.loads(proxy))
        for name,url in [('grafana-export.json','http://127.0.0.1:13000/api/dashboards/uid/hw11'),
                         ('prometheus-targets.json','http://127.0.0.1:19090/api/v1/targets'),
                         ('prometheus-rules.json','http://127.0.0.1:19090/api/v1/rules')]:
            response=httpx.get(url,timeout=10);response.raise_for_status();lab.save(name,response.json())
        images=lab.command(['docker','image','inspect','hw11-api:0.1.0','kindest/node:v1.34.0'])
        lab.save('images.json',json.loads(images))
        cri_images=lab.command(['docker','exec','hw11-control-plane','crictl','images','-o','json'])
        lab.save('images-lock.json',json.loads(cri_images))
        size=lab.command(['docker','inspect','--size','hw11-control-plane'])
        lab.save('kind-size.json',json.loads(size))
        disk=lab.command(['docker','system','df','-v']);(lab.RESULTS/'docker-disk-after.txt').write_text(disk,encoding='utf-8')
        pod=lab.ready_pods()[0]['metadata']['name']
        loaded=lab.kub('exec',pod,'--','python','-c',"import hashlib,json; from pathlib import Path; print(json.dumps({name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ['app.py','loadgen.py']}))")
        lab.save('container-source-hashes.json',{'pod':pod,'files':json.loads(loaded)})
        fetch_runtime();analyze_runtime()
        hpa=json.loads((lab.RESULTS/'c-hpa.json').read_text(encoding='utf-8'))
        timeline=json.loads((lab.RESULTS/'c-hpa-timeline.json').read_text(encoding='utf-8'))
        created={};ready={}
        for point in timeline:
            for pod in point.get('pods',{}).get('items',[]):
                uid=pod['metadata']['uid']
                if uid in hpa['baseline_uids']:continue
                created[uid]={'pod':pod['metadata']['name'],'created_unix':datetime.fromisoformat(pod['metadata']['creationTimestamp'].replace('Z','+00:00')).timestamp()}
                for condition in pod['status'].get('conditions',[]):
                    if condition['type']=='Ready' and condition['status']=='True':
                        ready.setdefault(uid,datetime.fromisoformat(condition['lastTransitionTime'].replace('Z','+00:00')).timestamp())
        lab.save('c-hpa-timing.json',[{**item,'created_after_load_seconds':item['created_unix']-hpa['load_started'],
            'ready_after_load_seconds':ready[uid]-hpa['load_started'],
            'creation_to_ready_seconds':ready[uid]-item['created_unix']} for uid,item in created.items() if uid in ready])
    finally:
        for process,stream in lab.FORWARDS:process.terminate();stream.close()

if __name__=='__main__':main()
