# 锁定构建工具版本；发布时可进一步通过镜像 digest 固定基础镜像
FROM public.ecr.aws/docker/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS builder
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple uv==0.13.1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY build-assets/ /build-assets/
ARG OFFLINE_BUILD=0
RUN if [ "$OFFLINE_BUILD" = "1" ]; then \
      uv venv /app/.venv && \
      uv --no-config pip install --python /app/.venv/bin/python --no-index --find-links=/build-assets/wheels \
        --require-hashes -r /build-assets/runtime-requirements.txt && \
      uv pip install --python /app/.venv/bin/python --no-index --find-links=/build-assets/wheels \
        hatchling==1.29.0; \
    else uv sync --frozen --no-dev --no-install-project; fi
COPY src/ ./src/
RUN if [ "$OFFLINE_BUILD" = "1" ]; then \
      uv pip install --python /app/.venv/bin/python --no-deps --no-build-isolation .; \
    else uv sync --frozen --no-dev --no-editable; fi \
    && .venv/bin/kama --version

FROM public.ecr.aws/docker/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=1 update \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=1 install -y --no-install-recommends bubblewrap util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 kama
WORKDIR /app/workspace
COPY --from=builder /app/.venv /app/.venv
RUN mkdir -p /app/runs /app/workspace /home/kama/.kama \
    && chown -R kama:kama /app/runs /app/workspace /home/kama/.kama
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    KAMA_HOST=127.0.0.1 \
    KAMA_PORT=7437 \
    KAMA_LOG_LEVEL=INFO \
    KAMA_LOG_FORMAT=json
USER kama
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "kama_claude.core.transport.healthcheck"]
EXPOSE 7437
CMD ["kama-core"]
