FROM debian:trixie-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends php8.4-cli \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY submission.php /app/submission.php
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["php", "-S", "0.0.0.0:8080", "/app/submission.php"]
