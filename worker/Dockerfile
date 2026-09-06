# worker/Dockerfile
#
# Minimal sandbox runtime worker for FrostAgent.

FROM python:3.12.12-slim-bookworm

LABEL maintainer="Foxerine"
LABEL description="Minimal sandbox runtime worker for FrostAgent."

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1

# --- System Dependencies ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        supervisor \
        tini \
        iptables \
        git \
        curl \
        jq \
        ripgrep \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Install Python dependencies using uv and remove build-only binaries ---
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt /bin/uv /bin/uvx

# --- Sandbox user ---
RUN useradd --create-home -d /sandbox --shell /bin/bash sandbox && \
    chown -R sandbox:sandbox /sandbox

# --- Application code ---
WORKDIR /worker
ENV PYTHONPATH=/

COPY . /worker
COPY supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

WORKDIR /sandbox

EXPOSE 8000
ENTRYPOINT ["/worker/entrypoint.sh"]
