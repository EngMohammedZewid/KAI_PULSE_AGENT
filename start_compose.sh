#!/bin/bash
set -e

COMPOSE_FILE="/mnt/d/ISYS data/watsonx orchestrate ADK/.venv/Lib/site-packages/ibm_watsonx_orchestrate/developer_edition/resources/docker/compose-lite.yml"
ENV_FILE="/mnt/c/Users/ISYS_Staff/.cache/orchestrate/merged.env"

echo "=== Starting DB service ==="
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up wxo-server-db -d --remove-orphans 2>&1
echo "=== DB started, waiting 10s for init ==="
sleep 10

echo "=== Starting all other services ==="
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up --scale ui=0 -d --remove-orphans 2>&1
echo "=== All services started ==="

echo "=== Container status ==="
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps 2>&1
echo "=== DONE ==="
