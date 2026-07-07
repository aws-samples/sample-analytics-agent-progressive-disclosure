# /ask 中继:轻量 Node 服务,验 JWT 后调 AgentCore Runtime 并透传 SSE。
FROM public.ecr.aws/docker/library/node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force
COPY server.mjs ./
ENV PORT=8000 NODE_ENV=production
EXPOSE 8000
USER node
CMD ["node", "server.mjs"]
