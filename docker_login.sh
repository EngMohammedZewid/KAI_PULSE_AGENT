#!/bin/bash
for i in 1 2 3 4 5; do
  echo "Attempt $i..."
  if printf "%s" "${WXO_REGISTRY_PASSWORD:?set WXO_REGISTRY_PASSWORD}" | docker login -u "${WXO_REGISTRY_USER:?set WXO_REGISTRY_USER}" --password-stdin registry.eu-de.watson-orchestrate.cloud.ibm.com 2>&1; then
    echo "LOGIN SUCCESS"
    exit 0
  fi
  echo "Failed, retrying in 5s..."
  sleep 5
done
echo "ALL ATTEMPTS FAILED"
exit 1
