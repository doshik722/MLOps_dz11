"""Пять собственных проверок живого Kubernetes-стенда и сырых результатов опытов."""
import hashlib
import json
import time
import unittest
from pathlib import Path
import sys

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import experiments as lab

class KubernetesChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not lab.KUBECONFIG.exists():
            raise RuntimeError('Нет kubeconfig стенда. Сначала выполнить stand.ps1 up и опыты A/B/C.')
        lab.get('deployment','api')

    @classmethod
    def tearDownClass(cls):
        for process,stream in lab.FORWARDS:
            process.terminate();stream.close()

    def connect(self,pod,port):
        lab.forward('pod/'+pod,port,8080)
        lab.wait_http(f'http://127.0.0.1:{port}/health',timeout=30)
        return f'http://127.0.0.1:{port}'

    def replace(self,pod):
        old={item['metadata']['uid'] for item in lab.get('pods','-l','app=api')['items']}
        lab.kub('delete','pod',pod,'--wait=false')
        until=time.monotonic()+80
        while time.monotonic()<until:
            candidates=[item for item in lab.get('pods','-l','app=api')['items']
                        if item['metadata']['uid'] not in old and item['status'].get('phase')=='Running']
            if candidates:return candidates[0]['metadata']['name']
            time.sleep(.5)
        self.fail('Контроллер не заменил Pod')

    def generate(self,url,marker,session='test'):
        body={'messages':[{'role':'system','content':'Сохранённая история'},
                          {'role':'user','content':marker}], 'session_id':session,'chunks':3}
        response=httpx.post(url+'/generate',json=body,timeout=30)
        self.assertEqual(response.status_code,200,response.text)
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertTrue(events[-1]['done'])
        return events

    def test_01_warmup_excluded_from_endpoints(self):
        pod=lab.ready_pods()[0]['metadata']['name']
        new=self.replace(pod)
        url=self.connect(new,18200)
        ready=httpx.get(url+'/ready',timeout=3)
        self.assertEqual(ready.status_code,503)
        endpoints=lab.get('endpointslices','-l','kubernetes.io/service-name=api')
        matching=[endpoint for item in endpoints['items'] for endpoint in item['endpoints']
                  if endpoint.get('targetRef',{}).get('name')==new]
        self.assertTrue(all(not endpoint.get('conditions',{}).get('ready',False) for endpoint in matching))
        lab.wait_http(url+'/ready',timeout=70)
        self.assertEqual(httpx.get(url+'/ready').json()['backend_mode'],'simulated')
        lab.save('checks/warmup.json',{'pod':new,'before':ready.status_code,'after':200,'endpoints':endpoints})

    def test_02_actual_version_after_rollback(self):
        raw=json.loads((lab.RESULTS/'runs/a-bad-rollback.json').read_text(encoding='utf-8'))
        rows=[row for agent in raw for row in agent['result']['rows']]
        self.assertTrue(rows and all(row['model_version']=='A' for row in rows))
        pod=lab.ready_pods()[0]['metadata']['name'];url=self.connect(pod,18201)
        events=self.generate(url,'ROLLBACK-CONTROL')
        self.assertTrue(all(item['model_version']=='A' for item in events))
        lab.save('checks/rollback.json',events)

    def test_03_replacement_restores_version_and_dialog(self):
        pod=lab.ready_pods()[0]['metadata']['name'];url=self.connect(pod,18202)
        before=self.generate(url,'REPLACEMENT-MARKER')
        new=self.replace(pod);new_url=self.connect(new,18203)
        lab.wait_http(new_url+'/ready',timeout=70)
        after=self.generate(new_url,'REPLACEMENT-MARKER')
        self.assertNotEqual(before[0]['pod'],after[0]['pod'])
        for field in ('model_version','artifact_sha256','history_sha256'):
            self.assertEqual(before[0][field],after[0][field])
        self.assertIn('REPLACEMENT-MARKER',after[0]['content'])
        lab.save('checks/replacement.json',{'before':before,'after':after})

    def test_04_rejections_and_aborts_not_counted_as_success(self):
        forced=json.loads((lab.RESULTS/'a-forced-kill.json').read_text(encoding='utf-8'))['row']
        self.assertIn(forced['outcome'],('aborted','transport_error','timeout'))
        self.assertGreater(forced['parts'],0)
        raw=json.loads((lab.RESULTS/'runs/c-alerts.json').read_text(encoding='utf-8'))
        rows=[row for agent in raw for row in agent['result']['rows']]
        rejected=[row for row in rows if row['status']==429]
        self.assertTrue(rejected,'Нагрузка должна воспроизвести отказ очереди')
        self.assertTrue(all(row['outcome']=='rejected' and row['parts']==0 for row in rejected))
        successful=[row for row in [forced,*rows] if row['outcome']=='completed']
        self.assertNotIn(forced,successful)
        self.assertEqual(len(rows),sum(agent['result']['planned'] for agent in raw))
        self.assertEqual(len(rows),len({row['request_id'] for row in rows}))
        self.assertTrue(all(row['outcome'] in ('completed','rejected','aborted','timeout','http_error','transport_error') for row in rows))
        lab.save('checks/accounting.json',{'attempts':len(rows)+1,'rejected':len(rejected),'forced_outcome':forced['outcome']})

    def test_05_two_dialogs_are_isolated(self):
        pod=lab.ready_pods()[0]['metadata']['name'];url=self.connect(pod,18204)
        alpha=self.generate(url,'ALPHA-ONLY-7391','alpha')
        beta=self.generate(url,'BETA-ONLY-2846','beta')
        for own,foreign,events in [('ALPHA-ONLY-7391','BETA-ONLY-2846',alpha),('BETA-ONLY-2846','ALPHA-ONLY-7391',beta)]:
            text=''.join(item.get('content','') for item in events)
            self.assertIn(own,text);self.assertNotIn(foreign,text)
        self.assertNotEqual(alpha[0]['history_sha256'],beta[0]['history_sha256'])
        lab.save('checks/dialog-isolation.json',{'alpha':alpha,'beta':beta})
