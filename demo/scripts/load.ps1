<#
.SYNOPSIS
  Load generator for demo-app. Drives the HPA, the volume, or both.

.DESCRIPTION
  No dependencies beyond PowerShell 7. Two kinds of pressure:

    cpu       calls /burn, which spins the CPU, which moves the HPA
    storage   calls /write, which does timed fsynced writes to the volume
    both      alternates, so you can watch them interact

  A port-forward always hits ONE pod, which is fine for CPU (the HPA reads the
  average across pods and one hot pod is enough to trigger it) but hides the
  interesting part of shared storage. For that, run the in-cluster variant in
  the examples below.

.EXAMPLE
  # port-forward first:  kubectl -n demo-dev port-forward svc/demo-app 8080:80
  .\demo\scripts\load.ps1 -Url http://localhost:8080 -Seconds 240 -Concurrency 4

.EXAMPLE
  # hammer the volume instead of the CPU
  .\demo\scripts\load.ps1 -Mode storage -WriteMb 32 -Seconds 120

.EXAMPLE
  # from inside the cluster, so the Service actually load-balances across pods
  kubectl -n demo-dev run load --rm -it --image=curlimages/curl --restart=Never -- `
    sh -c 'while true; do curl -s -o /dev/null "http://demo-app/burn?seconds=20"; sleep 1; done'
#>
param(
  [string]$Url = "http://localhost:8080",
  [ValidateSet("cpu", "storage", "both")]
  [string]$Mode = "cpu",
  [int]$Seconds = 120,
  [int]$Concurrency = 4,
  [int]$BurnSeconds = 20,
  [int]$WriteMb = 16
)

Write-Host "Driving $Url in '$Mode' mode for ${Seconds}s with $Concurrency workers..." -ForegroundColor Cyan
switch ($Mode) {
  "cpu" { Write-Host "Watch it with: kubectl get hpa,pods -w" -ForegroundColor DarkGray }
  "storage" { Write-Host "Watch it with: kubectl exec <pod> -- sh -c 'while true; do df -h /data; sleep 3; done'" -ForegroundColor DarkGray }
  "both" { Write-Host "Watch it with: kubectl get hpa,pods -w  (and /metrics for the volume series)" -ForegroundColor DarkGray }
}

$deadline = (Get-Date).AddSeconds($Seconds)

$jobs = 1..$Concurrency | ForEach-Object {
  Start-ThreadJob -ArgumentList $Url, $deadline, $BurnSeconds, $WriteMb, $Mode, $_ -ScriptBlock {
    param($url, $deadline, $burn, $mb, $mode, $worker)
    $n = 0
    while ((Get-Date) -lt $deadline) {
      $n++
      try {
        if ($mode -eq "cpu" -or ($mode -eq "both" -and $n % 2 -eq 1)) {
          Invoke-RestMethod -Uri "$url/burn?seconds=$burn" -TimeoutSec 15 | Out-Null
        }
        if ($mode -eq "storage" -or ($mode -eq "both" -and $n % 2 -eq 0)) {
          # A distinct name per worker and iteration, so this exercises the
          # filesystem rather than overwriting one inode over and over.
          $name = "load/w$worker-$n.bin"
          Invoke-RestMethod -Uri "$url/write?mb=$mb&name=$name&fsync=1" -TimeoutSec 60 | Out-Null
        }
        Invoke-RestMethod -Uri "$url/info" -TimeoutSec 10 | Out-Null
      } catch {
        # Failures are expected and often the point: a full volume returns 507,
        # a pod mid-rollout refuses the connection. Keep going.
      }
      Start-Sleep -Seconds 1
    }
    return $n
  }
}

$counts = $jobs | Wait-Job | Receive-Job
$jobs | Remove-Job
Write-Host "Done. $($counts | Measure-Object -Sum | Select-Object -ExpandProperty Sum) iterations." -ForegroundColor Green

if ($Mode -ne "cpu") {
  Write-Host "The volume now has a load/ directory on it. Check and clean up:" -ForegroundColor DarkGray
  Write-Host "  curl `"$Url/df`"" -ForegroundColor DarkGray
  Write-Host "  curl `"$Url/rm?name=load`"" -ForegroundColor DarkGray
}
if ($Mode -ne "storage") {
  Write-Host "Scale-down starts after the HPA stabilization window (60s by default)." -ForegroundColor DarkGray
}
