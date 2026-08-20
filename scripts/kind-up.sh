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

# Before anything reads a project file: TAG below asks uv for the version, and
# uv resolves the project from the working directory, not from this script.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CLUSTER=avd
CTX="kind-${CLUSTER}"
REG=kind-registry
REG_PORT=5001
# Named volume so the registry's contents outlive the container. kind-down.sh
# removes the registry with the cluster; without this, every teardown would also
# throw away the ~900MB cEOS image and make the next bring-up re-import it.
REG_VOL=${REG_VOL:-kind-registry-data}
IMG=function-avd-runtime
# Read from pyproject.toml, which the release workflow also publishes under, so
# a locally built image and a released package cannot mean different code under
# the same tag. Bump [project].version together with function changes: the
# registry caches by tag and pull is IfNotPresent, so rebuilding different code
# under an old tag ships the old image.
TAG=${TAG:-v$(uv version --short)}
# Pinned: an unpinned chart silently moves the cluster's Crossplane version, so
# e2e would test a different server than the one a result was recorded against.
# The CLI (v2.4.0) runs ahead of the chart; that skew is normal and fine.
# renovate: datasource=helm depName=crossplane registryUrl=https://charts.crossplane.io/stable
XP_CHART=${XP_CHART:-2.4.0}
# Multi-interface lab nodes (cEOS) need Multus and the bridge/host-device CNI
# plugins. Opt-in: they add a minute to a bring-up that the offline suite and the
# Fabric/Device tests never use. `WITH_NETCLAB=1 scripts/kind-up.sh` to get them.
WITH_NETCLAB=${WITH_NETCLAB:-0}
# renovate: datasource=github-releases depName=containernetworking/plugins
CNI_PLUGINS=${CNI_PLUGINS:-v1.9.1}
# netclab-chart's README points at master; pinned here
# renovate: datasource=github-releases depName=k8snetworkplumbingwg/multus-cni
MULTUS=${MULTUS:-v4.3.0}
# 0.5.9 is the real floor: it bootstraps cEOS eAPI on https/443, the same
# transport AVD renders. On 0.5.8 the bootstrap was http/6021, which the first
# pushed config replaced -- taking eAPI with it.
#
# Tracking 0.5.11 anyway, though nothing here needs it: its fixes are to
# RESTCONF, which this lab does not use. Staying current is cheaper than
# discovering the gap the day something does.
# renovate: datasource=helm depName=netclab registryUrl=https://netclab.github.io/netclab-chart
NETCLAB_CHART=${NETCLAB_CHART:-0.5.11}
# v1.0.14 is a floor too: the config push composes a *namespaced* Request
# (http.m.crossplane.io), which older provider-http releases do not serve.
# renovate: datasource=docker depName=xpkg.upbound.io/crossplane-contrib/provider-http
PROVIDER_HTTP=${PROVIDER_HTTP:-v1.0.14}
# The subset of the fabric to actually run. All 8 devices is 16Gi of cEOS; two
# make a link, and a link is enough to prove the config reached the device.
# At the default, the committed topology is used as-is; anything else is
# regenerated from LAB_FABRIC below.
LAB_HOSTS_DEFAULT=dc1-spine1,dc1-leaf1a
LAB_HOSTS=${LAB_HOSTS:-$LAB_HOSTS_DEFAULT}
# The Fabric the lab runs, and the topology derived from it. Keep the two in
# step: regenerate with
#   uv run avd-topology "$LAB_FABRIC" --hosts "$LAB_HOSTS_DEFAULT" > "$LAB_TOPOLOGY"
LAB_FABRIC=${LAB_FABRIC:-examples/fabric/single-dc-l3ls.yaml}
LAB_TOPOLOGY=${LAB_TOPOLOGY:-examples/lab/topology.yaml}

echo ">> local registry (data volume: ${REG_VOL})"
if [ -z "$(docker ps -q -f name="^${REG}$")" ]; then
  docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" \
    -v "${REG_VOL}:/var/lib/registry" --name "$REG" registry:2 >/dev/null
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
  name: netclab-function-avd
spec:
  package: ${REG_IP}:5000/netclab/function-avd:${TAG}
  packagePullPolicy: IfNotPresent
EOF
kubectl --context "$CTX" wait --for=condition=Healthy function.pkg.crossplane.io/netclab-function-avd --timeout=180s

echo ">> install XRDs + Compositions (Fabric + Device)"
kubectl --context "$CTX" apply -f apis/fabric/xrd.yaml -f apis/device/xrd.yaml
kubectl --context "$CTX" wait --for=condition=Established xrd/fabrics.avd.netclab.dev xrd/devices.avd.netclab.dev --timeout=60s
kubectl --context "$CTX" apply -f apis/fabric/composition.yaml -f apis/device/composition.yaml

if [ "$WITH_NETCLAB" = "1" ]; then
  echo ">> provider-http ${PROVIDER_HTTP} (config push over eAPI)"
  kubectl --context "$CTX" apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-http
spec:
  package: xpkg.upbound.io/crossplane-contrib/provider-http:${PROVIDER_HTTP}
EOF
  kubectl --context "$CTX" wait --for=condition=Healthy provider.pkg.crossplane.io/provider-http --timeout=300s
  # The push Requests are namespaced, so they reference a namespaced
  # ProviderConfig; the Secret token must work before AND after the first push,
  # which it does because the model's `arista` sha512 is the hash of "arista" --
  # the same credentials the netclab chart bootstraps.
  kubectl --context "$CTX" apply -f - <<EOF
apiVersion: http.m.crossplane.io/v1alpha2
kind: ProviderConfig
metadata:
  name: eapi
  namespace: default
spec:
  # eAPI auth rides in each Request's Authorization header (from the Secret
  # below); the ProviderConfig itself carries no credentials, but the field
  # is required, so it must say so explicitly.
  credentials:
    source: None
---
apiVersion: v1
kind: Secret
metadata:
  name: eapi-creds
  namespace: default
stringData:
  basic: $(printf 'arista:arista' | base64)
EOF

  echo ">> netclab-chart prerequisites: CNI plugins ${CNI_PLUGINS} + Multus ${MULTUS}"
  for node in $(kind get nodes --name "$CLUSTER"); do
    docker exec "$node" bash -c "curl -sSL \
      https://github.com/containernetworking/plugins/releases/download/${CNI_PLUGINS}/cni-plugins-linux-amd64-${CNI_PLUGINS}.tgz \
      | tar -xz -C /opt/cni/bin ./bridge ./host-device"
  done
  kubectl --context "$CTX" apply -f \
    "https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/${MULTUS}/deployments/multus-daemonset.yml"
  kubectl --context "$CTX" -n kube-system wait --for=jsonpath='{.status.numberReady}'=1 \
    --timeout=5m daemonset.apps/kube-multus-ds
  helm repo add netclab https://netclab.github.io/netclab-chart >/dev/null 2>&1 || true
  helm repo update netclab >/dev/null

  # The topology is derived from the lab fabric, not written by hand: AVD resolves
  # the cabling, so the lab cannot be wired differently from the config it runs.
  #
  # The default subset is generated once and committed, so this boots a
  # reviewed artifact and `test_topology_golden` can fail when an AVD upgrade
  # changes the peering. Asking for a different subset regenerates, because
  # --hosts is an input to generation and not a filter applied afterwards: a
  # link survives only when both of its ends are selected.
  if [ "$LAB_HOSTS" = "$LAB_HOSTS_DEFAULT" ]; then
    echo ">> lab topology from ${LAB_TOPOLOGY} (${LAB_HOSTS})"
    TOPO="$LAB_TOPOLOGY"
  else
    echo ">> lab topology regenerated for ${LAB_HOSTS}"
    TOPO="$(mktemp -t netclab-topology.XXXXXX.yaml)"
    TOPO_REGENERATED="$TOPO"
    uv run avd-topology "$LAB_FABRIC" --hosts "$LAB_HOSTS" > "$TOPO"
  fi

  # The topology names the image without a registry -- the name `docker import`
  # plus `kind load` leaves, and the one a reader of netclab-xp's documentation
  # ends up with. This lab serves cEOS from the local registry instead, because
  # that survives teardown. The EOS version still comes from the topology: it is
  # generated from the AVD model, so the model decides which image the lab runs.
  CEOS_TAG="$(awk '/image:/ {print $2; exit}' "$TOPO")"; CEOS_TAG="${CEOS_TAG##*:}"
  CEOS_REPO=${CEOS_REPO:-netclab/ceos}
  CEOS_IMG="localhost:${REG_PORT}/${CEOS_REPO}:${CEOS_TAG}"

  # cEOS cannot be pulled: it is licensed and needs an Arista login. Fail here
  # with the tag the topology asks for, rather than as an ImagePullBackOff later.
  if ! curl -sf "http://localhost:${REG_PORT}/v2/${CEOS_REPO}/tags/list" \
       | grep -q "\"${CEOS_TAG}\""; then
    echo "!! ${CEOS_IMG} is not in the local registry."
    echo "   Import it once (it outlives teardown in the ${REG_VOL} volume):"
    echo "     docker tag ceos:${CEOS_TAG} ${CEOS_IMG} && docker push ${CEOS_IMG}"
    exit 1
  fi

  # Rewrite a copy, never the committed file: it is an artifact other
  # repositories fetch at a tag, and `test_topology_golden` compares it to what
  # the generator produces.
  TOPO_LOCAL="$(mktemp -t netclab-topology-local.XXXXXX.yaml)"
  trap 'rm -f "$TOPO_LOCAL" "${TOPO_REGENERATED:-}"' EXIT
  sed "s|^\( *image: \).*|\1${CEOS_IMG}|" "$TOPO" > "$TOPO_LOCAL"

  echo ">> netclab-chart ${NETCLAB_CHART}"
  helm upgrade --install avd netclab/netclab --version "${NETCLAB_CHART}" \
    --kube-context "$CTX" -n default -f "$TOPO_LOCAL" >/dev/null
fi

echo
if [ "$WITH_NETCLAB" = "1" ]; then
  echo "Ready. cEOS nodes are up; apply the fabric they run:"
  echo "  kubectl --context ${CTX} apply -k examples/lab/"
  echo "  kubectl --context ${CTX} -n default get pods -l vendor=arista"
else
  echo "Ready. Apply a fabric, e.g.:"
  echo "  kubectl --context ${CTX} apply -f examples/fabric/single-dc-l3ls.yaml"
fi
echo "  kubectl --context ${CTX} -n default get fabric,cm -l avd.netclab.dev/device"
