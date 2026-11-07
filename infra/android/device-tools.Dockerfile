FROM ai-football-android-toolchain:v2.0.0-dev AS sdk
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=sdk /opt/android-sdk/platform-tools/adb /opt/android-sdk/platform-tools/adb
RUN echo 'a902be8f45c6c62e76c9efaf6947a0fa747c9cabd89a2ac8e0d16ecb30b3ed01  /opt/android-sdk/platform-tools/adb' | sha256sum --check -
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /source
