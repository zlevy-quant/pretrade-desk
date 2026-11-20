# Dependency-free: the image is just Python plus the source.
FROM python:3.12-slim

WORKDIR /app
COPY pretrade ./pretrade
COPY public ./public

RUN mkdir -p /app/data
ENV DATA_DIR=/app/data PORT=8080
EXPOSE 8080

CMD ["python", "-m", "pretrade"]
