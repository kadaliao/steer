#!/bin/sh
# testflight.sh archives Steer, signs it for the App Store, uploads it to App
# Store Connect, then runs beta.sh to hand it to the internal testers.
#
#   ASC_KEY_ID=<key id> ASC_ISSUER_ID=<issuer id> scripts/testflight.sh
#   ... NOTES="这一版改了什么" scripts/testflight.sh
#
# The API key (~/.appstoreconnect/private_keys/AuthKey_<key id>.p8, or
# ASC_KEY_PATH) both signs and uploads: Xcode creates the certificate and
# profile it needs through it, so no account has to be signed in to Xcode.
#
# The build number is one above the highest one App Store Connect has, unless
# BUILD_NUMBER is set. The version is MARKETING_VERSION in project.yml.
set -eu
: "${ASC_KEY_ID:?set ASC_KEY_ID to the App Store Connect API key ID}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID to the API key issuer ID}"
here=$(cd "$(dirname "$0")/.." && pwd)
key=${ASC_KEY_PATH:-$HOME/.appstoreconnect/private_keys/AuthKey_$ASC_KEY_ID.p8}
out=$here/build/release
auth="-authenticationKeyPath $key -authenticationKeyID $ASC_KEY_ID -authenticationKeyIssuerID $ASC_ISSUER_ID"

app=$("$here/scripts/app-id.sh")
version=$(sed -n 's/^ *MARKETING_VERSION: *"\(.*\)"/\1/p' "$here/project.yml")
if [ -z "${BUILD_NUMBER:-}" ]; then
	builds=$("$here/scripts/asc.sh" GET "/v1/builds?filter[app]=$app&fields[builds]=version&limit=200")
	BUILD_NUMBER=$(printf %s "$builds" | jq '[.data[].attributes.version | tonumber] | max // 0 | . + 1')
fi
echo "Steer $version ($BUILD_NUMBER)"

(cd "$here" && xcodegen generate --quiet)
rm -rf "$out"
mkdir -p "$out"

echo "[1/3] 归档"
# shellcheck disable=SC2086 # auth is a list of words
xcodebuild archive -project "$here/Steer.xcodeproj" -scheme Steer -configuration Release \
	-destination 'generic/platform=iOS' -archivePath "$out/Steer.xcarchive" \
	-allowProvisioningUpdates $auth CURRENT_PROJECT_VERSION="$BUILD_NUMBER" -quiet

echo "[2/3] 导出"
cat >"$out/ExportOptions.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>app-store-connect</string>
	<key>signingStyle</key>
	<string>automatic</string>
	<key>teamID</key>
	<string>HMDAVG9B8J</string>
	<key>uploadSymbols</key>
	<true/>
	<key>manageAppVersionAndBuildNumber</key>
	<false/>
</dict>
</plist>
PLIST
# shellcheck disable=SC2086
xcodebuild -exportArchive -archivePath "$out/Steer.xcarchive" -exportPath "$out/export" \
	-exportOptionsPlist "$out/ExportOptions.plist" -allowProvisioningUpdates $auth -quiet

echo "[3/3] 上传"
xcrun altool --upload-app -f "$out/export/Steer.ipa" -t ios --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
echo "已上传 $version ($BUILD_NUMBER)"

echo
"$here/scripts/beta.sh" "$version" "$BUILD_NUMBER"
