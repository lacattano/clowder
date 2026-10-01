# The relay image

A minimal replacement for `jacobmoura7/remote-pi-relay:latest`.

The published image is a 6.3MB compiled Rust binary on a full Debian base, runs as
root, and Docker Scout reported 96 vulnerabilities in 21 packages in the first
walkthrough. That count could not be re-checked here, because `docker scout` needs a
Docker login this machine does not have: treat it as the reporter's number, unverified.
Almost all of those sit in base packages the relay never runs.

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

- `Dockerfile` - `gcr.io/distroless/cc-debian12:nonroot` (pinned by digest, see
  Pinned bases), the binary at `/usr/local/bin/relay`, and a static busybox used only as
  `wget` for the healthcheck.

The runtime contract is unchanged: port 3000, `REMOTEPI_RELAY_PORT=3000`,
`REMOTEPI_MESH_DB_PATH=/data/mesh.db`, the named volume `remote-pi-data` at `/data`, and
`cmd: [relay]`.

## Build

```
docker build -t clowder-relay:0.1 crew/relay
```

## Pinned bases

Both bases are pinned by digest as well as tag, so a moved tag cannot change what the
image is built from. The digest is the multi-platform index; the build resolves the
platform manifest under it.

| Base | Tag it came from | Digest |
|---|---|---|
| distroless | `gcr.io/distroless/cc-debian12:nonroot` | `sha256:9dac0a79194e45a7da0158a9c6da57b217585af0786db3845d1f0ec1a0dd182f` |
| busybox | `busybox:1.37.0-musl` | `sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092` |

The build log names each `FROM ...@sha256:...`, which is the proof the pins resolve.

## Run on a spare port, with the same volume

The relay writes `/data/mesh.db`. The existing volume `remote-pi-data` is owned by
`root`, so give the image's non-root user (65532) write access once. This changes
ownership, not content, and the running relay (root) keeps working. `MSYS_NO_PATHCONV=1`
stops Git-Bash from rewriting the container path `/data` into a Windows path:

```
MSYS_NO_PATHCONV=1 docker run --rm --user 0 -v remote-pi-data:/data busybox chown -R 65532:65532 /data
```

Then run the image. Do not pass `-e` for either setting: the image already sets
`REMOTEPI_RELAY_PORT=3000` and `REMOTEPI_MESH_DB_PATH=/data/mesh.db` in its own `ENV`. It
matters because Git-Bash rewrites the value of a path-looking `-e`, so
`-e REMOTEPI_MESH_DB_PATH=/data/mesh.db` arrives inside the container as
`REMOTEPI_MESH_DB_PATH=C:/Program Files/Git/data/mesh.db`. The relay then makes a fresh,
empty database in its own layer and never reads the volume. The image's own `ENV` cannot
be rewritten, so leaving `-e` out is the fix:

```
docker run -d --name clowder-relay-test \
  -p 127.0.0.1:3001:3000 \
  -v remote-pi-data:/data \
  clowder-relay:0.1
```

`remote-pi-relay` on port 3000 keeps running. Nothing here stops, replaces or restarts
it, and the volume is reused, never recreated.

### What the first walkthrough proved, and what it did not

The first spare-port test read seven peers and called the mesh proven. It was not. Its
`REMOTEPI_MESH_DB_PATH` was the rewritten Windows path above, so the relay opened a
fresh database at `/home/nonroot/C:/Program Files/Git/data/mesh.db` in its own layer
(`docker diff clowder-relay-test` showed the file); the real volume's `mesh.db` was never
read. The peer list is live, in-memory membership, so it looked correct either way. The
pairing data is the signed blob in `mesh.db`; check that file, not the peer list.

## Check

```
docker inspect clowder-relay-test --format '{{.Config.User}} {{json .Config.Env}} {{json .Config.Cmd}}'
curl -fsS http://127.0.0.1:3001/health
```

`REMOTEPI_MESH_DB_PATH` must read `/data/mesh.db`. If it reads a Windows path, stop:
the relay is serving a fresh database in its own layer, not the volume.

The functional check is a client reading the mesh through the spare port, with the relay
URL overridden by an environment variable so the live config is untouched:

```
REMOTE_PI_RELAY=http://127.0.0.1:3001 remote-pi peers
```

That reads live membership, which is not the pairing data. To prove the volume's own
`mesh.db` is the file being served, compare the inode the relay holds open with the
volume's file:

```
docker run --rm --privileged --pid=container:clowder-relay-test busybox \
  sh -c 'for f in /proc/1/fd/*; do readlink "$f"; done' | grep /data/mesh.db
docker run --rm --user 0 -v remote-pi-data:/data:ro busybox \
  sh -c 'stat -c "inode=%i size=%s" /data/mesh.db'
```

The inode on the two lines must match. `GET /mesh/<owner_pk_hash>` on the spare port
must also return the same signed blob as the live relay; the owner hash and blob are in
the `mesh_versions` table of `mesh.db`.

Note: a plain container path passed to `docker run` (for example `busybox ls -la /data`)
is still rewritten by Git-Bash. Pass it inside `sh -c '...'`, as above, or prefix the
command with `MSYS_NO_PATHCONV=1`.

## Remove only the test container

```
docker rm -f clowder-relay-test
```

To switch the crew to this image later: stop and replace `remote-pi-relay` only on the
owner's word, because that container carries the bus.
