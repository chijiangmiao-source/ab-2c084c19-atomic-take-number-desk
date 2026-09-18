# 验收服务：Node（跑 Vitest）+ Python（跑 pytest / Playwright）合一的一次性容器。
FROM node:20-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_BREAK_SYSTEM_PACKAGES=1

COPY verify/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt \
    && playwright install --with-deps chromium

WORKDIR /app

# 前端依赖（Vitest 单元测试在验收容器内运行）
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci
COPY web ./web

COPY verify ./verify

ENV API_BASE=http://api:8000 \
    WEB_BASE=http://web \
    API_CONTAINER=shotnum-api

CMD ["bash", "verify/run_verify.sh"]
