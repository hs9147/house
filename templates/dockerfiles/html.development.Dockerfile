# 정적 HTML/CSS/JS — 빌드 단계가 없어 dev/release 서빙 방식은 동일하다
FROM caddy:2-alpine
COPY . /srv
EXPOSE 80
CMD ["caddy", "file-server", "--root", "/srv", "--listen", ":80"]
