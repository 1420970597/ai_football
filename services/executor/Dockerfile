FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY services/executor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd --system --uid 10004 --create-home executor
COPY services/collector /app/services/collector
COPY services/executor /app/services/executor
USER 10004:10004
CMD ["python", "-m", "services.executor.worker"]
