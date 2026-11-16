param([ValidateSet('build','up','ports','down','status')][string]$Action='status')
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
$env:KUBECONFIG=Join-Path $PSScriptRoot '.kubeconfig'
$env:PATH=(Join-Path $PSScriptRoot 'tools')+';'+$env:PATH
$kind=Join-Path $PSScriptRoot 'tools\kind.exe'
$helm=Join-Path $PSScriptRoot 'tools\windows-amd64\helm.exe'
function Check-Exit { if($LASTEXITCODE -ne 0) { throw "Команда завершилась с кодом $LASTEXITCODE" } }
switch($Action) {
  'build' {
    uv sync --locked; Check-Exit
    uv run python make_dashboard.py; Check-Exit
    uv run python environment.py --require-docker; Check-Exit
    docker build -t hw11-api:0.1.0 .; Check-Exit
  }
  'up' {
    $existing=& $kind get clusters
    if($existing -notcontains 'hw11') {
      & $kind create cluster --name hw11 --image kindest/node:v1.34.0 --config kind.yaml --kubeconfig $env:KUBECONFIG --wait 180s; Check-Exit
    }
    & $kind load docker-image hw11-api:0.1.0 --name hw11; Check-Exit
    kubectl apply -f monitoring\metrics-server.yaml; Check-Exit
    & $helm upgrade --install hw11 chart --namespace hw11 --create-namespace --history-max 8; Check-Exit
    kubectl -n hw11 rollout status deployment/api --timeout=180s; Check-Exit
  }
  'ports' {
    Write-Host 'Grafana: http://127.0.0.1:13000 ; Prometheus: http://127.0.0.1:19090'
    try {
      $dashboard=Invoke-RestMethod -Uri 'http://127.0.0.1:13000/api/dashboards/uid/hw11' -TimeoutSec 2
      if($dashboard.dashboard.uid -eq 'hw11') { Write-Host 'Просмотр этого стенда уже работает.'; return }
    } catch {}
    Start-Process -FilePath 'kubectl' -ArgumentList @('-n','hw11','port-forward','service/prometheus','19090:9090') -WindowStyle Hidden
    kubectl -n hw11 port-forward service/grafana 13000:3000
  }
  'status' {
    kubectl -n hw11 get pods,services,hpa
  }
  'down' {
    $pidFile=Join-Path $PSScriptRoot 'logs\view-pids.json'
    if(Test-Path -LiteralPath $pidFile) {
      $ownedViews=Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
      foreach($property in $ownedViews.PSObject.Properties) {
        $viewProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($property.Value)" -ErrorAction SilentlyContinue
        if($viewProcess -and $viewProcess.CommandLine.Contains($PSScriptRoot) -and $viewProcess.Name -in @('kubectl.exe','python.exe')) {
          Stop-Process -Id $viewProcess.ProcessId -ErrorAction SilentlyContinue
        }
      }
    }
    # Удаляется только отдельный кластер этого задания, Docker volumes других ДЗ не затрагиваются.
    & $kind delete cluster --name hw11; Check-Exit
  }
}
