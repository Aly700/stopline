FROM alpine:3.17

ARG VERSION=v1
RUN mkdir -p /www && printf 'checkout %s\n' "$VERSION" > /www/index.html
COPY respond.sh /respond.sh
RUN chmod 755 /respond.sh

USER 65534:65534
EXPOSE 8080
CMD ["nc", "-lk", "-p", "8080", "-e", "/respond.sh"]
