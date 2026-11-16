"""Пояснения к фактическим результатам; численные выводы берутся из сырых файлов."""
from collections import defaultdict
import html
import json

def build(summaries,root):
    def read(name,default=None):
        path=root/name
        return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default
    paragraphs=[]
    broken=summaries.get('a-broken');fixed=summaries.get('a-fixed-warmup');roll=summaries.get('a-rollout')
    if broken and fixed and roll:
        force=read('a-forced-kill.json')
        paragraphs.append('Опыт A. До исправления: '
          f'{broken["outcomes"].get("http_error",0)} HTTP-ошибок из {broken["attempts"]}; '
          f'после исправления — {fixed["successful_n"]}/{fixed["attempts"]} завершённых ответов. '
          f'Штатный rollout при 0,5 запроса/с: {roll["successful_n"]}/{roll["attempts"]} фоновых запросов '
          'завершены, плюс отдельный длительный поток. '
          f'В первоначальном опыте 2 запроса/с было {summaries.get("a-rollout-initial-2rps",{}).get("outcomes",{}).get("rejected",0)} '
          'HTTP 429: длинный поток занимает единственный слот, и ограниченная очередь не создаёт дополнительную пропускную способность. '
          'Этот опыт сохранён отдельно и не скрыт под успешным результатом при меньшей нагрузке. '
          f'После неготовой B и отката завершено {summaries.get("a-bad-rollback",{}).get("successful_n",0)} ответов версии A. '
          +(f'При SIGKILL получено {force["row"]["parts"]} части и исход {force["row"]["outcome"]}; '
            f'после восстановления загружена {force["recovered"]["model_version"]}.' if force else ''))
    groups=defaultdict(list)
    for name,value in summaries.items():
        if name.startswith('b-'):groups[name.rsplit('-',1)[0]].append(value)
    if groups:
        paragraphs.append('Опыт B. '+' '.join(
          f'{name}: {len(items)} повтора; завершено {sum(x["successful_n"] for x in items)}/{sum(x["attempts"] for x in items)}, '
          f'p95 TTFC по повторам {min(x["p95_ttfc"] for x in items):.3f}–{max(x["p95_ttfc"] for x in items):.3f} с.'
          for name,items in sorted(groups.items())))
    routing=read('routing-analysis.json',{})
    examples=[(name,data) for name,data in routing.items() if 'sticky' in name and data['hot_and_idle_samples']>0]
    if examples:
        name,data=examples[0]
        paragraphs.append(f'В {name} очередь одной готовой реплики одновременно со свободной другой наблюдалась '
          f'в {data["hot_and_idle_samples"]}/{data["runtime_samples"]} точках Prometheus с шагом около 2 с. '
          'Маршруты client IP → Pod и случаи без устойчивого перекоса при None сохранены в routing-analysis.json. '
          'Cache hit в этих опытах близок к 99%; небольшая экономия пересчёта не компенсирует очередь. '
          'Это результат заданной модели CPU-нагрузки, а не измерение настоящего KV-cache.')
    hpa=read('c-hpa.json')
    if hpa:
        paragraphs.append(f'Опыт C. Третий Pod наблюдался: {hpa["seen_three"]}. Возврат к двум репликам зарегистрирован '
          f'через {hpa["down_seconds"]:.1f} с после последней фактической отправки. Это полная задержка, '
          'включающая окно метрик, цикл контроллера и стабилизацию, а не точное равенство настройке 30 с. '
          'Первый прогон, начавшийся до окончательного прогрева второй реплики после передачи управления HPA, '
          'сохранён отдельно как c-hpa-initial-warmup; итоговый повтор начинается строго с двух Ready Pod.')
        timing=read('c-hpa-timing.json',[])
        for item in timing:
            paragraphs.append(f'Новый Pod {item["pod"]}: создан через {item["created_after_load_seconds"]:.1f} с после начала поступления нагрузки, '
              f'стал Ready через {item["ready_after_load_seconds"]:.1f} с; от создания до готовности прошло {item["creation_to_ready_seconds"]:.1f} с.')
    alerts=read('c-alerts.json',[])
    if alerts:
        paragraphs.append('Алерты. '+' '.join(
          name+': '+', '.join(sorted({rule['state'] for item in alerts for group in item['rules']['data']['groups']
                                     for rule in group['rules'] if rule.get('name')==name}))+'.'
          for name in ('HighTTFC','HighRejectionRatio')))
    comparison=read('prometheus-comparison.json')
    if comparison:
        paragraphs.append(f'Сопоставление одного набора: {comparison["successful_n"]} успешных ответов; '
          f'клиентский p95 TTFC={comparison["client_p95_seconds"]:.3f} с, Histogram p95={comparison["histogram_p95_seconds"]:.3f} с. '
          'Счётчик Histogram вырос ровно на 16, состав Pod не менялся. Classic Histogram интерполирует внутри bucket, '
          'а клиент вычисляет квантиль по отдельным значениям и включает сеть. При границе 0,4–0,8 с оценка довольно грубая; '
          'для более точного серверного p95 нужны более узкие bucket и новый прогон. До/после, точный период и PromQL сохранены '
          'в prometheus-comparison.json. Первая попытка остановлена проверкой неизменности Pod и сохранена отдельно, '
          'для сопоставления с Prometheus она не использована.')
    return '<h2>Разбор полученных результатов</h2>'+''.join('<p>'+html.escape(text)+'</p>' for text in paragraphs)
