ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# Apply the bounded cleanup correction to the actual current application
# release. Keep concurrent features, configuration, and schema files intact.
COPY --chown=10001:10001 services/api/app/raw_retention.py /app/app/raw_retention.py
COPY --chown=10001:10001 services/api/app/routes/telegram.py /app/app/routes/telegram.py
COPY --chown=10001:10001 services/api/app/message_read.py /app/app/message_read.py
COPY --chown=10001:10001 services/api/app/routes/expert_content.py /app/app/routes/expert_content.py
