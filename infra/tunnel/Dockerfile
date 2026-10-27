FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG OPENSSH_VERSION=1:10.0p1-7+deb13u4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends openssh-client=${OPENSSH_VERSION} \
    && dpkg-query -W > /usr/share/tunnel-package-versions \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY infra/tunnel/gateway.py /app/gateway.py
EXPOSE 15432 18080
HEALTHCHECK --interval=15s --timeout=4s --retries=3 CMD ["python", "/app/gateway.py", "health"]
ENTRYPOINT ["python", "/app/gateway.py"]
