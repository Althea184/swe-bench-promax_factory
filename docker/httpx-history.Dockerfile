FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir \
    hatchling hatch-fancy-pypi-readme editables "httpcore==1.*" anyio idna certifi \
    pytest==8.3.3 trustme==1.1.0 cryptography==43.0.1 uvicorn==0.31.0 trio==0.26.2 \
    brotli zstandard
