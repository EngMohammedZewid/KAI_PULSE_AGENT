#!/bin/bash
IMAGES=(
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-server-server:09-04-2026-b361c7c"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-server-conversation_controller:09-04-2026-b361c7c"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-agent-gateway:05-11-2025-91a7333"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-builder:08-04-2026-6417d8a"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-tempus-runtime:07-04-2026-3626e90"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/tools-runtime-manager:14-04-2026-55ec205"
"quay.io/coreos/etcd:v3.5.18"
"docker.io/redis:7.2"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-socket-handler:06-02-2025-1bddb56"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-agentic-memory:20260330-418e231"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-chat:01-04-2026-226ace"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-knowledge-mcp-server:10-04-2026-2745b90"
"opensearchproject/opensearch:2.11.0"
"docker.io/milvusdb/milvus:v2.5.15"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/ai-gateway:27-01-2026-6bb3df4"
"docker.io/minio/mc:latest"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/tools-runtime:14-04-2026-55ec205"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-agent-runtime:27-02-2026-55be48d"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/mcp-gateway:20260326-develop-156-d2b9120"
"registry.eu-de.watson-orchestrate.cloud.ibm.com/cp/wxo-lite/wxo-connections:2-04-2026-de7190d"
"docker.io/minio/minio:latest"
)
FAILED=()
COUNT=0
TOTAL=${#IMAGES[@]}
for img in "${IMAGES[@]}"; do
  COUNT=$((COUNT+1))
  echo "=== [$COUNT/$TOTAL] Pulling: $img ==="
  SUCCESS=0
  for attempt in 1 2 3; do
    if docker pull "$img"; then
      echo "=== SUCCESS: $img ==="
      SUCCESS=1
      break
    else
      echo "=== RETRY $attempt for $img ==="
    fi
  done
  if [ $SUCCESS -eq 0 ]; then
    echo "=== FAILED after 3 attempts: $img ==="
    FAILED+=("$img")
  fi
done
echo ""
echo "===== SUMMARY ====="
echo "Total: $TOTAL"
echo "Failed: ${#FAILED[@]}"
for f in "${FAILED[@]}"; do echo "  FAILED: $f"; done
echo "===== DONE ====="
