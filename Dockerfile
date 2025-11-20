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

CMD ["node", "index.js"]
