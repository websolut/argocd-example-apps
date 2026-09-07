# aks-lab

A small Helm chart built for **learning AKS**: autoscaling, probes, sidecars,
rolling updates and log collection. Deployed with Argo CD.

The app is ~350 lines of Python standard library mounted from a ConfigMap onto
the stock `python:3.12-alpine` image, so there is **nothing to build and no
container registry to set up**. You clone, point Argo CD at the repo, and start
experimenting.

## What the app does

It serves HTTP and continuously prints structured JSON logs to stdout. Every
interesting Kubernetes behaviour has an endpoint that triggers it:

| Endpoint | What it does | What you learn from it |
|---|---|---|
| `/` `/info` | pod name, node, version, colour, uptime | load balancing across replicas |
| `/healthz` | liveness endpoint | liveness probe = restart |
| `/readyz` | readiness endpoint | readiness probe = traffic |
| `/burn?seconds=120&workers=1` | burns CPU | HPA scale-up on CPU |
| `/mem?mb=200&seconds=120` | holds memory | HPA on memory, OOMKilled |
| `/slow?ms=3000` | slow response | probe timeouts, ingress timeouts |
| `/error?code=503` | logs an error, returns it | log filtering, alerting |
| `/crash` | `exit(1)` | restarts, `CrashLoopBackOff`, backoff timing |
| `/toggle?what=ready&value=false` | fails readiness | pod leaves Service endpoints |
| `/toggle?what=health&value=false` | fails liveness | kubelet kills the container |
| `/lograte?rps=100` | changes log volume | log pipeline cost and throughput |
| `/metrics` | Prometheus metrics | ServiceMonitor / Managed Prometheus |
| `/env` | non-secret env vars | ConfigMaps, Downward API |

On `SIGTERM` it fails readiness, keeps serving for `shutdownDelaySeconds`, then
exits — so graceful shutdown is visible in `kubectl logs` during a rollout.

## Repo layout

```
charts/lab-app/
  Chart.yaml
  values.yaml            # documented defaults - read this one
  values-dev.yaml        # overlay: autoscaling on, 2-8 pods
  values-prod.yaml       # overlay: PDB, anti-affinity, longer grace periods
  values-sidecar.yaml    # overlay: both sidecar patterns turned on
  files/app.py           # the application
  templates/             # deployment, service, hpa, pdb, ingress,
                         # httproute, servicemonitor
argocd/
  application-dev.yaml   # auto-sync + self-heal
  application-prod.yaml  # manual sync
  applicationset.yaml    # optional: one object generates both
docs/LABS.md             # guided exercises
scripts/                 # tiny load generators (bash + PowerShell)
```

## Quick start

### 1. Push this repo somewhere Argo CD can read

```powershell
cd c:\git\AKS\demo
git remote add origin https://github.com/YOUR-ORG/aks-lab.git
git push -u origin main
```

Then replace `repoURL` in `argocd/application-dev.yaml` with your remote and
commit that too. For a private repo, register credentials first:
`argocd repo add https://github.com/YOUR-ORG/aks-lab.git --username x --password <PAT>`.

### 2. Deploy it

```powershell
kubectl apply -f argocd/application-dev.yaml
kubectl -n argocd get application lab-app-dev -w
```

Or skip Argo CD while you are still editing the chart:

```powershell
helm upgrade --install lab-app charts/lab-app `
  -f charts/lab-app/values.yaml -f charts/lab-app/values-dev.yaml `
  -n lab-dev --create-namespace
```

### 3. Look at it

```powershell
kubectl -n lab-dev get pods,svc,hpa
kubectl -n lab-dev logs -l app.kubernetes.io/name=lab-app -f --prefix --tail=20
kubectl -n lab-dev port-forward svc/lab-app 8080:80
curl http://localhost:8080/info
```

### 4. Make it scale

```powershell
curl "http://localhost:8080/burn?seconds=180"
kubectl -n lab-dev get hpa,pods -w
```

Then work through [docs/LABS.md](docs/LABS.md).

## Prerequisites

- An AKS cluster (a single `Standard_B2s`-class node pool is enough) and
  `kubectl` context pointing at it
- `helm` 3.8+ if you want to render or install locally
- Argo CD in the cluster:
  ```powershell
  kubectl create namespace argocd
  kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
  ```
- `metrics-server` for the HPA — AKS ships it. Verify with `kubectl top pods`;
  if that fails, the HPA will report `<unknown>` and never scale.
- Only for the HTTPRoute overlay: the Gateway API CRDs, a controller, and an
  existing Gateway to attach to (`kubectl get gatewayclass`, `kubectl get gateway -A`).
  Not needed for anything else.

Neither `helm` nor `kubectl` was on this machine when the repo was created —
`winget install Kubernetes.kubectl Helm.Helm` (or `az aks install-cli`) gets you
both.

## Deliberate design choices worth knowing

- **No `replicas` in the Deployment when autoscaling is on.** The chart omits
  the field entirely so the HPA owns it. If you leave it in, Argo CD's self-heal
  and the HPA fight each other forever and the pod count flaps. This is the most
  common GitOps + HPA bug; `ignoreDifferences` on `/spec/replicas` in the
  Application is the belt to that braces.
- **No CPU limit by default.** With a CPU limit the container gets throttled
  instead of showing high utilisation, which makes HPA experiments read
  strangely. Add one when you want to see throttling.
- **Small CPU requests (50m).** The HPA target is a percentage *of the request*,
  so a small request means one `/burn` call is enough to trigger scale-up.
- **Config checksums on the pod template.** Editing a ConfigMap in Kubernetes
  does *not* restart pods by itself; the `checksum/env` annotation is what makes
  a config change roll the Deployment.
- **`automountServiceAccountToken: false`** — the app never calls the API
  server, so it does not get a token. Flip it on when you start learning RBAC or
  Workload Identity.
- **The chart ships an HTTPRoute but no Gateway.** A Gateway owns a public IP
  and is cluster infrastructure; a route is application config. Bundling the
  Gateway into an app chart is how you end up with six load balancers. Point
  `httpRoute.parentRefs` at the one your cluster already has. Enable either
  `ingress` or `httpRoute`, not both.
