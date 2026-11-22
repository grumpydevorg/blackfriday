FROM debian:trixie-slim
RUN mkdir -p /etc/postgresql-common \
    && printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates php8.4-cli php8.4-pgsql php8.4-sqlite3 postgresql-17 python3 \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0700 -o postgres -g postgres /state
WORKDIR /app
COPY db.php shop.php migrate.php schema.sql /app/
COPY aws/vm_runtime.py /app/
USER postgres
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/app/vm_runtime.py"]
