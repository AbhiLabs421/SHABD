# SHABD — production Dockerfile
#
# Zero runtime dependencies, so the image is tiny and the supply-chain
# surface is small. Build:
#
#     docker build -t shabd:2.2 .
#
# Run:
#
#     docker run -p 8765:8765 \
#         -e SHABD_SECRET=$(openssl rand -hex 32) \
#         -v $PWD/audit:/data \
#         shabd:2.2

FROM python:3.12-slim AS base

# Non-root user — never run as root in production.
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} shabd \
 && useradd  -u ${UID} -g shabd -d /app -s /usr/sbin/nologin shabd

WORKDIR /app

# Copy *only* what's needed at runtime.
COPY --chown=shabd:shabd shabd.py shabd_client.py /app/
COPY --chown=shabd:shabd examples/quickstart.py /app/server.py

# Where the Grimoire chain is persisted; mount a volume here.
RUN mkdir -p /data && chown shabd:shabd /data
VOLUME ["/data"]

USER shabd

ENV SHABD_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=2).status==200 else 1)"

CMD ["python", "/app/server.py"]
