FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY services/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --system --uid 10001 --create-home appuser
COPY services/api/app /app/app
COPY services/api/migrations /app/migrations
COPY --chmod=0444 services/quote_witness.py /app/quote_witness.py
COPY --chmod=0444 services/execution_scope.py /app/execution_scope.py
COPY --chmod=0444 services/basketball_scores.py /app/basketball_scores.py
# Frozen source checkouts can be private to the build user. Runtime source and
# SQL definitions are public code; keep them readable by the service account.
RUN chmod -R a+rX /app/app /app/migrations
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--proxy-headers", "--no-access-log"]
