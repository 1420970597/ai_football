FROM mcr.microsoft.com/playwright:v1.56.1-noble@sha256:f1e7e01021efd65dd1a2c56064be399f3e4de00fd021ac561325f2bfbb2b837a
WORKDIR /opt/verification
COPY tests/browser/package*.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
ENV NODE_PATH=/opt/verification/node_modules
WORKDIR /work
ENTRYPOINT ["node"]
