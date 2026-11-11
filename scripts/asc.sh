#!/bin/sh
# asc.sh calls the App Store Connect API and prints the JSON reply:
#
#   ASC_KEY_ID=<key id> ASC_ISSUER_ID=<issuer id> scripts/asc.sh GET "/v1/apps?filter[bundleId]=com.kadaliao.steer"
#   ... scripts/asc.sh POST /v1/betaGroups/<id>/relationships/builds '{"data": [...]}'
#
# It uses the same API key as testflight.sh, and needs only the openssl and
# curl that come with macOS. When Apple answers with an error, it prints the
# reply to stderr and fails.
set -eu
: "${ASC_KEY_ID:?set ASC_KEY_ID to the App Store Connect API key ID}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID to the API key issuer ID}"
key=${ASC_KEY_PATH:-$HOME/.appstoreconnect/private_keys/AuthKey_$ASC_KEY_ID.p8}
[ -f "$key" ] || { echo "no API key at $key" >&2; exit 1; }
[ $# -ge 2 ] || { echo "usage: $0 METHOD PATH [JSON]" >&2; exit 2; }

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# The token is an ES256 JWT. openssl signs in DER; a JWT wants the two
# 32-byte integers r and s side by side.
now=$(date +%s)
head=$(printf '{"alg":"ES256","kid":"%s","typ":"JWT"}' "$ASC_KEY_ID" | b64url)
body=$(printf '{"iss":"%s","iat":%d,"exp":%d,"aud":"appstoreconnect-v1"}' "$ASC_ISSUER_ID" "$now" $((now + 600)) | b64url)
sig=$(printf %s "$head.$body" | openssl dgst -sha256 -sign "$key" | openssl asn1parse -inform DER |
	awk -F: '/INTEGER/ { n = $NF; sub(/^(00)+/, "", n); while (length(n) < 64) n = "0" n; printf "%s", n }' |
	xxd -r -p | b64url)

method=$1 url=https://api.appstoreconnect.apple.com$2
if [ $# -ge 3 ]; then
	set -- -H 'Content-Type: application/json' -d "$3"
else
	set --
fi
# -g: paths like ?fields[builds]=version use brackets literally.
reply=$(curl -gsS --fail-with-body -X "$method" -H "Authorization: Bearer $head.$body.$sig" "$@" "$url") ||
	{ printf '%s\n' "$reply" >&2; exit 1; }
printf '%s\n' "$reply"
