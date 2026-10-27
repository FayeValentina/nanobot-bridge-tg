# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS webui
WORKDIR /src/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
COPY packages/client-events/ /src/packages/client-events/
# Channel-owned UI must be present when Vite discovers contributions.
COPY nanobot/channels/ /src/nanobot/channels/
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS python-build
WORKDIR /src
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY nanobot/ ./nanobot/
COPY --from=webui /src/nanobot/web/dist/ ./nanobot/web/dist/
RUN NANOBOT_SKIP_WEBUI_BUILD=1 uv build --wheel --out-dir /artifacts
RUN uv venv /opt/venv
RUN uv pip install --python /opt/venv/bin/python /artifacts/*.whl
COPY scripts/install_channel_dependencies.py ./scripts/
RUN uv pip install --python /opt/venv/bin/python pip && \
    /opt/venv/bin/python -m scripts.install_channel_dependencies telegram && \
    uv pip check --python /opt/venv/bin/python

FROM python:3.11-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 1001 nanobot && \
    useradd --uid 1001 --gid 1001 --create-home nanobot && \
    mkdir /data && chown 1001:1001 /data
COPY --from=python-build /opt/venv/ /opt/venv/
COPY --from=python-build /artifacts/ /opt/artifacts/
COPY deploy/smoke_probe.py /opt/smoke_probe.py
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /home/nanobot
USER 1001:1001
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="nanobot-bridge-tg" \
      org.opencontainers.image.source="https://github.com/FayeValentina/nanobot-bridge-tg" \
      org.opencontainers.image.revision=$SOURCE_REVISION
EXPOSE 8765 18790
HEALTHCHECK --interval=5s --timeout=5s --start-period=20s --retries=6 \
  CMD ["python", "/opt/smoke_probe.py", "--health-only"]
ENTRYPOINT ["python", "-m", "nanobot"]
CMD ["gateway", "--foreground", "--config", "/data/config.json"]
