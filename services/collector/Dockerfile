FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY services/collector/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --system --uid 10003 --create-home collector
COPY services/collector /app/collector
COPY --chmod=0444 services/quote_witness.py /app/quote_witness.py
COPY --chmod=0444 services/basketball_scores.py /app/basketball_scores.py
USER 10003:10003
CMD ["python", "-m", "collector.worker"]
