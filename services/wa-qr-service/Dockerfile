# PingPulse — WhatsApp Web session bridge.
FROM node:20-alpine

ENV NODE_ENV=production

WORKDIR /app

# Baileys resolves at least one dependency straight from a git URL, and some of
# its crypto dependencies build native code, so git and a toolchain have to be
# present at install time. They are removed again afterwards — a compiler left
# in a production image is dead weight and extra attack surface.
COPY services/wa-qr-service/package.json ./package.json
RUN apk add --no-cache --virtual .build-deps git python3 make g++ \
 && npm install --omit=dev --no-audit --no-fund \
 && apk del .build-deps

COPY services/wa-qr-service/src ./src

# Paired sessions live here, on a mounted volume, so a restart does not force
# every client to scan a QR code again.
RUN mkdir -p /data/wa_sessions

EXPOSE 3100

HEALTHCHECK --interval=20s --timeout=8s --start-period=20s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:3100/health || exit 1

CMD ["node", "src/index.js"]
