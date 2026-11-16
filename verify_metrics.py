"""Сравнение client p95 и classic Histogram на одном ограниченном наборе ответов."""
import time
import httpx
import experiments as lab
from report import quantile

def query(expr,at=None):
    params={'query':expr}
    if at is not None:params['time']=at
    result=httpx.get('http://127.0.0.1:19090/api/v1/query',params=params,timeout=10)
    result.raise_for_status();body=result.json();assert body['status']=='success',body
    return body

def main():
    try:
        lab.setup_forwards()
        lab.helm(**{'hpa.enabled':False});lab.rollout()
        for _ in range(40):
            pods=lab.get('pods','-l','app=api')['items']
            if len(pods)==2 and len(lab.ready_pods())==2:break
            time.sleep(1)
        else:raise AssertionError('Перед сравнением должны остаться ровно 2 Ready Pod без HPA')
        time.sleep(5)
        identity=[p['metadata']['uid'] for p in lab.ready_pods()]
        lab.reset_cache();time.sleep(4)
        before=time.time();buckets_before=query('api_ttfc_seconds_bucket',before)
        lab.start_agents('metric-comparison',duration=8,rate=2)
        rows=lab.collect_agents('metric-comparison',timeout=60)
        assert len(rows)==16 and all(row['outcome']=='completed' for row in rows)
        time.sleep(5);after=time.time()
        buckets_after=query('api_ttfc_seconds_bucket',after)
        expr=f'histogram_quantile(0.95, sum by(le) (api_ttfc_seconds_bucket - api_ttfc_seconds_bucket @ {before:.3f}))'
        value=query(expr,after)
        counts=query(f'sum(api_ttfc_seconds_count - api_ttfc_seconds_count @ {before:.3f})',after)
        assert int(float(counts['data']['result'][0]['value'][1]))==16
        assert sorted(identity)==sorted(p['metadata']['uid'] for p in lab.ready_pods())
        comparison={'before_unix':before,'after_unix':after,'successful_n':16,'promql':expr,
          'client_p95_seconds':quantile([r['ttfc_seconds'] for r in rows],.95),
          'histogram_p95_seconds':float(value['data']['result'][0]['value'][1]),
          'histogram_count_delta':counts,'buckets_before':buckets_before,'buckets_after':buckets_after,
          'note':'Серверный Histogram и клиент имеют разные границы измерения; квантиль Histogram интерполируется внутри bucket.'}
        lab.save('prometheus-comparison.json',comparison)
        print(comparison['client_p95_seconds'],comparison['histogram_p95_seconds'])
    finally:
        for process,stream in lab.FORWARDS:process.terminate();stream.close()

if __name__=='__main__':main()
