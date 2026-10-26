#!/usr/bin/env bash
# Host wrapper performs Docker/Git/file management only. All build and package tools run in Docker.
set -euo pipefail
android_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname -- "$android_dir")"
build_container="ai-football-android-candidate-$$"
output_dir="$android_dir/build/artifacts"
cleanup() { docker rm -f "$build_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mkdir -p "$output_dir"
docker build --target toolchain -f "$android_dir/Dockerfile" -t ai-football-android-toolchain:v2.0.0-dev "$project_dir"
docker create --name "$build_container" --cpus=1 --memory=1536m \
  -e GRADLE_USER_HOME=/workspace/gradle-cache \
  -v "$android_dir:/source/android:ro" -v "$project_dir/tests/android:/source/tests/android:ro" \
  --entrypoint sh ai-football-android-toolchain:v2.0.0-dev -c '
    set -eu
    mkdir -p /workspace/frozen/android/app /workspace/frozen/tests
    cp /source/android/build.gradle.kts /source/android/settings.gradle.kts /source/android/gradle.properties /workspace/frozen/android/
    cp -a /source/android/gradle /workspace/frozen/android/gradle
    cp /source/android/app/build.gradle.kts /source/android/app/gradle.lockfile /source/android/app/proguard-rules.pro /workspace/frozen/android/app/
    cp -a /source/android/app/src /workspace/frozen/android/app/src
    cp -a /source/tests/android /workspace/frozen/tests/android
    cd /workspace/frozen
    find android tests/android -type f -print0 | sort -z | xargs -0 sha256sum > source-sha256.txt
    cd android
    gradle --no-daemon --max-workers=1 --console=plain :app:testDebugUnitTest :app:lintDebug :app:assembleDebug :app:assembleRelease
  ' >/dev/null
docker start -a "$build_container"
container_exit="$(docker inspect --format '{{.State.ExitCode}}' "$build_container")"
if [[ "$container_exit" != 0 ]]; then exit "$container_exit"; fi
docker cp "$build_container:/workspace/frozen/android/app/build/outputs/apk/debug/app-debug.apk" "$output_dir/ballnews-2.0.0-dev-debug.apk"
docker cp "$build_container:/workspace/frozen/android/app/build/outputs/apk/release/app-release-unsigned.apk" "$output_dir/ballnews-2.0.0-dev-unsigned.apk"
docker cp "$build_container:/workspace/frozen/source-sha256.txt" "$output_dir/source-sha256.txt"
docker cp "$build_container:/workspace/frozen/android/app/build/reports/lint-results-debug.xml" "$output_dir/lint-results-debug.xml"
docker cp "$build_container:/workspace/frozen/android/app/build/test-results/testDebugUnitTest" "$output_dir/unit-results"
git -C "$project_dir" rev-parse HEAD > "$output_dir/source-parent-commit.txt"
git -C "$project_dir" status --porcelain -- android tests/android > "$output_dir/source-worktree-status.txt"
docker run --rm --cpus=0.5 --memory=256m -v "$output_dir:/artifacts:ro" \
  --entrypoint sh ai-football-android-toolchain:v2.0.0-dev -c '
    set -eu
    apksigner verify --verbose --print-certs /artifacts/ballnews-2.0.0-dev-debug.apk
    aapt dump badging /artifacts/ballnews-2.0.0-dev-unsigned.apk
    sha256sum /artifacts/ballnews-2.0.0-dev-debug.apk /artifacts/ballnews-2.0.0-dev-unsigned.apk
  ' > "$output_dir/package-verification.txt"
printf 'Development artifacts: %s\n' "$output_dir"
