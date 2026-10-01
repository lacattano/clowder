# syntax=docker/dockerfile:1
#
# The remote-pi relay, on a minimal base.
#
# The binary is committed beside this file (crew/relay/relay). It was extracted with
# `docker cp` from the published image, never rebuilt. Keeping it here means the image
# no longer depends on that third-party image or on the publisher's Dockerfile.
#
# Build:  docker build -t clowder-relay:0.1 crew/relay
# Recipe: crew/relay/README.md
#
# Both bases are pinned by digest as well as tag, so a moved tag cannot change what
# this image is built from. The digest is the multi-platform index; the tag beside it
# says where it came from. Recorded in README.md under "Pinned bases".

FROM gcr.io/distroless/cc-debian12:nonroot@sha256:9dac0a79194e45a7da0158a9c6da57b217585af0786db3845d1f0ec1a0dd182f

# Distroless has no shell, no curl and no package manager, and the relay has no health
# subcommand, so the healthcheck needs a probe binary. A static busybox is copied from
# its own image and used only as `wget`. The image still has no shell command and no
# package manager.
COPY --from=busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092 /bin/busybox /bin/busybox

COPY relay /usr/local/bin/relay

ENV REMOTEPI_RELAY_PORT=3000
ENV REMOTEPI_MESH_DB_PATH=/data/mesh.db

EXPOSE 3000
VOLUME ["/data"]

# Same contract as the published image: the command is `relay`.
CMD ["relay"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://localhost:3000/health"]
