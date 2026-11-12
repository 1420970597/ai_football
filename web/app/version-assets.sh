#!/bin/sh
# One content version for the entire module graph, CSS and images. Build only.
set -eu
export LC_ALL=C
asset_root=${1:-/usr/share/nginx/html}
test -f "$asset_root/index.html"
test -d "$asset_root/assets"
if find "$asset_root/assets" -type d -name 'v*' | grep -q .; then
    echo 'asset versioning requires a fresh build directory' >&2
    exit 1
fi
# Stage outside assets so the copy cannot include itself.
asset_stage=$(mktemp -d)
trap 'rm -rf "$asset_stage"' EXIT INT TERM
sha256sum < "$0" > "$asset_stage/manifest"
(
    cd "$asset_root"
    sha256sum index.html >> "$asset_stage/manifest"
    find assets -type f > "$asset_stage/files"
    sort "$asset_stage/files" > "$asset_stage/sorted"
    while IFS= read -r asset_file; do
        sha256sum "$asset_file" >> "$asset_stage/manifest"
    done < "$asset_stage/sorted"
)
asset_version=$(sha256sum "$asset_stage/manifest" | cut -d ' ' -f 1)
asset_prefix="/assets/v$asset_version/"
rm "$asset_stage/manifest" "$asset_stage/files" "$asset_stage/sorted"
cp -R "$asset_root/assets/." "$asset_stage/"
find "$asset_stage" -type f \( -name '*.js' -o -name '*.css' \) | while IFS= read -r asset_file; do
    sed -i "s|/assets/|$asset_prefix|g" "$asset_file"
done
sed -i "s|/assets/|$asset_prefix|g" "$asset_root/index.html"
# mktemp is private (0700); nginx's unprivileged workers must traverse it.
chmod -R a+rX "$asset_stage"
chmod a+r "$asset_root/index.html"
mv "$asset_stage" "$asset_root/assets/v$asset_version"
trap - EXIT INT TERM
printf 'asset_version=%s\n' "$asset_version"
