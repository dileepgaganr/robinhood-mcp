FROM node:20-slim

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --production

COPY requirements.txt ./
RUN pip3 install -r requirements.txt --break-system-packages

COPY . .

ENV NODE_TLS_REJECT_UNAUTHORIZED=0
ENV NODE_ENV=production
ENV NODE_OPTIONS="--max-old-space-size=2048"

# The container is a long-lived host for `docker exec -i ... node index.js`
# sessions, NOT a server itself. Running the MCP server as PID 1 meant it sat on
# a stdin nobody writes to, tripped its own idle watchdog, exited, and got
# restarted by `restart: unless-stopped` — and every container restart killed
# the exec session the MCP client was attached to. Keep PID 1 inert.
CMD ["sleep", "infinity"]
