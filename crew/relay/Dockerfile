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

FROM gcr.io/distroless/cc-debian12:nonroot

# Distroless has no shell, no curl and no package manager, and the relay has no health
# subcommand, so the healthcheck needs a probe binary. A static busybox is copied from
# its own image and used only as `wget`. The image still has no shell command and no
# package manager.
COPY --from=busybox:1.37.0-musl /bin/busybox /bin/busybox

COPY relay /usr/local/bin/relay

ENV REMOTEPI_RELAY_PORT=3000
ENV REMOTEPI_MESH_DB_PATH=/data/mesh.db

EXPOSE 3000
VOLUME ["/data"]

# Same contract as the published image: the command is `relay`.
CMD ["relay"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://localhost:3000/health"]
