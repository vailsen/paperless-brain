# PaperlessBrain — app image
#
# Build variants:
#   docker build -t paperless-brain .                     # full (web crawling incl. headless Chromium)
#   docker build --build-arg LEAN=1 -t paperless-brain:lean .
#                                                         # ~1 GB smaller; web pages are fetched with
#                                                         # trafilatura only (no JS rendering)
#
# Torch is installed from the PyTorch CPU wheel index — the +cpu local version
# outranks the CUDA build on PyPI, so no NVIDIA packages end up in the image.

FROM python:3.14-slim

ARG LEAN=0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PATH=/app/

# git            — vault sync (change detection & audit trail)
# openssh-client — optional remote shutdown of the Ollama host
# libgomp1       — OpenMP runtime for torch/onnxruntime CPU wheels
# libpango*      — WeasyPrint text layout engine (PDF generation)
# fonts-dejavu-core — a real font family behind the Helvetica/Arial CSS stack;
#                     without it generated PDFs render without glyphs
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git openssh-client libgomp1 \
        libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu . \
    && if [ "$LEAN" = "0" ]; then \
        pip install --extra-index-url https://download.pytorch.org/whl/cpu ".[crawl]" \
        && playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

EXPOSE 8080

# Uses stdlib only — curl is not part of the slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/login', timeout=4)"

CMD ["python", "main.py"]
