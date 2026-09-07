<#
.SYNOPSIS
  Tiny load generator for lab-app. Drives the HPA without installing anything.

.EXAMPLE
  # port-forward first:  kubectl -n lab-dev port-forward svc/lab-app 8080:80
  .\scripts\load.ps1 -Url http://localhost:8080 -Seconds 180 -Concurrency 4

.EXAMPLE
  # from inside the cluster instead (real load balancing across pods):
  kubectl -n lab-dev run load --rm -it --image=curlimages/curl -- `
    sh -c 'while true; do curl -s -o /dev/null "http://lab-app/burn?seconds=20"; sleep 1; done'
#>
param(
  [string]$Url = "http://localhost:8080",
  [int]$Seconds = 120,
  [int]$Concurrency = 4,
  [int]$BurnSeconds = 20
)

Write-Host "Driving $Url for ${Seconds}s with $Concurrency workers..." -ForegroundColor Cyan
Write-Host "Watch it with: kubectl get hpa,pods -w" -ForegroundColor DarkGray

$deadline = (Get-Date).AddSeconds($Seconds)
$jobs = 1..$Concurrency | ForEach-Object {
  Start-ThreadJob -ArgumentList $Url, $deadline, $BurnSeconds -ScriptBlock {
    param($url, $deadline, $burn)
    while ((Get-Date) -lt $deadline) {
      try {
        Invoke-RestMethod -Uri "$url/burn?seconds=$burn" -TimeoutSec 10 | Out-Null
        Invoke-RestMethod -Uri "$url/info" -TimeoutSec 10 | Out-Null
      } catch {}
      Start-Sleep -Seconds 1
    }
  }
}

$jobs | Wait-Job | Receive-Job
$jobs | Remove-Job
Write-Host "Done. Scale-down starts after the HPA stabilization window." -ForegroundColor Green
