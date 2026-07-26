# 정적 HTML/CSS/JS — 빌드 단계 없이 리포 내용을 그대로 서빙
FROM caddy:2-alpine
COPY . /srv
EXPOSE 80
CMD ["caddy", "file-server", "--root", "/srv", "--listen", ":80"]
