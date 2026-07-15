#!/usr/bin/env bash
# Tear down the local kind cluster and registry created by kind-up.sh.
set -euo pipefail
kind delete cluster --name avd || true
docker rm -f kind-registry 2>/dev/null || true
echo "cluster + registry removed"
