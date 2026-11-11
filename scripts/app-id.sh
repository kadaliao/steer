#!/bin/sh
# app-id.sh prints Steer's App Store Connect app ID, looked up by bundle ID, or
# fails when there is no app record yet (see docs/releasing.md).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
reply=$("$here/scripts/asc.sh" GET "/v1/apps?filter[bundleId]=com.kadaliao.steer&fields[apps]=bundleId")
id=$(printf %s "$reply" | jq -r '.data[0].id // empty')
[ -n "$id" ] || { echo "App Store Connect 里还没有 com.kadaliao.steer 的 App 记录，先在网页上建，见 docs/releasing.md" >&2; exit 1; }
printf '%s\n' "$id"
