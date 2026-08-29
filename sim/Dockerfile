# Night Watch telemetry simulator (data + action plane) for Cloud Run.
FROM node:20-slim
WORKDIR /app
COPY package.json ./
# zero runtime dependencies — pure Node stdlib
RUN npm install --omit=dev --ignore-scripts || true
COPY index.js ./
ENV PORT=8080
EXPOSE 8080
CMD ["node", "index.js"]
