"""Отчёт и четыре графика по фактическим результатам; отсутствующие опыты явно отмечаются."""
import csv
import hashlib
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parent
R=ROOT/'results'
FIG=R/'figures'
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10})

def load(name,default=None):
    path=R/name
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def quantile(values,q):
    values=sorted(value for value in values if value is not None and math.isfinite(value))
    if not values:return None
    where=(len(values)-1)*q;left=int(where);right=min(left+1,len(values)-1)
    return values[left]+(values[right]-values[left])*(where-left)

def summarize(rows):
    success=[row for row in rows if row['outcome']=='completed']
    return {'attempts':len(rows),'outcomes':dict(Counter(row['outcome'] for row in rows)),
      'successful_n':len(success),'p50_ttfc':quantile([row['ttfc_seconds'] for row in success],.5),
      'p95_ttfc':quantile([row['ttfc_seconds'] for row in success],.95),
      'p95_total':quantile([row['total_seconds'] for row in success],.95),
      'p95_send_lag':quantile([row['send_lag_seconds'] for row in rows],.95),
      'error_fraction':sum(row['outcome']!='completed' for row in rows)/len(rows) if rows else None,
      'by_pod':dict(Counter(row['pod'] for row in rows)),
      'cache_hit_fraction':sum(row.get('cache_hit') is True for row in success)/len(success) if success else None}

def rows_from_file(path):
    agents=json.loads(path.read_text(encoding='utf-8'))
    return [row for agent in agents for row in agent['result']['rows']]

def plot_ready():
    data=load('a-rollout-timeline.json',[])
    if not data:return False
    start=data[0]['unix'];xs=[];ready=[];endpoints=[]
    for item in data:
        if 'pods' not in item:continue
        xs.append(item['unix']-start)
        ready.append(sum(any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[]))
                         and not p['metadata'].get('deletionTimestamp') for p in item['pods']['items']))
        endpoints.append(sum(e.get('conditions',{}).get('ready',False) for group in item['endpoints']['items'] for e in group['endpoints']))
    fig,ax=plt.subplots(figsize=(10,4));ax.step(xs,ready,where='post',label='Ready Pod')
    ax.step(xs,endpoints,where='post',label='Ready EndpointSlice',linestyle='--')
    long=load('a-long-stream.json')
    if long:
        ax.axvline(long['rollout_started_unix']-start,color='purple',label='Начало rollout')
        ax.axvspan(long['row']['sent_unix']-start,long['row']['end_unix']-start,alpha=.15,color='green',label='Длительный поток')
    ax.set(xlabel='От начала наблюдения, с',ylabel='Число',title='Прогрев и rollout A → B; CPU simulated')
    ax.legend();ax.grid(alpha=.2);fig.tight_layout();fig.savefig(FIG/'01-ready-rollout.png',dpi=160);plt.close(fig);return True

def plot_pods(all_runs):
    selected=all_runs.get('b-api-sticky-skew-1')
    if not selected:return False
    by=defaultdict(list)
    for row in selected:
        if row['outcome']=='completed':by[row['pod']].append(row)
    names=list(by);fig,axes=plt.subplots(1,2,figsize=(11,4))
    for ax,field,title in zip(axes,['ttfc_seconds','queue_seconds'],['TTFC','Очередь backend']):
        ax.boxplot([[row[field] for row in by[name]] for name in names],tick_labels=[name[-5:] for name in names],showfliers=False)
        ax.set(xlabel='Pod, последние 5 знаков имени',ylabel='Время, с',title=title);ax.grid(axis='y',alpha=.2)
    fig.suptitle('ClientIP, 80% запросов от одного клиента; 120 с, CPU simulated')
    fig.tight_layout();fig.savefig(FIG/'02-pod-queue.png',dpi=160);plt.close(fig);return True

def plot_policies(all_runs):
    groups=defaultdict(list)
    for name,rows in all_runs.items():
        if not name.startswith('b-'):continue
        group=name.rsplit('-',1)[0];groups[group].append(summarize(rows))
    if not groups:return False
    fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True)
    names=list(groups)
    for index,name in enumerate(names):
        for summary in groups[name]:
            axes[0].scatter(index,summary['p95_ttfc'],color='purple')
            axes[1].scatter(index,100*summary['error_fraction'],color='firebrick')
    axes[0].set(ylabel='p95 TTFC успешных, с',title='Каждая точка — отдельный 120-секундный прогон; CPU simulated')
    axes[1].set(ylabel='Ошибки/отказы, %',xticks=range(len(names)),xticklabels=[x[2:] for x in names])
    axes[1].tick_params(axis='x',rotation=20)
    for ax in axes:ax.grid(alpha=.2)
    fig.tight_layout();fig.savefig(FIG/'03-policy-repeats.png',dpi=160);plt.close(fig);return True

def plot_hpa():
    data=load('c-hpa-timeline.json',[])
    if not data:return False
    start=data[0]['unix'];xs=[];replicas=[];ready=[];cpu=[]
    for item in data:
        if 'pods' not in item:continue
        xs.append(item['unix']-start)
        replicas.append(len([p for p in item['pods']['items'] if not p['metadata'].get('deletionTimestamp')]))
        ready.append(sum(any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[])) for p in item['pods']['items']))
        total=0
        for pod in item.get('metrics',{}).get('items',[]):
            if not pod['metadata']['name'].startswith('api-'):continue
            for container in pod['containers']:
                value=container['usage']['cpu']
                total+=float(value[:-1])/1e9 if value.endswith('n') else float(value[:-1])/1000 if value.endswith('m') else float(value)
        cpu.append(total)
    fig,ax=plt.subplots(figsize=(10,4));ax.step(xs,replicas,where='post',label='Pod')
    ax.step(xs,ready,where='post',label='Ready');ax.set(xlabel='От начала, с',ylabel='Реплики',title='HPA 2 → 3 → 2, CPU simulated')
    second=ax.twinx();second.plot(xs,cpu,color='gray',alpha=.6,label='CPU');second.set_ylabel('Суммарная CPU, ядра')
    info=load('c-hpa.json')
    if info:ax.axvline(info['load_stopped']-start,color='red',linestyle='--',label='Снижение нагрузки')
    ax.legend(loc='upper left');ax.grid(alpha=.2);fig.tight_layout();fig.savefig(FIG/'04-hpa.png',dpi=160);plt.close(fig);return True

def main():
    R.mkdir(exist_ok=True);FIG.mkdir(exist_ok=True)
    all_runs={path.stem:rows_from_file(path) for path in sorted((R/'runs').glob('*.json'))}
    rows=[row for run in all_runs.values() for row in run]
    force=load('a-forced-kill.json')
    if force and force.get('row'):rows.append(force['row'])
    summaries={name:summarize(run) for name,run in all_runs.items()}
    (R/'summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    if rows:
        columns=['run_id','request_id','client','planned_mono','sent_mono','sent_unix','send_lag_seconds','first_mono','end_mono','end_unix','status','outcome','pod','model_version','parts','ttfc_seconds','total_seconds','queue_seconds','cache_hit','server_client_ip','backend_mode','intervals_seconds','error']
        with (R/'requests.csv').open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    figures=[plot_ready(),plot_pods(all_runs),plot_policies(all_runs),plot_hpa()]
    checks=load('check-status.json',{})
    repeat_verification=load('repeat-verification.json',{})
    complete=(all(figures) and checks.get('exit_code')==0 and len(repeat_verification)==15
              and (R/'checks/dialog-isolation.json').exists() and (R/'prometheus-comparison.json').exists())
    esc=lambda value:html.escape(str(value))
    table=''.join('<tr><td>'+esc(name)+'</td><td>'+str(s['attempts'])+'</td><td>'+str(s['successful_n'])+'</td><td>'+esc(s['outcomes'])+'</td><td>'+('—' if s['p95_ttfc'] is None else f"{s['p95_ttfc']:.3f}")+'</td><td>'+f"{100*s['error_fraction']:.1f}"+'</td></tr>' for name,s in summaries.items())
    shots=''.join(f'<figure><img src="results/figures/{path.name}"><figcaption>Рисунок {i} — {esc(title)}</figcaption></figure>'
      for i,(path,title) in enumerate(zip([FIG/'01-ready-rollout.png',FIG/'02-pod-queue.png',FIG/'03-policy-repeats.png',FIG/'04-hpa.png'],
        ['Прогрев и обновление','Задержка и очередь по Pod','Три повтора сравнения политик','Масштабирование вместе с нагрузкой']),1) if path.exists())
    review=(ROOT/'docs/review.md').read_text(encoding='utf-8')
    evidence=load('environment.json',{})
    tests=load('check-status.json',{})
    status='Все обязательные опыты выполнены; подробности и ограничения приведены ниже.' if complete else 'Работа не завершена: реальные Kubernetes-опыты и кластерные проверки пока не подтверждены. Локальная проверка HTTP и Helm lint не заменяют результаты Kubernetes.'
    report=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>ДЗ 11 · Kubernetes под нагрузкой</title>
<style>body{{font:18px/1.6 system-ui;color:#18263d;max-width:1120px;margin:auto;padding:32px}}h1,h2{{color:#453174}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #ccc;padding:8px;text-align:left}}img{{max-width:100%}}pre{{white-space:pre-wrap;background:#f2f1f8;padding:16px}}.status{{padding:20px;background:#eee8ff}}a{{color:#5341aa}}@media print{{body{{font:12pt/1.4 Georgia}}figure{{break-inside:avoid}}}}</style>
<h1>ДЗ 11. Сервис работает, пользователи ждут</h1><p class="status">{esc(status)}</p>
<h2>Цель и задание</h2><p>Разместите свой API в Kubernetes, воспроизведите три эксплуатационные проблемы и подтвердите исправления замерами.</p>
<p>Полное исходное задание: <a href="homework11.html">homework11.html</a>. Основа API ДЗ 8–10 сохранена в legacy; перенесены проверка SHA артефакта, неизменная загруженная версия и готовность после прогрева. Добавлены ограниченная очередь, потоковая выдача, внешний контекст диалога и инструменты наблюдения.</p>
<h2>Стенд и воспроизводимость</h2><p>Backend во всех основных сравнениях — simulated. Веса Qwen, обучение и GPU не используются. Файл model.json приходит из ConfigMap, сверяется SHA256 и загруженной версией. Каждая реплика независимо восстанавливает этот файл; кеш и состояние очереди локальны, история диалога целиком передаётся в запросе.</p>
<p>Число реплик — 2, HPA — 2–3; requests 250m/80Mi, limits 600m/192Mi на API. Прогрев — 20 с, startup — до 90 с, readiness — после прогрева, liveness — доступность HTTP. Grace period — 75 с, preStop снимает readiness и даёт 4 с распространиться изменению endpoints; Uvicorn ждёт активные потоки до 60 с.</p>
<p>Источник артефакта, образ, версия, прогрев, очередь и реплики заданы в Helm values. Зафиксированы pyproject.toml, uv.lock, requirements.txt и версии инструментов. Prometheus хранит не более 2 ч/128 MB, том ограничен 256 MiB, Grafana — 128 MiB. Удаление только своего стенда: <code>pwsh -File stand.ps1 down</code>; глобальный docker prune не применяется.</p>
<pre>{esc(json.dumps(evidence,ensure_ascii=False,indent=2))}</pre>
<h2>Опыт A: прогрев, rollout и откат</h2><p>Гипотеза: HTTP-процесс может быть жив, пока backend не готов. Воспроизведение: Helm brokenProbes=true направляет startup/readiness в /health. Исправление: отдельные /startup и /ready; backend остаётся тем же. Подтверждение — HTTP-коды, серверные записи cold, Ready и EndpointSlice до/после. Штатное обновление A→B выполняется при активном длинном потоке; от него требуется ноль 5xx и оборванных потоков. Неготовая B не должна войти в рабочие endpoints, затем Helm rollback возвращает A. SIGKILL исследуется отдельно и не маскируется повтором клиента.</p>
<h2>Опыт B: горячая реплика</h2><p>Сравниваются Service None и ClientIP над одним Deployment. Четыре клиентских Pod имеют разные IP; HTTP keep-alive отключён. iptables выбирает endpoint на соединение, поэтому None не называется строгим round-robin. В равномерном сценарии клиенты чередуются; в перекошенном 80% отправок назначены client-0. Для каждой пары policy/traffic предусмотрено три повтора по 120 с; после исправления ClientIP→None — ещё три перекошенных прогона. Каждый прогон начинает с пустого локального кеша.</p>
<p>Имитатор: running=1, waiting≤8; LRU-кеш содержит не более 16 префиксов. Cache hit стоит 0,04 CPU-с, miss — 0,24 CPU-с; каждая часть — 0,01 CPU-с, всего 4 части с интервалом 0,05 с. Работа PBKDF2 измеряется thread_time, а не заменяется sleep. План — 5 запросов/с, 600 на прогон. Истинные график отправки, задержка генератора и наблюдённый клиентский IP сохраняются. Неожиданное отсутствие перекоса сохраняется вместе с трассой и не подменяется ожидаемым результатом.</p>
<table><tr><th>Прогон</th><th>Попытки</th><th>Успешные n</th><th>Исходы</th><th>p95 TTFC, с</th><th>Ошибки, %</th></tr>{table}</table>
<h2>Опыт C: HPA и алерты</h2><p>HPA autoscaling/v2 использует CPU относительно requests, диапазон 2–3, scaleDown stabilization 30 с. Нагрузка вычислительная. До измерения проверяются metrics.k8s.io, requests и условия HPA; сохраняются реальные времена создания и готовности третьего Pod, затем возврат к двум. Горячий клиент повторяется при ClientIP: новая реплика сама по себе не отменяет старую привязку.</p>
<p>Алерт HighTTFC: p95 за 30 с >0,8 с при ≥20 наблюдениях, for=15 с. HighRejectionRatio: доля rejected >10% за 30 с при ≥20 исходах, for=15 с. Управляемая нагрузка должна показать inactive→pending→firing→inactive. Настройки находятся в chart/templates/monitoring.yaml; экспорт дашборда — chart/dashboards/hw11.json, запросы — monitoring/queries.json.</p>
<h2>Методика измерения</h2><p>Клиент фиксирует perf_counter для плана, отправки, первой содержательной части и завершения; Unix-время служит для сопоставления с Kubernetes, не для расчёта задержек. TTFC=first−sent, полная задержка=end−sent, inter-chunk latency — разность соседних моментов получения содержательных частей. Для N частей имеется N−1 интервалов; при N=1 список пуст. Служебное done не считается содержательной частью. Это части имитатора, поэтому TTFT, TPOT и tokens/s не заявляются.</p>
<p>Запланированные, отправленные, completed, rejected, aborted, timeout и transport_error учитываются отдельно; HTTP 200 без конечного done считается обрывом. Процентили p50/p95 считаются только для completed, рядом приводятся n и доля ошибок. Histogram использует границы 0.025…51.2 с; PromQL суммирует bucket по le и нужным разрезам. Сравнение с клиентом проводится для одинакового ограниченного набора ответов с вычитанием bucket до/после, отдельно от фоновых опытов. Идентификаторы запросов и диалогов не становятся метками Prometheus.</p>
{shots}<h2>Пять собственных проверок</h2><p>tests/test_kubernetes.py проверяет живые HTTP в Kubernetes: исключение непрогретого Pod из endpoints, фактическую версию после отката, восстановление артефакта/истории после замены Pod, отдельный учёт отказов и обрывов, изоляцию двух контрольных диалогов. results/checks содержит фактические ответы; неподтверждённые проверки не считаются пройденными.</p><pre>{esc(json.dumps(tests,ensure_ascii=False,indent=2))}</pre>
<h2>Ограничения</h2><p>Один узел kind, Windows/WSL и CPU-имитатор не представляют production inference engine. Prefix cache воспроизводит заданную стоимость и не хранит реальные KV. Нет автоматического переноса начатого потока, распределённого кеша или автоматических клиентских повторов. Исправление маршрутизации требует повторного измерения с настоящей моделью, её prefill/decode и сетевым стеком. Если Docker не готов, кластерные опыты, графики и выводы остаются неподтверждёнными.</p>
<h2>Ревью PR</h2><pre>{esc(review)}</pre>
<h2>Вывод</h2><p>{esc('Результаты выполненных опытов и их ограничения представлены в сырых журналах и таблицах.' if complete else 'Подготовлены сервис, Helm, мониторинг, генератор и проверки. Для завершения необходим работающий Docker/Kubernetes и выполнение всех предусмотренных опытов; численные результаты не выдумывались.')}</p></html>'''
    (ROOT/'report.html').write_text(report,encoding='utf-8')
    print(json.dumps({'report':'report.html','complete':complete,'runs':len(all_runs),'requests':len(rows),'figures':sum(figures)},ensure_ascii=False))

if __name__=='__main__':main()
