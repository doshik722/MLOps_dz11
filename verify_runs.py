"""Проверка полноты повторов и идентичности планов сравниваемых вариантов."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def verify():
    expected=[f'b-{policy}-{traffic}-{repeat}' for policy in ('api-sticky','api')
              for traffic in ('uniform','skew') for repeat in range(1,4)]
    expected += [f'b-fixed-none-skew-{repeat}' for repeat in range(1,4)]
    output={};hashes={}
    for name in expected:
        agents=json.loads((ROOT/'results/runs'/f'{name}.json').read_text(encoding='utf-8'))
        assert len(agents)==4
        plans=[];rows=[]
        for agent in agents:
            result=agent['result'];assert result['phase_seconds']>=120
            assert result['config']['duration']==120 and result['config']['rate']==5
            plans+=result['schedule'];rows+=result['rows']
        assert len(plans)==len(rows)==600
        assert len({row['request_id'] for row in rows})==600
        canonical=[{key:item[key] for key in ('offset_seconds','client','body')} for item in sorted(plans,key=lambda x:x['offset_seconds'])]
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        traffic='uniform' if '-uniform-' in name else 'skew'
        hashes.setdefault(traffic,digest);assert hashes[traffic]==digest,(name,'Изменился план')
        ips={row.get('server_client_ip') for row in rows if row.get('server_client_ip')}
        output[name]={'planned':len(plans),'sent':len(rows),'schedule_sha256':digest,'observed_client_ips':sorted(ips),
                      'max_send_lag_seconds':max(row['send_lag_seconds'] for row in rows)}
    (ROOT/'results/repeat-verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Проверено {len(output)} прогонов по 120 с; планы каждого traffic совпадают.')
    return output

if __name__=='__main__':verify()
