#!/usr/bin/env bash
# Host performs Docker/Git/file management only; signing secrets remain mounted files.
set -euo pipefail
task_android=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
task_root=$(dirname -- "$task_android")
task_version=${ANDROID_RELEASE_VERSION:?Set an explicit semantic version or numbered rc version}
task_code=${ANDROID_RELEASE_VERSION_CODE:?Set the reviewed positive Android version code}
task_signing=${ANDROID_SIGNING_DIRECTORY:?Set the project-local private signing directory}
task_certificate=${ANDROID_SIGNER_SHA256:?Set the externally recorded signer certificate SHA-256}
task_commit=$(git -C "$task_root" rev-parse HEAD)
task_container=ai-football-android-signed-$$
task_output="$task_android/build/signed-artifacts"
if [[ -e "$task_output" ]]; then
  echo 'Signed output already exists; preserve or remove the previous candidate explicitly before rebuilding.' >&2; exit 2
fi
task_python=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
cleanup() { docker rm -fv "$task_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# Keep fixture/default endpoints out of every signed build, just as in the dev entrypoint.
if rg -n --glob '*.java' --glob '*.kt' --glob '*.xml' \
  'http://|localhost|127\.0\.0\.1|0\.0\.0\.0|[A-Za-z0-9.-]+\.invalid|[A-Za-z0-9.-]+\.test' \
  "$task_android/app/src/main" | grep -v 'xmlns:android='; then
  echo 'Android application source contains a default or fixture service URL' >&2; exit 2
fi
case "$(realpath -- "$task_signing")" in "$task_root"/.runtime/*) ;; *) echo 'Signing material must remain in project-local .runtime.' >&2; exit 2;; esac
task_signing=$(realpath -- "$task_signing")
if [[ ! -d "$task_signing" || $(stat -c '%a' "$task_signing") != 700 ]]; then
  echo 'Signing directory must be private mode 0700.' >&2; exit 2
fi
for task_secret in signing.jks store-password key-password key-alias; do
  task_path="$task_signing/$task_secret"
  if [[ ! -f "$task_path" || -L "$task_path" || $(stat -c '%a' "$task_path") != 600 ]]; then
    echo 'Each required signing input must be a private 0600 regular file.' >&2; exit 2
  fi
done
docker run --rm --cpus=.25 --memory=128m --network none -v "$task_root:/source:ro" -w /source "$task_python" \
  python infra/android/release_policy.py --version "$task_version" --version-code "$task_code" --commit "$task_commit" >/dev/null
mkdir -p "$task_output"
docker build --memory 768m --cpu-period 100000 --cpu-quota 50000 --target toolchain \
  -f "$task_android/Dockerfile" -t ai-football-android-toolchain:v2.0.0-dev "$task_root"
docker create --name "$task_container" --cpus=1 --memory=1536m --pids-limit=256 \
  --user 0 --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  -e GRADLE_USER_HOME=/workspace/gradle-cache -e ANDROID_RELEASE_VERSION="$task_version" -e ANDROID_RELEASE_VERSION_CODE="$task_code" \
  -v "$task_android:/source/android:ro" -v "$task_root/tests/android:/source/tests/android:ro" \
  -v "$task_signing:/run/android-signing:ro" --entrypoint sh ai-football-android-toolchain:v2.0.0-dev -c '
    set -eu
    mkdir -p /workspace/frozen/android/app /workspace/frozen/tests /workspace/artifacts
    cp /source/android/build.gradle.kts /source/android/settings.gradle.kts /source/android/gradle.properties /workspace/frozen/android/
    cp -a /source/android/gradle /workspace/frozen/android/gradle
    cp /source/android/app/build.gradle.kts /source/android/app/gradle.lockfile /source/android/app/proguard-rules.pro /workspace/frozen/android/app/
    cp -a /source/android/app/src /workspace/frozen/android/app/src
    cp -a /source/tests/android /workspace/frozen/tests/android
    cd /workspace/frozen
    find android tests/android -type f -print0 | sort -z | xargs -0 sha256sum > /workspace/artifacts/source-sha256.txt
    cd android
    gradle --no-daemon --max-workers=1 --console=plain -PplatformVersionName="$ANDROID_RELEASE_VERSION" -PplatformVersionCode="$ANDROID_RELEASE_VERSION_CODE" :app:testDebugUnitTest :app:lintRelease :app:assembleRelease
    apksigner sign --v4-signing-enabled false --ks /run/android-signing/signing.jks --ks-key-alias "$(cat /run/android-signing/key-alias)" --ks-pass file:/run/android-signing/store-password --key-pass file:/run/android-signing/key-password --out /workspace/artifacts/app-signed.apk app/build/outputs/apk/release/app-release-unsigned.apk
    apksigner verify --verbose --print-certs /workspace/artifacts/app-signed.apk > /workspace/artifacts/package-verification.txt
    aapt dump badging /workspace/artifacts/app-signed.apk >> /workspace/artifacts/package-verification.txt
    cp app/build/reports/lint-results-release.xml /workspace/artifacts/
    cp -a app/build/test-results/testDebugUnitTest /workspace/artifacts/unit-results
  ' >/dev/null
docker start -a "$task_container"
task_exit=$(docker inspect --format '{{.State.ExitCode}}' "$task_container")
if [[ $task_exit != 0 ]]; then exit "$task_exit"; fi
docker cp "$task_container:/workspace/artifacts/." "$task_output/"
mv "$task_output/app-signed.apk" "$task_output/ballnews-$task_version-signed-candidate.apk"
printf '%s\n' "$task_commit" > "$task_output/source-parent-commit.txt"
git -C "$task_root" status --porcelain > "$task_output/source-worktree-status.txt"
docker run --rm --cpus=.25 --memory=128m --network none -v "$task_root:/source:ro" -w /source "$task_python" \
  python infra/android/release_policy.py --version "$task_version" --version-code "$task_code" --commit "$task_commit" \
  --package-output android/build/signed-artifacts/package-verification.txt --certificate-sha256 "$task_certificate" \
  --apk "android/build/signed-artifacts/ballnews-$task_version-signed-candidate.apk" \
  --worktree-status android/build/signed-artifacts/source-worktree-status.txt \
  > "$task_output/signing-manifest.json"
printf 'Verified signed candidate: %s\n' "$task_output"
