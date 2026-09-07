# demo-app labs

Nine exercises. Each one has a thing to do, a thing to watch, and the point of
having done it. They build on each other, so go in order the first time.

Throughout, `$URL` is however you are reaching the app. The simplest option:

```powershell
kubectl -n demo-dev port-forward svc/demo-app 8080:80
# then, in another terminal
$URL = "http://localhost:8080"
```

A port-forward always targets **one** pod, which matters from lab 3 onwards. To
spread requests across replicas the way a Service does, drive it from inside the
cluster instead:

```powershell
kubectl -n demo-dev run curl --rm -it --image=curlimages/curl --restart=Never -- sh
# then from that shell: curl http://demo-app/info
```

Two terminals is the right setup for all of these: one running a `-w` watch, one
issuing the curl that makes something happen.

---

## Lab 1 - Nothing persists (storage.mode = none)

Deploy the ephemeral overlay and prove to yourself what you are missing.

```powershell
helm upgrade --install demo-app demo `
  -f demo/values.yaml -f demo/values-ephemeral.yaml `
  -n demo-dev --create-namespace
```

Write something, then look at the baseline:

```powershell
curl "$URL/write?mb=16"      # note duration_ms and throughput_mb_s
curl "$URL/ls"               # your file is there
curl "$URL/boots"            # boots_recorded: 1
```

Now kill the **process**, which restarts the container inside the same pod:

```powershell
curl "$URL/crash"
kubectl -n demo-dev get pods       # RESTARTS went up by 1, pod name unchanged
# re-establish the port-forward, then:
curl "$URL/ls"                     # your file is STILL THERE
curl "$URL/boots"                  # boots_recorded: 2
```

That is not the failure you were expecting. Now delete the **pod**:

```powershell
kubectl -n demo-dev delete pod -l app.kubernetes.io/name=demo-app
kubectl -n demo-dev get pods -w    # a new pod, with a new name
# re-establish the port-forward, then:
curl "$URL/ls"                     # empty
curl "$URL/boots"                  # back to 1
```

**The point.** An `emptyDir` is scoped to the **pod**, not the container, so a
crash-and-restart keeps the data and a pod replacement destroys it. Those are
two different events that both get called "a restart" in conversation, and
telling them apart is most of the skill here. A `Deployment` rollout, a node
drain, an eviction and a scale-down all replace pods - so in practice, on a real
cluster, `emptyDir` data is gone constantly.

Also worth a look: `curl "$URL/df"` reports the size of the **node** disk, not
the 512Mi you set. `emptyDir.sizeLimit` is a quota that gets your pod evicted
when you cross it, not a filesystem boundary the app can see. That one surprises
people, and it means a runaway `/fill` on `emptyDir` fills the node, not a
volume - which is why `mode: none` is the one mode where you should not run
lab 7.

---

## Lab 2 - A real volume that survives (storage.mode = shared)

```powershell
helm upgrade --install demo-app demo `
  -f demo/values.yaml -f demo/values-shared.yaml `
  -n demo-dev
```

Check the claim **before** you check the pods:

```powershell
kubectl -n demo-dev get pvc
kubectl -n demo-dev get pv
kubectl -n demo-dev describe pvc demo-app-data
```

You want `STATUS: Bound`. If it says `Pending`, read the Events at the bottom of
`describe` - that message is the answer, and 90% of the time it is either "no
such StorageClass" or an access mode the class cannot provide.

Now repeat the part of lab 1 that actually destroyed data - deleting the pod,
not just crashing the process:

```powershell
curl "$URL/df"               # fstype should be cifs - this is SMB, not a disk
curl "$URL/write?mb=16"
kubectl -n demo-dev delete pod -l app.kubernetes.io/name=demo-app
# once the replacement is Ready and the port-forward is back:
curl "$URL/ls"               # your file survived
curl "$URL/boots"            # boots_recorded: 2, on a brand new pod
```

**The point.** `boots_recorded: 2` is the whole lesson in one integer. A pod
that never existed when you wrote that file can read it back - the volume
outlived the pod, which is the one thing `emptyDir` could not do.

Compare `duration_ms` from `/write` here against lab 1 while you are at it.
Network storage is slower, and now you know by how much on your cluster rather
than in general.

---

## Lab 3 - One volume, many readers (RWX)

Still on `values-shared.yaml`. Scale up and ask every replica the same question.

```powershell
kubectl -n demo-dev scale deploy/demo-app --replicas=4
kubectl -n demo-dev get pods -o wide      # note the NODE column
```

The pods should be on different nodes (that is what `podAntiAffinity` is for).
Now, from inside the cluster so the Service load-balances:

```powershell
kubectl -n demo-dev run curl --rm -it --image=curlimages/curl --restart=Never -- `
  sh -c 'for i in 1 2 3 4 5 6 7 8; do curl -s http://demo-app/boots | grep -E "pod|boots_recorded"; done'
```

**The point.** Every replica reports the same `boots_recorded`, and
`distinct_pods_seen` lists all four pod names - from four pods, on different
nodes, writing to one Azure Files share simultaneously. That is what
ReadWriteMany buys you, and it is the only storage mode a `Deployment` with an
HPA can use without breaking.

Try `curl "$URL/write?mb=8&name=data/shared.bin"` against one pod and `/ls` on
another: the file appears everywhere.

---

## Lab 4 - The Multi-Attach trap (do this on purpose)

The most common AKS storage bug, deliberately reproduced.

```powershell
helm upgrade --install demo-app-trap demo `
  -f demo/values.yaml -f demo/values-rwo-trap.yaml `
  -n demo-trap --create-namespace
kubectl -n demo-trap get pods -w
```

One pod goes `Running`. The other two sit in `ContainerCreating` and stay there.

```powershell
kubectl -n demo-trap describe pod -l app.kubernetes.io/name=demo-app | Select-String -Pattern "Multi-Attach" -Context 0,4
kubectl -n demo-trap get events --sort-by=.lastTimestamp | Select-Object -Last 15
```

You are looking for:

```
Multi-Attach error for volume "pvc-..."
Volume is already exclusively attached to one node and cannot be attached to another
```

**The point.** `ReadWriteOnce` constrains the **node**, not the pod. Two pods on
the *same* node share an RWO volume perfectly well - which is exactly why this
bug hides on a single-node dev cluster and appears the day the cluster grows or
a node gets drained. The `podAntiAffinity: required` in that overlay is there to
guarantee the failure instead of leaving it to the scheduler.

Note too that nothing is "broken" from the autoscaler's point of view: the
Deployment scaled to 3, the HPA is satisfied, `kubectl get deploy` shows 1/3
ready. The controller did its job; the storage could not follow.

Clean up: `helm uninstall demo-app-trap -n demo-trap`

---

## Lab 5 - One volume per pod (StatefulSet)

```powershell
helm uninstall demo-app -n demo-dev      # Deployment -> StatefulSet needs a delete
helm upgrade --install demo-app demo `
  -f demo/values.yaml -f demo/values-perpod.yaml `
  -n demo-dev
kubectl -n demo-dev get pods,pvc -w
```

Watch the ordering: pod `demo-app-0` is created, its claim `data-demo-app-0` is
provisioned and attached, it goes Ready, and only *then* does `demo-app-1`
start. That is `podManagementPolicy: OrderedReady`.

Now address one specific replica through the headless Service:

```powershell
kubectl -n demo-dev run curl --rm -it --image=curlimages/curl --restart=Never -- `
  sh -c 'curl -s http://demo-app-0.demo-app-headless/boots; curl -s http://demo-app-1.demo-app-headless/boots'
```

**The point.** Each pod reports `distinct_pods_seen: 1` - its own name. Two pods,
two disks, two independent histories. Write a file on pod 0 and it will *not*
appear on pod 1. That is the correct shape for anything that owns its data
(a database), and the wrong shape for anything that shares it.

Then delete a pod and watch the identity hold:

```powershell
kubectl -n demo-dev delete pod demo-app-0
kubectl -n demo-dev get pods -w
# once it is back:
curl "$URL/boots"     # boots_recorded went up; distinct_pods_seen is still just demo-app-0
```

The replacement pod has the same name, the same DNS record and the same disk.
Compare that to a Deployment, where the replacement gets a new random name and
none of its predecessor's state.

---

## Lab 6 - Scale-down keeps the disks

Still on the StatefulSet.

```powershell
kubectl -n demo-dev scale statefulset/demo-app --replicas=3
kubectl -n demo-dev get pvc           # three claims
kubectl -n demo-dev scale statefulset/demo-app --replicas=1
kubectl -n demo-dev get pods          # one pod
kubectl -n demo-dev get pvc           # STILL THREE CLAIMS
kubectl -n demo-dev scale statefulset/demo-app --replicas=3
curl "$URL/boots"                     # pod 2 remembers its old boots
```

**The point.** `persistentVolumeClaimRetentionPolicy.whenScaled: Retain` is why
scaling a StatefulSet in does not destroy data - and also why it does not stop
the bill. Those two Azure Disks kept existing, and kept being charged for, the
whole time you were at one replica.

Set `whenScaled: Delete` in `values-perpod.yaml`, re-run the cycle, and watch
the claims disappear on scale-in. Both settings are defensible. Only one of them
is what people assume.

---

## Lab 7 - Fill it up

A full volume is a different failure from a busy one, and no autoscaler fixes it.

```powershell
# terminal 1
kubectl -n demo-dev exec -it demo-app-0 -- sh -c 'while true; do df -h /data; sleep 3; done'
# terminal 2
curl "$URL/df"
curl "$URL/fill?percent=95"
```

Watch `df` climb, then check the logs for the failure:

```powershell
kubectl -n demo-dev logs demo-app-0 | Select-String -Pattern "volume full","write failed"
curl "$URL/metrics" | Select-String -Pattern "demo_app_write_errors_total|demo_app_volume_used_bytes"
```

Then prove that scaling does not help:

```powershell
kubectl -n demo-dev scale statefulset/demo-app --replicas=4
curl "$URL/write?mb=64"     # still fails on pod 0. New pods have new empty disks.
```

Clean up with `curl "$URL/rm?name=fill"`.

**The point.** `ENOSPC` is not a capacity problem you solve with replicas. The
HPA has no idea the volume is full - CPU is fine, memory is fine, the pod is
Ready, and the app cannot do its job. This is why
`demo_app_volume_used_bytes / demo_app_volume_bytes_total` belongs on a
dashboard with an alert, and why `serviceMonitor.enabled` exists in this chart.

---

## Lab 8 - Expand a volume in place

Only possible when the StorageClass has `allowVolumeExpansion: true`. Check:

```powershell
kubectl get sc
kubectl get sc managed-csi -o jsonpath='{.allowVolumeExpansion}'
```

For the **shared** mode (a plain PVC), expansion is a git edit - which is the
whole promise of GitOps:

```powershell
# edit demo/values-shared.yaml: storage.size 5Gi -> 10Gi, commit, push
argocd app sync demo-app-dev
kubectl -n demo-dev get pvc -w        # watch capacity change with no pod restart
curl "$URL/df"                        # the app sees the new size immediately
```

For the **per-pod** mode it is not, because `volumeClaimTemplates` is immutable.
Try the git edit first so you see the error, then do it properly - the full
recipe is in the comment block at the bottom of
[../argocd/application-perpod.yaml](../argocd/application-perpod.yaml).

**The point.** Expansion is online and cheap; shrinking is impossible; and the
ergonomics differ sharply between a PVC you control and one a StatefulSet
generates. Also note what expansion is *not*: it does not add IOPS in
proportion, and on Azure Disk the performance tier is a separate axis from the
size.

---

## Lab 9 - HPA and storage together

Back to the shared overlay, where autoscaling and persistence coexist.

```powershell
helm upgrade --install demo-app demo -f demo/values.yaml -f demo/values-shared.yaml -n demo-dev
# terminal 1
kubectl -n demo-dev get hpa,pods -w
# terminal 2
.\demo\scripts\load.ps1 -Url $URL -Seconds 240 -Concurrency 4
```

Watch the sequence: `TARGETS` climbs past 60%, replicas go up in steps
(`scaleUp.policies` allows doubling or +4 pods every 15s), and the new pods all
mount the same share. Then stop the load and wait - scale-down does nothing for
60 seconds (`scaleDown.stabilizationWindowSeconds`) and then removes at most
half the pods every 30 seconds.

```powershell
kubectl -n demo-dev describe hpa demo-app | Select-Object -Last 20
```

Now the GitOps part. While it is scaled up, try to make Argo CD fight it:

```powershell
argocd app get demo-app-dev            # should be Synced, not OutOfSync
```

It stays Synced for two independent reasons, and it is worth knowing both:

1. The chart does not render `spec.replicas` at all when
   `autoscaling.enabled` is true (see the comment in
   [../templates/deployment.yaml](../templates/deployment.yaml)).
2. The Application has `ignoreDifferences` on `/spec/replicas` as a backstop.

Delete reason 1 - set `autoscaling.enabled: false` but leave the HPA object in
the cluster - and you get the classic flapping loop: the HPA scales up, Argo CD
self-heals it back down, forever. Worth doing once so you recognise it.

**The point.** Horizontal autoscaling and persistent storage are compatible, but
only in specific combinations. Ephemeral + HPA: fine. Shared RWX + HPA: fine.
Per-pod RWO + HPA: works, and costs a disk per replica. Single RWO + HPA: broken
(lab 4). That table is the thing to take away.

---

## Cleanup

```powershell
helm uninstall demo-app -n demo-dev
helm uninstall demo-app-trap -n demo-trap

# PVCs are deliberately kept - storage.retainOnDelete and helm.sh/resource-policy.
# They are real Azure resources and they are still costing money:
kubectl -n demo-dev get pvc
kubectl -n demo-dev delete pvc --all
kubectl delete namespace demo-dev demo-trap demo-perpod
```

Then confirm the underlying disks and shares actually went, because a `Retain`
reclaim policy leaves them behind:

```powershell
kubectl get pv
az disk list -o table
```
