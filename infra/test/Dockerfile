FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY services/api/requirements.txt /tmp/api-requirements.txt
COPY services/collector/requirements.txt /tmp/collector-requirements.txt
COPY services/hermes-mcp/requirements.txt /tmp/mcp-requirements.txt
RUN pip install --no-cache-dir -r /tmp/api-requirements.txt -r /tmp/collector-requirements.txt -r /tmp/mcp-requirements.txt \
    && useradd --system --uid 10001 --create-home verifier
WORKDIR /workspace
ENV PYTHONPATH=/workspace/services/api:/workspace
USER 10001:10001
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
