"""Экспорт дашборда и PromQL, используемых в измерениях."""
import json
from pathlib import Path

QUERIES = {
    'p95 TTFC, s': 'histogram_quantile(0.95, sum by (le,pod) (rate(api_ttfc_seconds_bucket[30s])))',
    'p95 full response, s': 'histogram_quantile(0.95, sum by (le,pod) (rate(api_duration_seconds_bucket[30s])))',
    'running / Pod': 'api_running',
    'waiting / Pod': 'api_waiting',
    'completed requests/s': 'sum by(pod) (rate(api_requests_total{outcome="completed"}[30s]))',
    'errors and rejections/s': 'sum by(pod,outcome) (rate(api_requests_total{outcome!="completed"}[30s]))',
    'CPU cores / Pod': 'sum by(pod) (rate(container_cpu_usage_seconds_total{namespace="hw11",container="api"}[30s]))',
    'RAM bytes / Pod': 'sum by(pod) (container_memory_working_set_bytes{namespace="hw11",container="api"})',
    'Ready replicas': 'sum(kube_pod_status_ready{namespace="hw11",condition="true",pod=~"api-.*"})',
    'p95 queue, s': 'histogram_quantile(0.95, sum by(le,pod) (rate(api_queue_seconds_bucket[30s])))',
}
if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    panels = []
    for index,(title,query) in enumerate(QUERIES.items()):
        panels.append({'id':index+1,'title':title,'type':'timeseries',
          'datasource':{'type':'prometheus','uid':'prometheus'},
          'gridPos':{'x':index%2*12,'y':index//2*8,'w':12,'h':8},
          'targets':[{'refId':'A','expr':query,'legendFormat':'{{pod}} {{outcome}}'}],
          'fieldConfig':{'defaults':{'unit':'s' if ', s' in title else 'short'},'overrides':[]}})
    dashboard={'uid':'hw11','title':'HW11 — CPU simulated backend','schemaVersion':41,
       'version':1,'refresh':'5s','time':{'from':'now-1h','to':'now'},'panels':panels,'tags':['hw11','simulated']}
    folder=root/'chart/dashboards';folder.mkdir(parents=True,exist_ok=True)
    (folder/'hw11.json').write_text(json.dumps(dashboard,ensure_ascii=False,indent=2),encoding='utf-8')
    folder=root/'monitoring';folder.mkdir(exist_ok=True)
    (folder/'queries.json').write_text(json.dumps(QUERIES,ensure_ascii=False,indent=2),encoding='utf-8')
