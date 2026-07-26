# 플랫폼(FastAPI 백엔드 + 콘솔 정적 산출물)을 하나의 이미지로 묶는다.
# 콘솔은 app/main.py가 /console에 정적 마운트하는 기존 방식을 그대로 쓴다 — 이 이미지는
# console/dist를 빌드해 넣기만 할 뿐, 서빙 로직은 바꾸지 않는다.
#
# 주의: 이 플랫폼 자체가 프로젝트를 배포할 때 호스트 Docker 데몬에 `docker build`/컨테이너
# 기동을 직접 실행한다(services/build.py, services/runtime/docker_runtime.py). 그래서 이
# 이미지에도 docker CLI를 넣고, 실행 시 호스트의 /var/run/docker.sock을 마운트해야 한다
# (docker-compose.yml 참고. "docker outside of docker" 구성).

FROM node:22-alpine AS console-build
WORKDIR /console
COPY console/package*.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt docker

COPY app/ ./app/
COPY templates/ ./templates/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY --from=console-build /console/dist/ ./console/dist/

EXPOSE 7000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7000"]
