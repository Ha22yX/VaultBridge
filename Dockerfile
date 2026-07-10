FROM python:3.12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends git openssh-client \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

ENV VAULTBRIDGE_HOST=0.0.0.0
ENV VAULTBRIDGE_PORT=8728
ENV VAULTBRIDGE_DATA_DIR=/app/data

EXPOSE 8728
CMD ["python", "-m", "app.main"]

