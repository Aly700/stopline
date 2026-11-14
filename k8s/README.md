# Checkout demo target

The existing kind cluster must be named `stopline`. These scripts never create
or delete a cluster. They use only context `kind-stopline`, namespace `checkout`.

```sh
demo-app/build.sh
k8s/up.sh
uv run pytest tests/kube -o addopts=""
```

The build uses the local `alpine:3.17` image, with no package installation or
image pull. It creates and loads `stopline/checkout:v1` and `:v2`. Each image
uses BusyBox `nc` and a shell script to answer HTTP requests on port 8080 with
one line: `checkout v1` or `checkout v2`. Both run as a non-root user.

The starting state has one ready replica of `checkout-api` at v2 and one of
`checkout-worker` at v1. Images use `imagePullPolicy: Never`; there are no probes
to delay startup. The API's v2 image represents the bad release in the demo;
the HTTP response itself still works.

```sh
k8s/reset.sh
```

Reset deletes only the two named Deployments and their owned ReplicaSets and
Pods, then recreates the initial state. It intentionally clears all history
between demonstrations. Do not reset or run the tests during a demonstration.

The Python adapter is `stopline.targets.kube.KubeCluster`. Each send changes
the image or restart timestamp and the action annotations in one merge patch.
The returned patch object confirms acceptance; it does not wait for rollout.
The pod-template annotations `stopline.dev/action-id`, `stopline.dev/args-hash`,
and `stopline.dev/action` preserve the id, arguments hash and original result.
ReplicaSet templates retain the same evidence after a later action. Evidence
uses the Deployment generation because the revision is assigned asynchronously.
The patch includes the resource version read beforehand. A conflict retries
within the same five-second deadline, including any wait for the sender lock.
Before replacing an earlier action's
template, the adapter checks that its ReplicaSet already exists; a paused
controller cannot make it erase the only copy of an earlier action's evidence.

Repeated action ids return the original evidence without another mutation.
Changing the arguments, tool, or target with the same id is refused. A fence
stores the id in the `stopline.dev/fences` metadata annotation on both
Deployments, because the fence protocol receives an id without a target name.
Fences survive adapter restarts. Lookup distinguishes applied actions from
fenced actions; fencing an already applied action does not hide its outcome.
`cut()` makes this adapter immediately unreachable, and `restore()` reconnects
it. `reset()` restores connectivity and invokes the same reset script above.
Neither cluster tool declares an inverse.

```sh
kubectl --context kind-stopline -n checkout get deployments,replicasets -o json
```

The sender is the only writer during a run. Threads and adapter instances in
that process share a mutation lock. Each Deployment retains up to 100
old ReplicaSets; reset clears them. This is a small demonstration target, not
an unbounded action archive. Live tests skip when the dedicated cluster cannot
be reached; transport tests also run without a cluster.
