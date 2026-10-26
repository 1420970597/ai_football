FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY services/hermes-mcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --system --uid 10002 --create-home appuser
COPY services/hermes-mcp/server.py /app/server.py
USER 10002:10002
EXPOSE 8090
CMD ["python", "/app/server.py"]
