FROM alpine:3.20
# python3 自带 sqlite3 模块；不装 pip 和任何第三方包
RUN apk add --no-cache python3 tzdata \
 && find /usr/lib/python3* -type d \( -name test -o -name tests -o -name idlelib -o -name tkinter -o -name ensurepip -o -name lib2to3 -o -name pydoc_data \) -prune -exec rm -rf {} + \
 && find /usr/lib/python3* -name '__pycache__' -prune -exec rm -rf {} + \
 && adduser -D -H -u 1000 jiapu && mkdir -p /data && chown jiapu /data
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV DB_PATH=/data/family.db PORT=8080 TZ=Asia/Shanghai PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER jiapu
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/api/health >/dev/null 2>&1 || exit 1
CMD ["python3", "/app/server.py"]
