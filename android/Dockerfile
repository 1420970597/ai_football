FROM gradle:8.9.0-jdk17@sha256:682c0245826af1e6f5f7b95306cc7039d3d8d8e8de06b1a330e6f9015ee757a0 AS toolchain
USER root
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH=/opt/android-sdk/cmdline-tools/23.0/bin:/opt/android-sdk/build-tools/35.0.0:/opt/android-sdk/platform-tools:${PATH}
ARG COMMAND_TOOLS_SHA256=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583
RUN curl --fail --location --retry 3 --max-time 240 https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip -o /tmp/android-tools.zip \
    && echo "${COMMAND_TOOLS_SHA256}  /tmp/android-tools.zip" | sha256sum --check - \
    && mkdir -p /opt/android-sdk/cmdline-tools \
    && unzip -q /tmp/android-tools.zip -d /opt/android-sdk/cmdline-tools \
    && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/23.0 \
    && rm /tmp/android-tools.zip \
    && yes | sdkmanager --licenses >/dev/null \
    && curl --fail --location --retry 3 --max-time 180 https://dl.google.com/android/repository/platform-35_r02.zip -o /tmp/platform.zip \
    && echo '0988cacad01b38a18a47bac14a0695f246bc76c1b06c0eeb8eb0dc825ab0c8e0  /tmp/platform.zip' | sha256sum --check - \
    && mkdir -p /opt/android-sdk/platforms \
    && unzip -q /tmp/platform.zip -d /opt/android-sdk/platforms \
    && curl --fail --location --retry 3 --max-time 180 https://dl.google.com/android/repository/build-tools_r35_linux.zip -o /tmp/build-tools.zip \
    && echo 'bd3a4966912eb8b30ed0d00b0cda6b6543b949d5ffe00bea54c04c81e1561d88  /tmp/build-tools.zip' | sha256sum --check - \
    && mkdir -p /opt/android-sdk/build-tools /tmp/build-tools \
    && unzip -q /tmp/build-tools.zip -d /tmp/build-tools \
    && mv /tmp/build-tools/android-15 /opt/android-sdk/build-tools/35.0.0 \
    && curl --fail --location --retry 3 --max-time 180 https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip -o /tmp/platform-tools.zip \
    && echo 'd230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1  /tmp/platform-tools.zip' | sha256sum --check - \
    && unzip -q /tmp/platform-tools.zip -d /opt/android-sdk \
    && rm /tmp/platform.zip /tmp/build-tools.zip /tmp/platform-tools.zip \
    && chown -R gradle:gradle /opt/android-sdk
USER gradle
WORKDIR /workspace

FROM toolchain AS build
COPY --chown=gradle:gradle android /workspace/android
COPY --chown=gradle:gradle tests/android /workspace/tests/android
WORKDIR /workspace/android
RUN gradle --no-daemon --max-workers=1 --console=plain :app:testDebugUnitTest :app:lintDebug :app:assembleDebug :app:assembleRelease

FROM scratch AS artifacts
COPY --from=build /workspace/android/app/build/outputs/apk/debug/app-debug.apk /ballnews-2.0.0-dev.apk
COPY --from=build /workspace/android/app/build/outputs/apk/release/app-release-unsigned.apk /ballnews-2.0.0-dev-unsigned.apk
