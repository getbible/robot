# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/python -m pip install --require-hashes -r /tmp/requirements.txt

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROBOT_MODE=multi \
    ROBOT_CONFIG_DIR=/config/instances \
    ROBOT_DATA_DIR=/data

LABEL org.opencontainers.image.title="GetBible Robot" \
    org.opencontainers.image.description="Bounded and resilient Telegram interface for GetBible Scripture" \
    org.opencontainers.image.source="https://github.com/getbible/robot" \
    org.opencontainers.image.licenses="GPL-2.0-only"

RUN groupadd --gid 10001 robot \
    && useradd --uid 10001 --gid robot --home-dir /app --no-create-home robot \
    && install -d -o robot -g robot -m 0700 /app /data \
    && install -d -o robot -g robot -m 0750 /config/instances

COPY --from=builder /opt/venv /opt/venv
COPY --chown=robot:robot bot.py config.py /app/
COPY --chown=robot:robot modules /app/modules
COPY --chown=robot:robot miniapp /app/miniapp
COPY --chown=robot:robot container /app/container
COPY --chown=robot:robot LICENSE /app/LICENSE
COPY --chmod=0755 container/getbible-robot-container \
    /usr/local/bin/getbible-robot-container
COPY --chmod=0755 container/setup.sh /app/setup.sh
COPY --chmod=0755 container/setup.sh /usr/local/bin/getbible-robot-setup

USER 10001:10001
WORKDIR /app

# These are documentation only. Every instance may choose different ports.
EXPOSE 8081 9001 9201
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
    CMD ["python", "/app/container/runtime.py", "health"]

ENTRYPOINT ["python", "/app/container/runtime.py"]
CMD ["run"]
