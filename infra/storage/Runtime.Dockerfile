ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# Overlay only verified storage lifecycle files on each role's actual image.
# Never rebuild unrelated working-tree transaction/account changes.
COPY --chown=10001:10001 services/api/migrations/033_raw_content_retention.sql /app/migrations/033_raw_content_retention.sql
COPY --chown=10001:10001 services/api/app/raw_retention.py services/api/app/retention_media.py services/api/app/message_read.py services/api/app/review_candidates.py /app/app/
COPY --chown=10001:10001 services/api/app/routes/public.py services/api/app/routes/expert_content.py services/api/app/routes/telegram.py /app/app/routes/
