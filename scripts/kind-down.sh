#!/usr/bin/env bash
# Tear down the local kind cluster and registry created by kind-up.sh.
set -euo pipefail
kind delete cluster --name avd || true
docker rm -f kind-registry 2>/dev/null || true
# The registry's data volume is deliberately kept: re-importing the ~900MB cEOS
# image on every bring-up costs minutes. `docker volume rm kind-registry-data`
# to reclaim the space.
echo "cluster + registry container removed (registry data volume kept)"
