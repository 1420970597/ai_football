#!/bin/sh
# Runs in the pinned nginx image, against disposable copies only.
set -eu
source_root=/source/web/app
make_root() {
    root="/evidence/builds/$1"
    mkdir -p "$root/assets"
    cp "$source_root/index.html" "$root/"
    for file in app.css app.js ui.js admin.js identity.js account-history.js qrcode.js qrcode.LICENSE; do
        cp "$source_root/$file" "$root/assets/"
    done
    cp -R "$source_root/cc-reference" "$root/assets/"
}
version() { sed -n 's|.*href="/assets/v\([0-9a-f]*\)/app.css".*|\1|p' "$1/index.html"; }
for kind in first repeat css module image html; do
    make_root "$kind"
    case "$kind" in
        css) printf '\n/* isolated version-change probe */\n' >> "$root/assets/app.css" ;;
        module) printf '\n// isolated version-change probe\n' >> "$root/assets/ui.js" ;;
        image) printf '\n' >> "$root/assets/cc-reference/home_match_vs_icon.png" ;;
        html) printf '\n<!-- isolated version-change probe -->\n' >> "$root/index.html" ;;
    esac
    sh "$source_root/version-assets.sh" "$root"
    version "$root" > "/evidence/$kind.version"
done
first=$(cat /evidence/first.version)
test "${#first}" = 64
test "$first" = "$(cat /evidence/repeat.version)"
for kind in css module image html; do
    test "$first" != "$(cat "/evidence/$kind.version")"
done
if sh "$source_root/version-assets.sh" /evidence/builds/first > /evidence/double-run.log 2>&1; then
    echo 'FAIL: in-place second transformation must be rejected' >&2
    exit 1
fi
printf 'PASS deterministic build; CSS/module/image/HTML invalidate group; double-run rejected\n'
