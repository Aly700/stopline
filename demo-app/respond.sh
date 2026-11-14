#!/bin/sh
# Consume the request headers before closing the connection.
cr=$(printf '\r')
while IFS= read -r line; do
    [ "$line" = "$cr" ] && break
    [ -z "$line" ] && break
done
printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' \
    "$(wc -c < /www/index.html | tr -d ' ')"
cat /www/index.html
