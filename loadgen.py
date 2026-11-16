"""Открытая нагрузка из четырёх Pod: одинаковый план, независимые HTTP-соединения."""
import argparse
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

async def one(client, url, body, request_id, planned, run_id, client_index):
    await asyncio.sleep(max(0, planned - time.perf_counter()))
    sent = time.perf_counter()
    row = {'run_id': run_id, 'request_id': request_id, 'client': client_index,
           'planned_mono': planned, 'sent_mono': sent, 'sent_unix': time.time(),
           'send_lag_seconds': sent-planned, 'status': None, 'outcome': 'transport_error',
           'first_mono': None, 'end_mono': None, 'pod': None, 'model_version': None,
           'parts': 0, 'intervals_seconds': [], 'queue_seconds': None, 'cache_hit': None}
    done = False
    previous = None
    try:
        async with client.stream('POST', url+'/generate', json=body,
                                 headers={'X-Request-ID': request_id, 'Connection': 'close'}) as response:
            row['status'] = response.status_code
            row['pod'] = response.headers.get('x-pod')
            row['model_version'] = response.headers.get('x-model-version')
            if response.status_code != 200:
                await response.aread()
                row['outcome'] = 'rejected' if response.status_code == 429 else 'http_error'
            else:
                async for line in response.aiter_lines():
                    if not line.startswith('data: '):
                        continue
                    item = json.loads(line[6:])
                    if item.get('done'):
                        done = True
                        continue
                    now = time.perf_counter()
                    if previous is not None:
                        row['intervals_seconds'].append(now-previous)
                    else:
                        row['first_mono'] = now
                    previous = now
                    row['parts'] += 1
                    row.update(pod=item['pod'], model_version=item['model_version'],
                               queue_seconds=item['queue_seconds'], cache_hit=item['cache_hit'],
                               server_client_ip=item['client_ip'], backend_mode=item['backend_mode'])
                row['outcome'] = 'completed' if done else 'aborted'
    except httpx.TimeoutException as error:
        row['outcome'] = 'timeout'
        row['error'] = type(error).__name__
    except (httpx.HTTPError, OSError) as error:
        row['outcome'] = 'aborted' if row['parts'] else 'transport_error'
        row['error'] = type(error).__name__
    row['end_mono'] = time.perf_counter()
    row['end_unix'] = time.time()
    row['ttfc_seconds'] = None if row['first_mono'] is None else row['first_mono']-sent
    row['total_seconds'] = row['end_mono']-sent
    return row

async def run(config):
    start = time.perf_counter() + config['start_unix'] - time.time()
    index = config['client']
    duration, rate = config.get('duration', 120), config.get('rate', 5)
    count = round(duration*rate)
    plan = []
    jobs = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(90), limits=httpx.Limits(max_connections=128, max_keepalive_connections=0)) as client:
        for k in range(count):
            if config.get('traffic') == 'skew':
                owner = 0 if k%10<8 else 1+(k//10*2+k%10-8)%3
            else:
                owner = k%4
            if owner != index:
                continue
            body = {'messages': [{'role': 'user', 'content': 'Классифицируй: '+('пример '+str(k%7)+' ') * 8}],
                    'session_id': 'session-'+str(k%16), 'prefix': 'prefix-'+str(k%4),
                    'chunks': config.get('chunks', 4), 'interval_seconds': config.get('interval', .05)}
            identity = config['run_id']+'-'+str(k)
            planned = start+k/rate
            plan.append({'request_id': identity, 'offset_seconds': k/rate, 'client': index, 'body': body})
            jobs.append(asyncio.create_task(one(client, config['url'], body, identity, planned, config['run_id'], index)))
        rows = await asyncio.gather(*jobs)
    return {'config': config, 'planned': len(plan), 'rows': rows, 'schedule': plan,
            'finished_unix': time.time(), 'phase_seconds': duration}

STATE = {'running': False, 'result': None, 'error': None}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_GET(self):
        self.reply(STATE if self.path=='/result' else {'running': STATE['running'], 'error': STATE['error']})
    def do_POST(self):
        if STATE['running']:
            return self.reply({'error': 'already running'}, 409)
        config = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        STATE.update(running=True, result=None, error=None)
        def worker():
            try:
                STATE['result'] = asyncio.run(run(config))
            except Exception as error:
                STATE['error'] = repr(error)
            finally:
                STATE['running'] = False
        threading.Thread(target=worker, daemon=True).start()
        self.reply({'started': True})

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent', action='store_true')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.agent:
        ThreadingHTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
    else:
        print(json.dumps(asyncio.run(run(json.loads(args.config.read_text()))), ensure_ascii=False))
