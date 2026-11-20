# Build and run the Pre-Trade Desk as a self-contained container.
# No npm install: the app uses only Node built-ins (node:http, node:sqlite).
FROM node:22-bookworm-slim

WORKDIR /app

COPY package.json ./
COPY src ./src
COPY public ./public

RUN mkdir -p /app/data
ENV DATA_DIR=/app/data
ENV PORT=8080
EXPOSE 8080

CMD ["node", "--experimental-sqlite", "src/server.js"]
