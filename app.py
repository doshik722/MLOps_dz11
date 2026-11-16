"""Продолжение API ДЗ 8–10: проверка артефакта, прогрев и потоковый CPU-имитатор."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

POD = os.getenv('POD_NAME', 'local')
BUCKETS = (.025, .05, .1, .2, .4, .8, 1.6, 3.2, 6.4, 12.8, 25.6, 51.2)
FIRST = Histogram('api_ttfc_seconds', 'Время до первой содержательной части', buckets=BUCKETS)
TOTAL = Histogram('api_duration_seconds', 'Полное время успешного ответа', buckets=BUCKETS)
QUEUE = Histogram('api_queue_seconds', 'Ожидание допуска к backend', buckets=BUCKETS)
RUNNING = Gauge('api_running', 'Вычисляющиеся запросы')
WAITING = Gauge('api_waiting', 'Запросы в очереди')
READY = Gauge('api_ready', 'Загрузка и прогрев завершены')
OUTCOME = Counter('api_requests', 'Исход запроса', ['outcome'])
CACHE = Counter('api_prefix_cache', 'Локальный кеш имитатора', ['result'])
for name in ('completed', 'rejected', 'aborted', 'cold', 'invalid'):
    OUTCOME.labels(name)

def log(kind, **fields):
    print(json.dumps({'kind': kind, 'unix': time.time(), 'pod': POD,
                      'backend_mode': 'simulated', **fields}, ensure_ascii=False), flush=True)

def cpu_work(seconds):
    """Вычислительная стоимость в CPU-секундах потока, а не sleep."""
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        hashlib.pbkdf2_hmac('sha256', b'hw11', b'cpu-simulator', 2000)

class Message(BaseModel):
    role: Literal['system', 'user', 'assistant']
    content: Annotated[str, StringConstraints(min_length=1, max_length=4096)]

class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    messages: Annotated[list[Message], Field(min_length=1, max_length=20)]
    session_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    chunks: Annotated[int, Field(strict=True, ge=1, le=64)] = 4
    interval_seconds: Annotated[float, Field(ge=0, le=2)] = .05
    prefix: Annotated[str, StringConstraints(max_length=128)] = 'shared'

class State:
    def __init__(self):
        self.ready = False
        self.draining = False
        self.error = None
        self.version = None
        self.digest = None
        self.running = 0
        self.waiting = 0
        self.capacity = int(os.getenv('MAX_RUNNING', '1'))
        self.queue_limit = int(os.getenv('MAX_WAITING', '8'))
        self.slots = asyncio.Semaphore(self.capacity)
        self.cache = OrderedDict()
        self.calls = 0

    async def load(self):
        try:
            data = Path(os.getenv('ARTIFACT_PATH', '/artifact/model.json')).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != os.environ['ARTIFACT_SHA256']:
                raise ValueError('SHA256 артефакта не совпал')
            artifact = json.loads(data)
            if artifact['version'] != os.environ['MODEL_VERSION']:
                raise ValueError('Версия файла отличается от требуемой')
            self.version, self.digest = artifact['version'], digest
            log('loaded', model_version=self.version, artifact_sha256=digest)
            await asyncio.sleep(float(os.getenv('WARMUP_SECONDS', '20')))
            await asyncio.to_thread(cpu_work, .05)
            if os.getenv('FAIL_LOAD', 'false') == 'true':
                raise ValueError('Управляемый отказ прогрева')
            self.ready = True
            READY.set(1)
            log('warmup_finished', model_version=self.version)
        except Exception as error:
            self.error = str(error)
            log('load_failed', error=self.error)

state = State()

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(state.load())
    yield
    task.cancel()
    state.ready = False
    READY.set(0)

app = FastAPI(title='ДЗ 11 · Kubernetes · CPU simulator', lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    request_id=request.headers.get('x-request-id') or str(uuid.uuid4())
    OUTCOME.labels('invalid').inc()
    log('request', request_id=request_id, model_version=state.version, outcome='invalid', status=422)
    return JSONResponse({'detail':'Некорректная схема запроса','pod':POD,
                         'request_id':request_id,'model_version':state.version,'backend_mode':'simulated'},status_code=422)

@app.get('/health')
async def health():
    return {'alive': True, 'pod': POD}

@app.get('/startup')
async def startup():
    if not state.ready:
        raise HTTPException(503, state.error or 'Прогрев не завершён')
    return {'started': True, 'pod': POD}

@app.get('/ready')
async def ready():
    if not state.ready or state.draining:
        raise HTTPException(503, state.error or 'Прогрев или вывод из обслуживания')
    return {'ready': True, 'pod': POD, 'model_version': state.version,
            'artifact_sha256': state.digest, 'backend_mode': 'simulated'}

@app.post('/drain')
async def drain():
    state.draining = True
    log('drain_started', running=state.running, waiting=state.waiting)
    await asyncio.sleep(4)
    return {'draining': True}

@app.get('/metrics')
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get('/debug')
async def debug():
    return {'pod': POD, 'ready': state.ready, 'running': state.running,
            'waiting': state.waiting, 'calls': state.calls,
            'model_version': state.version, 'cache_size': len(state.cache)}

@app.post('/reset-cache')
async def reset_cache():
    if state.running or state.waiting:
        raise HTTPException(409, 'Сначала завершить запросы')
    state.cache.clear()
    return {'pod': POD, 'cache_size': 0}

@app.post('/generate')
async def generate(body: GenerateRequest, request: Request):
    started = time.perf_counter()
    request_id = request.headers.get('x-request-id') or str(uuid.uuid4())
    base = {'request_id': request_id, 'session_id': body.session_id,
            'pod': POD, 'model_version': state.version, 'artifact_sha256': state.digest,
            'backend_mode': 'simulated', 'client_ip': request.client.host}
    if not state.ready:
        OUTCOME.labels('cold').inc()
        log('request', **base, outcome='cold', status=503)
        raise HTTPException(503, 'Backend ещё не прогрет', headers={
            'X-Pod':POD,'X-Model-Version':state.version or 'unloaded','X-Request-ID':request_id,'X-Backend-Mode':'simulated'})
    if state.running + state.waiting >= state.capacity + state.queue_limit:
        OUTCOME.labels('rejected').inc()
        log('request', **base, outcome='rejected', status=429)
        raise HTTPException(429, 'Очередь заполнена', headers={
            'X-Pod':POD,'X-Model-Version':state.version,'X-Request-ID':request_id,'X-Backend-Mode':'simulated'})
    state.waiting += 1
    WAITING.inc()

    async def stream():
        acquired = False
        first = None
        count = 0
        outcome = 'aborted'
        try:
            await state.slots.acquire()
            acquired = True
            state.waiting -= 1
            WAITING.dec()
            state.running += 1
            RUNNING.inc()
            state.calls += 1
            queued = time.perf_counter() - started
            QUEUE.observe(queued)
            cache_hit = body.prefix in state.cache
            CACHE.labels('hit' if cache_hit else 'miss').inc()
            state.cache[body.prefix] = True
            state.cache.move_to_end(body.prefix)
            while len(state.cache) > int(os.getenv('CACHE_CAPACITY', '16')):
                state.cache.popitem(last=False)
            cost = float(os.getenv('HIT_CPU_SECONDS' if cache_hit else 'MISS_CPU_SECONDS', '.04' if cache_hit else '.24'))
            await asyncio.to_thread(cpu_work, cost)
            # История целиком приходит от клиента, локального хранилища диалогов нет.
            contents = '|'.join(message.content for message in body.messages)
            history_digest = hashlib.sha256(contents.encode()).hexdigest()
            for index in range(body.chunks):
                if await request.is_disconnected():
                    return
                if index:
                    await asyncio.sleep(body.interval_seconds)
                await asyncio.to_thread(cpu_work, float(os.getenv('CHUNK_CPU_SECONDS', '.01')))
                now = time.perf_counter()
                if first is None:
                    first = now
                    FIRST.observe(now - started)
                count += 1
                part = {**base, 'index': index, 'content': f'{state.version}:{contents[:160]}:{index}',
                        'history_sha256': history_digest, 'queue_seconds': queued, 'cache_hit': cache_hit}
                yield 'data: ' + json.dumps(part, ensure_ascii=False) + '\n\n'
            yield 'data: ' + json.dumps({**base, 'done': True, 'parts': count}) + '\n\n'
            outcome = 'completed'
            TOTAL.observe(time.perf_counter() - started)
        finally:
            if acquired:
                state.slots.release()
                state.running -= 1
                RUNNING.dec()
            else:
                state.waiting -= 1
                WAITING.dec()
            OUTCOME.labels(outcome).inc()
            log('request', **base, outcome=outcome, parts=count,
                ttfc_seconds=None if first is None else first-started,
                total_seconds=time.perf_counter()-started)

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'X-Request-ID': request_id, 'X-Pod': POD,
                                      'X-Model-Version': state.version, 'Cache-Control': 'no-cache'})
