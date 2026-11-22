FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends php-cli php-sqlite3 php-pgsql \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 10001 -g 10001 /state
WORKDIR /app
COPY arena.py simulator.py index.html db.php shop.php migrate.php schema.sql ./
USER 10001:10001
CMD ["python", "-u", "arena.py"]
