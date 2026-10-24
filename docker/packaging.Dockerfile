FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir "flit_core>=3.12" "pytest>=6.2" "hypothesis>=6" pretend "coverage[toml]>=7.2" tomli_w
