# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG INFISICAL_CLI_VERSION=0.43.84
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
  && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@infisical/cli@${INFISICAL_CLI_VERSION}"
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system .

EXPOSE 8080
CMD ["iris-voice", "--host", "0.0.0.0", "--port", "8080"]
