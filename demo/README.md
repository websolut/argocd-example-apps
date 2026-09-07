# demo-app

A small Python app and Helm chart for **learning AKS storage and autoscaling**,
deployed with Argo CD.

It is a sibling of [../lab-app](../lab-app), which covers probes, sidecars and
rolling updates. This one covers the two things that chart leaves out:
**PersistentVolumes** and the **HPA**, and specifically how they interact -
because the interesting failures live exactly there.

The app is ~740 lines of Python standard library mounted from a ConfigMap onto
the stock `python:3.12-alpine` image, so there is **nothing to build and no
container registry to set up**.

## The idea

Storage is hard to learn from documentation because the failures are invisible
from inside the app - a volume that is not persisting looks exactly like one
that is, until the pod restarts. So this app reports on its own volume:

```console
$ curl $URL/boots
{
  "pod": "demo-app-0",
  "storage_mode": "perPod",
  "boots_recorded": 4,
  "distinct_pods_seen": ["demo-app-0"],
  "reading": "1 boot = nothing persisted; several pods = a shared (RWX) volume; ..."
}
```

`boots_recorded` is one integer that tells you whether your storage works. It
counts process starts recorded *on the volume*. Stuck at 1 no matter how often
you restart? Nothing is persisting. Climbing? It is. Listing several pod names?
Several replicas are writing to one share, so ReadWriteMany is really working.

## Three storage modes, one app

The container never changes. Only the volume behind `/data` does, and the whole
object graph follows from that:

| `storage.mode` | Volume | Controller | AKS class | Survives container crash | Survives pod replacement | Shared between replicas |
|---|---|---|---|---|---|---|
| `none` | `emptyDir` | Deployment | - | yes | **no** | no |
| `shared` | one PVC, RWX | Deployment | `azurefile-csi` | yes | yes | **yes** |
| `perPod` | one PVC per pod, RWO | StatefulSet | `managed-csi` | yes | yes | no |

Those middle two columns are separate on purpose. An `emptyDir` is scoped to the
pod, so a crashing container keeps its data and a rollout, drain, eviction or
scale-down does not - two events that both get called "a restart" in
conversation. Lab 1 walks through the difference.

Switch with an overlay:

```powershell
helm upgrade --install demo-app demo -f demo/values.yaml -f demo/values-shared.yaml -n demo-dev
```

There is also [values-rwo-trap.yaml](values-rwo-trap.yaml), which is
**deliberately broken** - one ReadWriteOnce disk behind three replicas - so you
can see a Multi-Attach error for yourself and recognise it later.

## Endpoints

| Endpoint | What it does | What you learn from it |
|---|---|---|
| `/` `/info` | pod, node, uptime, and a full storage report | which replica answered |
| `/df` | capacity, used, free, filesystem type | `ext4` vs `cifs` vs `overlay` |
| `/boots` | process starts recorded on the volume | whether persistence works at all |
| `/ls` | walk the volume | whether replicas share bytes |
| `/write?mb=64&fsync=1` | timed, fsynced write | Disk vs Files throughput and latency |
| `/read?name=x` | read a file back | it really is the same volume |
| `/fill?percent=95` | fill until `ENOSPC` | a full PVC, and why the HPA cannot help |
| `/rm?name=fill` | delete a file or directory | cleanup |
| `/mounts` | the entry from `/proc/mounts` | the mount options actually in force |
| `/burn?seconds=120&workers=1` | burns CPU | HPA scale-up on CPU |
| `/mem?mb=200&seconds=120` | holds memory | HPA on memory, `OOMKilled` |
| `/healthz` `/readyz` | probe endpoints | liveness restarts, readiness routes |
| `/toggle?what=ready&value=false` | fail readiness | pod leaves the Service endpoints |
| `/crash` | `exit(1)` | restarts, and what the volume kept |
| `/slow?ms=3000` | slow response | probe and HTTPRoute timeouts |
| `/error?code=503` | logs an error, returns it | log filtering, alerting |
| `/lograte?rps=100` | changes log volume | log pipeline cost |
| `/metrics` | Prometheus metrics, including volume gauges | disk-full alerting |
| `/env` | non-secret env vars | ConfigMaps, Downward API |

On `SIGTERM` it fails readiness, keeps serving for `shutdownDelaySeconds`, then
exits - so graceful shutdown is visible in `kubectl logs` during a rollout, and
a disk gets a chance to detach cleanly.

## Layout

```
demo/
  Chart.yaml
  values.yaml               documented defaults - read the storage block first
  values-ephemeral.yaml     overlay: emptyDir, nothing persists
  values-shared.yaml        overlay: one Azure Files share, RWX, all replicas
  values-perpod.yaml        overlay: StatefulSet, one Azure Disk per pod
  values-rwo-trap.yaml      overlay: deliberately broken, for lab 4
  files/app.py              the application
  templates/
    _helpers.tpl            names, labels, and the storage-mode validation
    _pod.tpl                the pod template, shared by both controllers
    deployment.yaml         used for mode none + shared
    statefulset.yaml        used for mode perPod (volumeClaimTemplates)
    pvc.yaml                the single shared claim, mode shared only
    storageclass.yaml       optional, for the expansion lab
    service.yaml            the normal Service
    service-headless.yaml   per-pod DNS, StatefulSet only
    httproute.yaml          Gateway API route - the way in, no Ingress here
    hpa.yaml, pdb.yaml, servicemonitor.yaml
    configmap-app.yaml      app.py, embedded
    configmap-env.yaml      runtime config
  argocd/
    application-dev.yaml    auto-sync + self-heal, shared storage
    application-perpod.yaml manual sync, StatefulSet storage
    applicationset.yaml     optional: all three modes at once, for comparison
  docs/LABS.md              nine guided exercises - start here
  scripts/load.ps1          load generator (PowerShell)
  scripts/load.sh           load generator (POSIX sh, also runs in-cluster)
```

## Quick start

### 1. Deploy it

```powershell
kubectl apply -f demo/argocd/application-dev.yaml
kubectl -n argocd get application demo-app-dev -w
```

Or with Helm directly, while you are still editing the chart:

```powershell
helm upgrade --install demo-app demo `
  -f demo/values.yaml -f demo/values-shared.yaml `
  -n demo-dev --create-namespace
```

### 2. Check the claim bound, before anything else

```powershell
kubectl -n demo-dev get pvc
```

A `Pending` PVC means the pods will sit in `ContainerCreating` and nothing else
you try will make sense. `kubectl describe pvc` and read the Events.

### 3. Look at it

```powershell
kubectl -n demo-dev get pods,svc,hpa,pvc
kubectl -n demo-dev port-forward svc/demo-app 8080:80
curl http://localhost:8080/info
curl http://localhost:8080/boots
```

### 4. Work through the labs

[docs/LABS.md](docs/LABS.md) - persistence, RWX sharing, the Multi-Attach trap,
StatefulSet identity, scale-down retention, filling a volume, expanding one, and
the HPA on top of all of it.

## Getting in: Gateway API, not Ingress

There is no `ingress.yaml`. The chart publishes a
[Gateway API](https://gateway-api.sigs.k8s.io/) `HTTPRoute` and expects a Gateway
to already exist - because a Gateway owns a public IP and a certificate, which
makes it platform infrastructure rather than app config. One shared Gateway,
many app routes.

```yaml
httpRoute:
  enabled: true
  parentRefs:
    - name: platform-gateway
      namespace: default        # where the Gateway lives, NOT where the app lives
  hostnames:
    - demoapp.websolutsg.co.uk  # must intersect the listener hostname
```

Unlike Ingress, attachment is a **two-way handshake** and either side can
refuse. Both failures are silent - the route is created, Argo CD goes green, and
requests 404:

1. **The listener must allow this namespace.** The Gateway API default is
   `allowedRoutes.namespaces.from: Same`, and this app does not live in the
   Gateway's namespace. The Gateway needs `from: All` (or a selector matching
   `demo-dev`).
2. **The hostnames must intersect.** A listener with
   `hostname: argocd.websolutsg.co.uk` will not accept a route for
   `demoapp.websolutsg.co.uk`. Add a listener per hostname, or use a wildcard.

The verdict is only visible on the route:

```powershell
kubectl -n demo-dev describe httproute demo-app   # Parents -> Conditions
```

`Accepted=False` with `NotAllowedByListeners` is problem 1 or 2.
`Accepted=True` but `ResolvedRefs=False` is a backend Service name typo.

### What the Gateway needs to have

The chart never creates a Gateway, so the one you already run has to accept the
route. Two things on its side:

```yaml
listeners:
  - name: http
    port: 80
    protocol: HTTP
    allowedRoutes:
      namespaces:
        from: All              # <- or this route is rejected
  - name: https
    port: 443
    protocol: HTTPS
    hostname: demoapp.websolutsg.co.uk   # <- or use a *.websolutsg.co.uk wildcard
    tls:
      mode: Terminate
      certificateRefs:
        - name: demoapp-tls
    allowedRoutes:
      namespaces:
        from: All
```

Leaving port 80 open is deliberate, not sloppiness: the ACME HTTP-01 challenge
is plain HTTP and has to keep working for **renewals**, not just first issuance.
Closing it is how certificates silently stop renewing 60 days later.

Each overlay uses a different hostname (`demoapp`, `demoapp-ephemeral`,
`demoapp-perpod`), so either add a listener per hostname or give the Gateway one
wildcard listener. If you would rather not touch the Gateway at all, set
`httpRoute.enabled: false` - every lab works over `port-forward` too.

## Prerequisites

- An AKS cluster (a single `Standard_B2s`-class node pool is enough for
  everything except lab 3 and lab 4, which need **two or more nodes** to show
  what they are about) and `kubectl` pointing at it
- `helm` 3.8+ to render or install locally
- Argo CD in the cluster:
  ```powershell
  kubectl create namespace argocd
  kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
  ```
- `metrics-server` for the HPA - AKS ships it. Verify with `kubectl top pods`;
  if that fails, the HPA reports `<unknown>` and never scales.
- The CSI drivers, which AKS enables by default. Confirm the classes exist:
  ```powershell
  kubectl get storageclass
  ```
  You want `azurefile-csi` and `managed-csi` in that list.
- An existing Gateway to attach the HTTPRoute to, with a listener that accepts
  this namespace and hostname:
  ```powershell
  kubectl get gateway -A
  kubectl get gateway platform-gateway -n default -o yaml
  ```
  Set `httpRoute.enabled: false` if you only want to reach the app by
  `port-forward` - every lab works that way too.

Neither `helm` nor `kubectl` may be on this machine - `winget install
Kubernetes.kubectl Helm.Helm` (or `az aks install-cli`) gets you both.

## Running the app without Kubernetes

It is a single stdlib file, so it runs anywhere:

```powershell
$env:DATA_DIR = "$env:TEMP\demo-data"; $env:PORT = "8080"; $env:LOG_RATE = "0"
python demo\files\app.py
```

Everything works except `/mounts`, which reads `/proc/mounts` and therefore only
has anything to say on Linux. Useful for checking a change to the app before
pushing it and waiting on a sync.

## Deliberate design choices worth knowing

- **The chart omits `replicas` entirely when autoscaling is on.** If it did not,
  Argo CD self-heal and the HPA would fight each other forever and the pod count
  would flap. This is the most common GitOps + HPA bug;
  `ignoreDifferences` on `/spec/replicas` is the belt to that braces.
- **`storage.mode` picks the controller, not just the volume.** `perPod` renders
  a StatefulSet because `volumeClaimTemplates` is the only mechanism in
  Kubernetes that creates one PVC per replica. Helm cannot turn a Deployment
  into a StatefulSet in place, so switching modes on a live release needs a
  `helm uninstall` first. The PVCs survive that deliberately.
- **The pod template is defined once**, in `_pod.tpl`, and included by both
  controllers. The container should not know what kind of volume it got.
- **The chart ships an HTTPRoute and never a Gateway.** A Gateway owns a public
  IP and a certificate and is cluster infrastructure; a route is application
  config. Bundling a Gateway into an app chart is how you end up with six load
  balancers. Each overlay also uses its own hostname, so the three storage modes
  can run side by side without their routes fighting over one hostname on the
  shared Gateway.
- **`_helpers.tpl` fails the render** on `perPod` + `ReadWriteMany`, because that
  combination is always a mistake. It does *not* block `shared` + `ReadWriteOnce`
  - that one is a valid experiment, and it is lab 4.
- **PVCs are annotated `helm.sh/resource-policy: keep` and
  `argocd.argoproj.io/sync-options: Prune=false`.** Deleting an Application
  should not silently destroy data. The flip side is that they outlive the
  release and keep costing money - the cleanup section of LABS.md matters.
- **No CPU limit by default.** With a CPU limit the container gets throttled
  instead of showing high utilisation, which makes HPA experiments read
  strangely. Add one when you want to see throttling.
- **Small CPU requests (10m).** The HPA target is a percentage *of the request*,
  so a small request means one `/burn` call is enough to trigger scale-up.
- **`fsGroup: 1000` is set, and it is not enough for Azure Files.** The kubelet
  chowns an `ext4` volume to match `fsGroup`, so a non-root container can write
  to an Azure Disk. `cifs` ignores it - a File share needs `uid`/`gid` in the
  StorageClass `mountOptions` instead. `storageClass.create` in values.yaml
  ships a class that sets them. If `/info` reports `"writable": false`, this is
  almost always why.
- **`ServerSideApply=true` in the Applications.** `app.py` is embedded in a
  ConfigMap, and client-side apply stores a copy in the
  `last-applied-configuration` annotation, which has a 256KB limit. Server-side
  apply avoids the whole problem.
- **`automountServiceAccountToken: false`** - the app never calls the API
  server, so it does not get a token.
