# ДЗ 11 — Kubernetes под нагрузкой

Репозиторий: https://github.com/doshik722/MLOps_dz11 . Итоговый отчёт — `report.html`.
Выполнены 15 сравнительных прогонов по 120 секунд, всего 9000 запросов в опыте B. Семь проверок проходят, из них пять обращаются к живому Kubernetes-стенду или проверяют его аварийные результаты. Сырые файлы включают сохранённые исходные сбои и отдельно повторённые опыты; всего в мастер-CSV 16476 попыток.

Продолжение API ДЗ 8–10. Основной backend явно обозначен `simulated`: это CPU-имитатор с проверяемым файлом артефакта, ограниченной очередью, LRU-кешем заданной стоимости и SSE-ответами. Обучение и замеры Qwen в эту работу не входят.

## Установка

Нужны Windows с работающим Docker Desktop/Linux Engine, Python 3.12, uv, kubectl и PowerShell 7. Локальные инструменты: kind 0.30.0, Helm 3.18.6. Они хранятся в `tools/` и не включаются в Git.

```powershell
cd D:\downloads_programs\Git_hub_progr\Labs_MLops\hw11
New-Item -ItemType Directory -Path tools -Force
Invoke-WebRequest https://kind.sigs.k8s.io/dl/v0.30.0/kind-windows-amd64 -OutFile tools/kind.exe
Invoke-WebRequest https://get.helm.sh/helm-v3.18.6-windows-amd64.zip -OutFile tools/helm.zip
Expand-Archive tools/helm.zip -DestinationPath tools -Force
Invoke-WebRequest https://dl.k8s.io/release/v1.34.0/bin/windows/amd64/kubectl.exe -OutFile tools/kubectl.exe
uv sync --locked
docker info
```

`docker info` должен показать раздел Server с ресурсами Linux Engine. HTTP 500 от Docker и зависшее `wsl --status` означают, что запуск Kubernetes ещё невозможен. Локальные тесты при этом не доказывают выполнение кластерной части.

## Полный запуск

```powershell
uv run python -u run_all.py
```

Выполняются сборка образа, отдельный кластер `hw11`, Helm, опыты A/B/C, сравнение гистограммы с клиентским журналом, проверки и отчёт. Полный запуск занимает больше 30 минут только на обязательные 120-секундные сравнения; загрузка образов и подготовка кластера добавляют время. Вывод идёт в терминал, журналы — в `logs/`, сырые данные — в `results/`. Существующие завершённые B-прогоны не запускаются повторно; для полного нового опыта сохраните прежние `results/` отдельно, затем используйте пустой каталог.

Отдельные этапы:

```powershell
pwsh -File stand.ps1 build
pwsh -File stand.ps1 up
uv run python -u experiments.py a
uv run python -u experiments.py balance
uv run python -u experiments.py c
uv run python verify_metrics.py
uv run python -m unittest discover -s tests -v
uv run python report.py
```

Короткая локальная проверка до Kubernetes:

```powershell
uv run python local_check.py
uv run python -m unittest discover -s tests -p test_analysis.py -v
.\tools\windows-amd64\helm.exe lint chart
```

## Наблюдение

```powershell
pwsh -File stand.ps1 status
pwsh -File stand.ps1 ports
```

Grafana: http://127.0.0.1:13000/d/hw11 ; Prometheus: http://127.0.0.1:19090 . Все адреса предназначены для локального стенда. Во время `experiments.py` эти порты заняты его собственными перенаправлениями; отдельный `ports` в это время не запускается.

`results/*-timeline.json` связывает Pod Ready, EndpointSlice, HPA и resource metrics по времени. `results/runs/*.json` содержит каждую запланированную попытку и её фактические монотонные отметки. `results/requests.csv` и `results/summary.json` собираются из этих файлов. В `report.html` отсутствующие опыты явно обозначаются; графики появляются только после сохранённых измерений.

## Состав проекта

- `legacy/` — неизменённые исходные service.py/release.py из предыдущей работы;
- `app.py` — проверка SHA/версии, прогрев, очередь, кеш, поток и метрики;
- `chart/` — Deployment, два Service, ConfigMap, probes, HPA, Prometheus, Grafana и экспорт дашборда;
- `monitoring/metrics-server.yaml` — metrics-server 0.8.0; для локального kind добавлен `--kubelet-insecure-tls`;
- `loadgen.py` — четыре клиентских агента, открытое расписание, отдельные HTTP-соединения;
- `experiments.py` — воспроизведение и повтор опытов, Kubernetes-наблюдатели;
- `tests/test_kubernetes.py` — пять обязательных собственных проверок;
- `verify_metrics.py` — classic Histogram и клиентский p95 для одних и тех же 16 ответов;
- `docs/review.md` — ревью предложения о постоянной привязке, кейс Google из лекции;
- `report.py` — таблицы, четыре графика и HTML-отчёт по реальным файлам.

## Ресурсы и удаление

Выделенный WSL-стенд ограничен 6 GB RAM, 4 CPU, swap 512 MB; на время работы отключены графические Linux-приложения. Это настройки `.wslconfig` Windows, не свойства Helm. В исходной конфигурации файл отсутствовал. Docker-диск прежних ДЗ расположен на D:; его удаление не требуется.

API: 2 реплики, максимум 3 при HPA; на каждую requests 250m/80Mi, limits 600m/192Mi. Prometheus: retention 2 часа/128 MB, emptyDir 256 MiB; Grafana: emptyDir 128 MiB. Собственные данные и исходный артефакт имеют небольшой ограниченный размер; фактический размер образов и стенда фиксируется после запуска.

Удаляется только кластер данного задания:

```powershell
pwsh -File stand.ps1 down
```

Команда также останавливает локальные просмотры, запущенные из этой папки и записанные в `logs/view-pids.json`. Загруженные образы остаются в кеше Docker. При необходимости после удаления кластера можно отдельно убрать собственный образ: `docker image rm hw11-api:0.1.0`.

Это уничтожает локальные метрики и ресурсы внутри `hw11`. Сначала сохраните результаты опытов. Не используйте глобальные `docker system prune` или удаление дисков WSL для очистки этой работы. Репозитории и Docker volumes прошлых ДЗ остаются отдельно.
