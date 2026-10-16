# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_MODULES_CACHE=/tmp/hf_modules \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    TOKENIZERS_PARALLELISM=false
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 vistra
FROM base AS dependencies
ENV PATH=/opt/venv/bin:$PATH
RUN python -m venv /opt/venv
# Keep CPU torch in its own layer; application changes reuse the large wheel.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --no-compile 'torch>=2.8,<3' --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --no-compile -r requirements.txt && pip check
# Inference uses prebuilt libraries; extension compiler headers and pip are build-only.
RUN python -c "import shutil; shutil.rmtree('/opt/venv/lib/python3.12/site-packages/torch/include')" \
    && pip uninstall -y pip

FROM base AS runtime
ENV PATH=/opt/venv/bin:$PATH
COPY --from=dependencies /opt/venv /opt/venv
COPY src ./src
COPY api/__init__.py api/server.py api/runtime.py ./api/
COPY main.py config.yaml ./
ENV HF_HOME=/tmp/huggingface \
    HF_HUB_CACHE=/app/.cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
USER vistra
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
