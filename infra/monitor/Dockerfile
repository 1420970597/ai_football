# The caller supplies an already present, exact application image ID/digest.
ARG EXPERT_MONITOR_BASE_IMAGE=sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d
FROM ${EXPERT_MONITOR_BASE_IMAGE}
USER 0:0
COPY expert_iteration_supervisor.py /opt/expert-monitor/expert_iteration_supervisor.py
COPY scan_scheduler.py /opt/expert-monitor/scan_scheduler.py
COPY result-schema.json /opt/expert-monitor/result-schema.json
COPY toolchain-packages.tsv /opt/expert-monitor/toolchain-packages.tsv
COPY toolchain-python-packages.json /opt/expert-monitor/toolchain-python-packages.json
# No installs, application startup, migrations, or auth/config baked into image.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/opt/expert-monitor/expert_iteration_supervisor.py"]
