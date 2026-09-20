# Standard library only, so there is nothing to install and nothing to audit for
# vulnerabilities beyond the interpreter itself.
#
# The tag floats rather than pinning to a digest on purpose: `python:3.12-slim` still
# receives Debian's own security patches on every rebuild, and nothing here automates
# bumping a pinned digest when one ships. Pinned without that automation, this would
# freeze in place and quietly stop receiving exactly the patches floating the tag gets
# for free — a worse trade for a project this size than the reproducibility a pin buys.
# Revisit this once a renewal process exists to keep a pin current.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="TV Retention" \
      org.opencontainers.image.description="Per-series retention for a TV library, applied through Sonarr." \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Not root. The container mounts no media and needs no host identity: it writes to one
# volume and talks to Sonarr over the network, so it has nothing to be privileged for.
RUN apt-get update \
  && apt-get install --no-install-recommends --yes tzdata \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --system --uid 1000 --create-home --home-dir /home/tvr tvr

WORKDIR /app
COPY src/worker/ /app/worker/
COPY src/assets/ /app/assets/
COPY src/include/ /app/include/
COPY VERSION BUILD LICENSE /app/
# Build metadata belongs to the image, not the semantic version. A source checkout has no
# honest build date; an image does, and this file is read only when a snapshot is requested.
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /app/BUILD_DATE

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TVR_CONFIG_DIR=/config \
  TVR_PORT=8787 \
  TZ=Etc/UTC

VOLUME ["/config"]
EXPOSE 8787

# Deliberately not USER: the process starts as root only long enough to take ownership of
# its volume, then drops to PUID/PGID itself. Run it with `user:` set and it skips both.

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status == 200 else 1)"

CMD ["python3", "/app/worker/server.py"]
