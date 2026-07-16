#!/usr/bin/env bash
# Bring up a local kind cluster running the AVD Fabric composite function,
# end to end: local registry -> kind -> Crossplane v2 -> function package ->
# XRD + Composition. Idempotent-ish; safe to re-run after `kind-down.sh`.
#
# Reproduces the manual bring-up. Key hurdle it encodes: the function image must
# be pullable by BOTH the Crossplane pod (cluster network) and the node's
# containerd (node network), served over plain HTTP -- so we reference the
# registry by its kind-network IP and mark it insecure for containerd. Crossplane
# itself falls back to HTTP for that registry automatically.
set -euo pipefail

CLUSTER=avd
CTX="kind-${CLUSTER}"
REG=kind-registry
REG_PORT=5001
IMG=function-avd-runtime
TAG=${TAG:-v0.0.7}
# Pinned: an unpinned chart silently moves the cluster's Crossplane version, so
# e2e would test a different server than the one a result was recorded against.
# The CLI (v2.4.0) runs ahead of the chart; that skew is normal and fine.
XP_CHART=${XP_CHART:-2.3.3}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ">> local registry"
if [ -z "$(docker ps -q -f name="^${REG}$")" ]; then
  docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" --name "$REG" registry:2 >/dev/null
fi

echo ">> kind cluster"
if ! kind get clusters | grep -qx "$CLUSTER"; then
  cat <<EOF | kind create cluster --name "$CLUSTER" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
EOF
fi
docker network connect kind "$REG" 2>/dev/null || true
REG_IP="$(docker inspect -f '{{(index .NetworkSettings.Networks "kind").IPAddress}}' "$REG")"
echo "   registry kind-network IP: ${REG_IP}"

echo ">> containerd: trust the registry over plain HTTP (localhost mirror + IP)"
for node in $(kind get nodes --name "$CLUSTER"); do
  for host in "localhost:${REG_PORT}" "${REG_IP}:5000"; do
    docker exec "$node" mkdir -p "/etc/containerd/certs.d/${host}"
    printf '[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' "$REG" \
      | docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/${host}/hosts.toml"
  done
done

echo ">> build + push function image/xpkg (tag ${TAG})"
docker build --provenance=false -t "${IMG}:${TAG}" .
crossplane xpkg build --package-root=package --embed-runtime-image="${IMG}:${TAG}" -o "function-avd-${TAG}.xpkg"
crossplane xpkg push -f "function-avd-${TAG}.xpkg" "localhost:${REG_PORT}/netclab/function-avd:${TAG}"

echo ">> install Crossplane (chart ${XP_CHART})"
helm repo add crossplane-stable https://charts.crossplane.io/stable >/dev/null 2>&1 || true
helm repo update crossplane-stable >/dev/null
helm upgrade --install crossplane crossplane-stable/crossplane --version "${XP_CHART}" \
  --namespace crossplane-system --create-namespace --wait --timeout 5m >/dev/null

echo ">> install Function (referenced by kind-network IP so both pullers reach it)"
kubectl --context "$CTX" apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Function
metadata:
  name: function-avd
spec:
  package: ${REG_IP}:5000/netclab/function-avd:${TAG}
  packagePullPolicy: IfNotPresent
EOF
kubectl --context "$CTX" wait --for=condition=Healthy function.pkg.crossplane.io/function-avd --timeout=180s

echo ">> install XRDs + Compositions (Fabric + Device)"
kubectl --context "$CTX" apply -f apis/fabric/xrd.yaml -f apis/device/xrd.yaml
kubectl --context "$CTX" wait --for=condition=Established xrd/fabrics.avd.netclab.dev xrd/devices.avd.netclab.dev --timeout=60s
kubectl --context "$CTX" apply -f apis/fabric/composition.yaml -f apis/device/composition.yaml

echo
echo "Ready. Apply a fabric, e.g.:"
echo "  kubectl --context ${CTX} apply -f examples/fabric/single-dc-l3ls.yaml"
echo "  kubectl --context ${CTX} -n default get fabric,cm -l avd.netclab.dev/device"
