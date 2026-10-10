FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

ARG VERSION
ENV VERSION=${VERSION:-master}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
RUN /opt/venv/bin/pip install --no-cache-dir \
    "git+https://github.com/eggplants/edcb-bridge@${VERSION}"

FROM al3xos/python-distroless:3.14-debian13@sha256:5ae3f2e81e56307ba2548f34c216f52c58c2d1dcfd33646c3609189612fe8a72
COPY --from=builder /opt/venv /opt/venv
ENV PYTHONPATH="/opt/venv/lib/python3.14/site-packages"

EXPOSE 4510
ENTRYPOINT ["python", "/opt/venv/bin/edcb-bridge"]
