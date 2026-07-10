FROM python:3.12-slim

LABEL org.opencontainers.image.title="VaultBridge" \
      org.opencontainers.image.description="Self-hosted NAS backup panel with SSH, rsync, and Git-versioned snapshots." \
      org.opencontainers.image.source="https://github.com/Ha22yX/VaultBridge"

RUN apt-get update \
  && apt-get install -y --no-install-recommends git openssh-client rsync sshpass \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN groupadd --gid 1002 vaultbridge \
  && useradd --uid 1001 --gid 1002 --home-dir /app/data --create-home --shell /usr/sbin/nologin vaultbridge \
  && chown -R 1001:1002 /app/data

ENV VAULTBRIDGE_HOST=0.0.0.0
ENV VAULTBRIDGE_PORT=8728
ENV VAULTBRIDGE_DATA_DIR=/app/data
ENV HOME=/app/data

EXPOSE 8728
CMD ["python", "-m", "app.main"]
