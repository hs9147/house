FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ENV NODE_ENV=production
RUN npm run build --if-present && npm prune --omit=dev
EXPOSE 3000
USER node
CMD ["npm", "start"]
