"""Проверки потокового контракта до запуска Kubernetes; не заменяют кластерные опыты."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import httpx
from loadgen import one

async def check():
    with tempfile.TemporaryDirectory() as directory:
        artifact=Path(directory)/'model.json'
        artifact.write_text('{"version":"A","backend_mode":"simulated"}',encoding='utf-8')
        env={**os.environ,'ARTIFACT_PATH':str(artifact),'ARTIFACT_SHA256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
             'MODEL_VERSION':'A','WARMUP_SECONDS':'2'}
        process=subprocess.Popen([os.sys.executable,'-m','uvicorn','app:app','--port','18088','--no-access-log'],env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(60):
                    try:
                        if (await client.get('http://127.0.0.1:18088/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                cold=await client.get('http://127.0.0.1:18088/ready');assert cold.status_code==503
                await asyncio.sleep(2.2)
                ready=await client.get('http://127.0.0.1:18088/ready');assert ready.status_code==200
                body={'messages':[{'role':'user','content':'control-A'}],'session_id':'alpha','chunks':3}
                result=await one(client,'http://127.0.0.1:18088',body,'local-1',time.perf_counter(),'local',0)
                assert result['outcome']=='completed' and result['parts']==3 and result['model_version']=='A'
                assert len(result['intervals_seconds'])==2 and result['backend_mode']=='simulated'
                print(json.dumps({'cold_status':cold.status_code,'ready':ready.json(),'stream':result},ensure_ascii=False,indent=2))
        finally:
            process.terminate()
            output=process.communicate(timeout=10)[0]
            print(output)

if __name__=='__main__':asyncio.run(check())
