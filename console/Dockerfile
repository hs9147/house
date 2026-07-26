# 이 리포 Dockerfile은 services/build.py의 "리포 자체 Dockerfile 우선" 규칙에 따라
# templates/dockerfiles/react.*.Dockerfile 대신 쓰인다 — 콘솔을 자기 배포(옵트인,
# PAAS_SELF_DEPLOY_CONSOLE)로 별도 컨테이너로 띄울 때 전용으로 필요하다.
#
# vite.config.ts의 base는 '/console/'(FastAPI가 /console에 정적 마운트하는 기본 배포
# 방식 전제)이지만, 자기 배포된 콘솔은 배포가 서브패스 기반이라 base_domain을 공유하고
# /apps/admin/paas-console/ 경로로 서빙된다(services/self_deploy.py — admin 조직
# 소속으로 등록한다). 그래서 base를 그 경로로 덮어써야 한다 — 일반
# react.release.Dockerfile 템플릿을 그대로 못 쓰는 이유.
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ENV NODE_ENV=production
RUN npm run build -- --base=/apps/admin/paas-console/

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
EXPOSE 80
CMD ["caddy", "file-server", "--root", "/srv", "--listen", ":80"]
