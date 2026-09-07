# Guided labs

Each lab is self-contained and takes 5–15 minutes. Everything assumes:

```powershell
$NS = "lab-dev"
kubectl -n $NS port-forward svc/lab-app 8080:80   # leave this running in one terminal
```

Keep a second terminal on the logs and a third on the pods:

```powershell
kubectl -n $NS logs -l app.kubernetes.io/name=lab-app -f --prefix --tail=20
kubectl -n $NS get pods -w
```

---

## Lab 1 — Manual scaling and how a Service load-balances

1. Set `replicaCount: 3` in `charts/lab-app/values-dev.yaml`, commit, push.
2. Watch Argo CD notice it (up to 3 minutes on the default poll, or press
   **Refresh** in the UI / `argocd app get lab-app-dev --refresh`).
3. Hit the app repeatedly and watch the pod name change:
   ```powershell
   1..20 | ForEach-Object { (curl -s localhost:8080/info | ConvertFrom-Json).pod }
   ```
   *Note:* a `port-forward` targets **one** pod, so you will see a single name.
   Run `kubectl -n $NS run curl --rm -it --image=curlimages/curl -- sh` and curl
   `http://lab-app/info` from inside the cluster to see real balancing.
4. Now try `kubectl -n $NS scale deploy/lab-app --replicas=1`. With
   `selfHeal: true`, Argo CD puts it back within seconds. That is the whole
   point of GitOps — the cluster is not the source of truth.

**Takeaway:** manual `kubectl scale` is a debugging tool, not a deployment
mechanism. And a Service load-balances per *connection*, not per request.

---

## Lab 2 — Horizontal Pod Autoscaler

Autoscaling is already enabled in `values-dev.yaml` (2–8 pods, 60% CPU).

```powershell
kubectl -n $NS get hpa lab-app -w      # terminal A
curl "http://localhost:8080/burn?seconds=240&workers=2"
```

Watch `TARGETS` climb past 60% and `REPLICAS` follow. Then:

```powershell
kubectl -n $NS describe hpa lab-app    # read the Events at the bottom
```

Things to try:

- **Why does scale-down take a minute?** `behavior.scaleDown.stabilizationWindowSeconds`
  in `values.yaml` is 60s. Kubernetes defaults to 300s to avoid thrashing. Change
  it and observe.
- **Why did it only scale to 8?** `maxReplicas`. Raise it and burn again.
- **Break it on purpose:** delete the `resources.requests.cpu` value. The HPA has
  nothing to compute a percentage against and reports `<unknown>`. This is the
  #1 reason a CPU HPA silently does nothing.
- **Memory target:** set `autoscaling.targetMemoryUtilizationPercentage: 70` and
  drive it with `/mem?mb=200&seconds=300`. Notice memory scaling is a worse
  signal — memory does not fall when load falls, so it scales up and stays up.
- **Scale below min:** the HPA will not let you go under `minReplicas`.

**Takeaway:** the HPA target is a percentage of the *request*, not of the node
or the limit. Requests are the unit of autoscaling.

---

## Lab 3 — Sidecar containers

Two patterns, both in the chart:

```powershell
helm upgrade --install lab-app charts/lab-app -n $NS `
  -f charts/lab-app/values.yaml -f charts/lab-app/values-dev.yaml `
  -f charts/lab-app/values-sidecar.yaml
```

(or add `values-sidecar.yaml` to `valueFiles` in the Argo CD Application.)

```powershell
kubectl -n $NS get pods                  # READY now shows 3/3
kubectl -n $NS logs <pod> -c app         # the application
kubectl -n $NS logs <pod> -c log-tailer  # same lines, prefixed [tailer]
kubectl -n $NS logs <pod> -c heartbeat   # the native sidecar
kubectl -n $NS describe pod <pod>        # heartbeat is under Init Containers
```

What is going on:

- **`log-tailer`** is the *classic* streaming sidecar: a normal entry under
  `spec.containers`. The app writes to `/var/log/app/app.log` on a shared
  `emptyDir`; the sidecar tails that file to its own stdout. Every container in a
  pod shares the network namespace and any volumes you mount into both — that is
  the entire mechanism behind sidecars.
- **`heartbeat`** is a *native* sidecar (Kubernetes 1.29+): an `initContainer`
  with `restartPolicy: Always`. Look at the ordering — it is running *before*
  the app container starts, and it is terminated *after* the app exits. That
  ordering guarantee is why log shippers and service-mesh proxies moved to this
  shape; a classic sidecar can start too late or die too early.

Things to try:

- Kill just the sidecar: `kubectl -n $NS exec <pod> -c log-tailer -- kill 1`.
  The pod is not recreated — only that container restarts, and `RESTARTS` counts
  it. Pods are the scheduling unit; containers restart independently.
- Watch the shared network namespace: exec into `log-tailer` and
  `wget -qO- localhost:8080/info` — `localhost` reaches the app container.
- Add your own via `extraContainers` in `values-sidecar.yaml`.

**Takeaway:** a sidecar is just another container in the same pod, sharing
network and volumes. Prefer native sidecars for anything that must outlive or
predate the main container.

---

## Lab 4 — Probes: readiness vs liveness

They are constantly confused. Prove the difference to yourself.

**Readiness** — removes the pod from the Service, does not restart it:

```powershell
curl "http://localhost:8080/toggle?what=ready&value=false"
kubectl -n $NS get pods            # READY 0/1, RESTARTS unchanged
kubectl -n $NS get endpointslices -l kubernetes.io/service-name=lab-app -o yaml
curl "http://localhost:8080/toggle?what=ready&value=true"
```

**Liveness** — kills and restarts the container:

```powershell
curl "http://localhost:8080/toggle?what=health&value=false"
kubectl -n $NS get pods -w         # RESTARTS goes to 1 after ~30s
kubectl -n $NS describe pod <pod>  # Events: "Liveness probe failed", "Killing"
```

**Crash loop** — see the exponential backoff:

```powershell
curl http://localhost:8080/crash
kubectl -n $NS get pods -w         # Error -> CrashLoopBackOff, 10s, 20s, 40s...
```

**Startup probe** — set `app.startupDelaySeconds: 45` while
`probes.startup.failureThreshold: 30` (60s budget) and redeploy: the pod starts
fine. Now set `probes.startup.enabled: false` and redeploy: the liveness probe
kills it before it ever finishes booting. That is what a startup probe is for.

**Takeaway:** readiness controls *traffic*, liveness controls *restarts*, startup
buys *boot time*. A liveness probe that is too aggressive turns a slow app into a
crash loop.

---

## Lab 5 — Rolling updates and graceful shutdown

```powershell
# terminal A - continuous requests from inside the cluster
kubectl -n $NS run load --rm -it --image=curlimages/curl -- `
  sh -c 'while true; do curl -s -o /dev/null -w "%{http_code} " http://lab-app/info; sleep 0.2; done'
```

Change `app.color` from `blue` to `green` in `values-dev.yaml`, commit, push, and
watch. You should see zero non-200s, thanks to three settings working together:

- `strategy.rollingUpdate.maxUnavailable: 0` — never drop below the current count
- `lifecycle.preStopSleepSeconds: 5` — stop receiving new connections before shutdown
- `app.shutdownDelaySeconds: 5` < `terminationGracePeriodSeconds: 30` — finish in-flight work

Now break it deliberately: set `lifecycle.preStopSleepSeconds: 0` and
`app.shutdownDelaySeconds: 0`, redeploy, and roll again. Connection errors
appear, because pods stop serving before kube-proxy has finished removing them
from every node's rules.

Other things to try:

```powershell
kubectl -n $NS rollout status deploy/lab-app
kubectl -n $NS rollout history deploy/lab-app
kubectl -n $NS rollout undo deploy/lab-app     # Argo CD will re-sync it back!
```

That last one is worth doing: under GitOps, rollback means `git revert`, not
`kubectl rollout undo`.

**Takeaway:** zero-downtime deploys are not the default. They come from
`maxUnavailable: 0` plus a `preStop` delay plus an app that handles SIGTERM.

---

## Lab 6 — Logs

```powershell
curl "http://localhost:8080/lograte?rps=200"       # 200 lines/sec/pod
kubectl -n $NS logs -l app.kubernetes.io/name=lab-app --tail=5 --prefix
```

- Logs go to stdout; the kubelet writes them to the node's disk and rotates at
  10Mi per container by default. `kubectl logs` reads those files, which is why
  `kubectl logs` cannot show you anything from before the last rotation. Use
  `--previous` to read the *previous* container instance after a restart.
- Switch `app.logFormat: text` and redeploy. Container Insights and Loki can
  parse the JSON form into fields; the text form has to be regex-parsed. Feel the
  difference.
- If Container Insights is enabled on the cluster, query it:
  ```kusto
  ContainerLogV2
  | where PodNamespace == "lab-dev"
  | where LogMessage.level == "ERROR"
  | project TimeGenerated, PodName, LogMessage.msg
  | take 50
  ```
- Turn the rate back down with `/lograte?rps=2` — log ingestion is billed per GB.

**Takeaway:** stdout is the contract. Structured logs cost nothing extra to
produce and save real money and time downstream.

---

## Lab 7 — Disruption, node drains and PDBs

```powershell
helm upgrade lab-app charts/lab-app -n $NS `
  -f charts/lab-app/values.yaml -f charts/lab-app/values-prod.yaml
kubectl -n $NS get pdb
kubectl get nodes
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
```

With `minAvailable: 2` and 3 replicas the drain proceeds one pod at a time. Set
`minAvailable` equal to the replica count and try again — the drain blocks
forever. That exact misconfiguration stalls real AKS node pool upgrades.

```powershell
kubectl uncordon <node>
```

**Takeaway:** a PDB constrains only *voluntary* disruption (drains, upgrades,
cluster-autoscaler scale-in). It never protects against a crash or node failure,
and set too tight it stops you upgrading at all.

---

## Lab 8 — Argo CD behaviours worth internalising

- **Drift correction:** `kubectl -n $NS set env deploy/lab-app FOO=bar`, then
  watch `selfHeal` revert it.
- **Prune:** set `podDisruptionBudget.enabled: false`, push — Argo CD deletes the
  PDB because `prune: true`. Without prune, removed resources linger forever.
- **Diff before sync:** `argocd app diff lab-app-prod` on the manual-sync app.
- **Sync waves:** add `argocd.argoproj.io/sync-wave: "-1"` as an annotation on
  the ConfigMaps and watch the order in the UI.
- **App of apps / ApplicationSet:** `kubectl apply -f argocd/applicationset.yaml`
  generates both environments from one object (delete the individual
  Applications first).

**Takeaway:** Argo CD's job is to make the cluster match git, continuously. Every
manual change you make is a change it will undo.

---

## Lab 9 — Exposing the app with a Gateway API HTTPRoute

Ingress is frozen; HTTPRoute is what replaces it. The split matters: a
**Gateway** is infrastructure (it owns a public IP, a platform team runs it),
an **HTTPRoute** is application config (you own it, it attaches to their
Gateway). This chart ships only the route — which is the correct division.

First find out what you are attaching to:

```powershell
kubectl get gatewayclass          # is a controller installed at all?
kubectl get gateway -A            # PROGRAMMED=True and an ADDRESS = usable
```

The route is enabled in `values.yaml` by default — deliberately, so an Argo CD
Application created through the UI (which loads `values.yaml` and nothing else)
deploys it with no extra configuration. Set `httpRoute.parentRefs[0].name` and
`httpRoute.hostnames` there to match your cluster, then:

```powershell
helm upgrade --install labapp charts/lab-app -n default -f charts/lab-app/values.yaml
```

which renders exactly this:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: labapp
  namespace: default
spec:
  hostnames:
    - labapp.websolutsg.co.uk
  parentRefs:
    - name: platform-gateway
  rules:
    - backendRefs:
        - name: labapp
          port: 80
```

**The first thing to check is always attachment.** An HTTPRoute no Gateway
accepted is silently inert — no error, no events on your side, just nothing:

```powershell
kubectl -n default describe httproute labapp
# Parents -> Conditions: Accepted=True, ResolvedRefs=True is what you want
```

`Accepted=False` almost always means one of:

- the Gateway's listener has `allowedRoutes.namespaces.from: Same` and your
  route is in a different namespace — attachment is a **two-way handshake**,
  unlike Ingress, and the Gateway owner has to opt you in
- your `hostnames` do not intersect the listener's `hostname`
- `sectionName` names a listener that does not exist

`ResolvedRefs=False` means the backend Service name or port is wrong.

Then try the things Ingress needed vendor-specific annotations for:

- **Timeouts** — set `httpRoute.timeouts.request: 3s` and hit `/slow?ms=5000`.
  You get a 504 from the gateway, no annotations involved.
- **Header filters** — uncomment `httpRoute.filters`; the app echoes what it
  received on `/env`.
- **Traffic splitting** — deploy a second release and split traffic by weight:
  ```powershell
  # the canary release serves traffic but publishes no route of its own
  helm upgrade --install labapp-canary charts/lab-app -n default `
    -f charts/lab-app/values.yaml --set app.color=green --set httpRoute.enabled=false
  helm upgrade labapp charts/lab-app -n default -f charts/lab-app/values.yaml `
    --set httpRoute.canary.enabled=true --set httpRoute.canary.serviceName=labapp-canary
  # then watch the "color" field shift ~20% of the time:
  1..30 | ForEach-Object { (curl -s http://labapp.websolutsg.co.uk/info | ConvertFrom-Json).color }
  ```
  Weights are *relative*, not percentages — `80`/`20` and `8`/`2` behave
  identically. They only look like percentages because they sum to 100.
- **Header-based routing** — put a full rule list in `httpRoute.rules` (there is
  a commented example in `values.yaml`); it replaces the generated rule
  entirely. Rules are evaluated most-specific-first, not in file order.

**Takeaway:** HTTPRoute moves routing from annotations into a typed, portable
API, and splits ownership between the Gateway (infrastructure) and the route
(your app). When nothing happens, check `Accepted` before you check anything
else.

---

## Cleanup

```powershell
kubectl delete -f argocd/application-dev.yaml
kubectl delete -f argocd/application-prod.yaml
kubectl delete ns lab-dev lab-prod --ignore-not-found
```
