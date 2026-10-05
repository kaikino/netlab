#!/usr/bin/env bash
# netlab launcher: ./play.sh            start the lab (or open another terminal into a running one)
#                  ./play.sh --reset    wipe saved progress
#                  ./play.sh --rebuild  force an image rebuild
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=netlab:latest NAME=netlab VOLUME=netlab-data

if ! command -v docker >/dev/null; then
  echo "Docker is required: https://www.docker.com/products/docker-desktop/"; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker…"
  [ "$(uname)" = Darwin ] && open -ga Docker
  for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || { echo "Docker isn't running. Start Docker Desktop and retry."; exit 1; }
fi

case "${1:-}" in
  --reset)   docker rm -f $NAME >/dev/null 2>&1 || true; docker volume rm $VOLUME >/dev/null 2>&1 || true
             echo "Progress wiped."; exit 0 ;;
  --rebuild) docker image rm -f $IMAGE >/dev/null 2>&1 || true ;;
esac

# Rebuild only when the lab sources change.
HASH=$(cat Dockerfile lab/* | shasum | cut -c1-12)
if [ "$(docker image inspect -f '{{index .Config.Labels "netlab.hash"}}' $IMAGE 2>/dev/null)" != "$HASH" ]; then
  echo "Building the netlab image (first run takes a minute)…"
  docker build -q --label netlab.hash="$HASH" -t $IMAGE . >/dev/null
fi

if [ "$(docker inspect -f '{{.State.Running}}' $NAME 2>/dev/null)" = true ]; then
  echo "netlab is already running: opening another shell into the same lab."
  exec docker exec -it -e NETLAB_ATTACH=1 $NAME bash
fi
docker rm -f $NAME >/dev/null 2>&1 || true
exec docker run --rm -it --privileged --name $NAME --hostname netlab -v $VOLUME:/root/.netlab $IMAGE
