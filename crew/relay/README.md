# The relay image

A minimal replacement for `jacobmoura7/remote-pi-relay:latest`.

The published image is a 6.3MB compiled Rust binary on a full Debian base, runs as
root, and Docker Scout reports 96 vulnerabilities in 21 packages. Almost all of those
sit in base packages the relay never runs.

This image keeps the same binary on a distroless base, runs as a non-root user, and has
no shell command and no package manager.

## What is here

- `relay` - the binary, extracted from the published image with `docker cp`. Not
  rebuilt from source, so the publisher's Dockerfile is not used.

  ```
  docker create --name relay-extract jacobmoura7/remote-pi-relay:latest
  docker cp relay-extract:/usr/local/bin/relay crew/relay/relay
  docker rm relay-extract
  ```

  sha256 of the committed binary, so the bytes can be checked:

  ```
  12eb99d8b767b8d519c870fb164c88de1ca157e95212b7698ea9a7a32bf2f082
  ```

- `Dockerfile` - `gcr.io/distroless/cc-debian12:nonroot`, the binary at
  `/usr/local/bin/relay`, and a static busybox used only as `wget` for the healthcheck.

The runtime contract is unchanged: port 3000, `REMOTEPI_RELAY_PORT=3000`,
`REMOTEPI_MESH_DB_PATH=/data/mesh.db`, the named volume `remote-pi-data` at `/data`, and
`cmd: [relay]`.

## Build

```
docker build -t clowder-relay:0.1 crew/relay
```

## Run on a spare port, with the same volume

The relay writes `/data/mesh.db`. The existing volume `remote-pi-data` is owned by
`root`, so give the image's non-root user (65532) write access once. This changes
ownership, not content, and the running relay (root) keeps working:

```
docker run --rm --user 0 -v remote-pi-data:/data busybox chown -R 65532:65532 /data
docker run -d --name clowder-relay-test \
  -p 127.0.0.1:3001:3000 \
  -v remote-pi-data:/data \
  -e REMOTEPI_RELAY_PORT=3000 \
  -e REMOTEPI_MESH_DB_PATH=/data/mesh.db \
  clowder-relay:0.1
```

`remote-pi-relay` on port 3000 keeps running. Nothing here stops, replaces or restarts
it, and the volume is reused, never recreated.

## Check

```
docker inspect clowder-relay-test --format '{{.Config.User}} {{json .Config.Env}} {{json .Config.Cmd}}'
curl -fsS http://127.0.0.1:3001/health
docker scout quickview clowder-relay:0.1
```

## Remove only the test container

```
docker rm -f clowder-relay-test
```

To switch the crew to this image later: stop and replace `remote-pi-relay` only on the
owner's word, because that container carries the bus.
