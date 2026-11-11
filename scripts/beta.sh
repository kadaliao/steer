#!/bin/sh
# beta.sh hands an uploaded build to the internal testing group 「内部测试」.
# Internal testers are the team's App Store Connect users; they get every
# build as soon as Apple has processed it, with no TestFlight review.
#
#   ASC_KEY_ID=<key id> ASC_ISSUER_ID=<issuer id> scripts/beta.sh <version> <build number>
#
# testflight.sh runs it after every upload. It waits for processing, writes
# What to Test (NOTES, or a default line) and, the first time, creates the
# group and adds the team's users to it. Running it again is safe.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
asc=$here/scripts/asc.sh
app=$("$here/scripts/app-id.sh")
group_name=内部测试
version=${1:?usage: beta.sh <version> <build number>}
number=${2:?usage: beta.sh <version> <build number>}

# Replies go into a variable before jq reads them, so that a failed request
# stops the script: in a pipe, only jq's status would count.
groups=$("$asc" GET "/v1/apps/$app/betaGroups?fields[betaGroups]=name&limit=200")
group_id=$(printf %s "$groups" | jq -r --arg n "$group_name" '.data[] | select(.attributes.name == $n) | .id')
if [ -z "$group_id" ]; then
	created=$("$asc" POST /v1/betaGroups "$(jq -nc --arg n "$group_name" --arg a "$app" \
		'{data: {type: "betaGroups", attributes: {name: $n, isInternalGroup: true, hasAccessToAllBuilds: true, feedbackEnabled: true}, relationships: {app: {data: {type: "apps", id: $a}}}}}')")
	group_id=$(printf %s "$created" | jq -r .data.id)
fi

# The first time, every App Store Connect user of the team joins the group.
testers=$("$asc" GET "/v1/betaGroups/$group_id/betaTesters?limit=1")
if [ "$(printf %s "$testers" | jq '.data | length')" = 0 ]; then
	users=$("$asc" GET "/v1/users?fields[users]=username,firstName,lastName&limit=200")
	printf %s "$users" | jq -c '.data[].attributes' | while read -r user; do
		"$asc" POST /v1/betaTesters "$(printf %s "$user" | jq -c --arg g "$group_id" \
			'{data: {type: "betaTesters", attributes: {email: .username, firstName: .firstName, lastName: .lastName}, relationships: {betaGroups: {data: [{type: "betaGroups", id: $g}]}}}}')" >/dev/null
	done
	echo "已建好「${group_name}」，团队里的 App Store Connect 用户都在里面"
fi

# A build shows up in the API a minute or so after the upload, then takes 5
# to 15 minutes to process. A failed request counts as not processed yet.
printf '等 App Store Connect 处理 %s (%s)' "$version" "$number"
tries=0
while :; do
	build=$("$asc" GET "/v1/builds?filter[app]=$app&filter[version]=$number&filter[preReleaseVersion.version]=$version&fields[builds]=processingState" 2>/dev/null |
		jq -c '.data[0] // empty')
	state=$(printf %s "$build" | jq -r '.attributes.processingState // empty')
	case $state in
	VALID) break ;;
	FAILED | INVALID) echo; echo "构建处理失败（${state}），到 App Store Connect 看看原因" >&2; exit 1 ;;
	esac
	tries=$((tries + 1))
	[ $tries -le 120 ] || { echo; echo "等了一小时还没处理完，过会儿再运行 scripts/beta.sh $version $number" >&2; exit 1; }
	printf .
	sleep 30
done
echo
build_id=$(printf %s "$build" | jq -r .id)

notes=${NOTES:-"连上你的电脑，试试新建会话、审批、提问卡片和断线重连。遇到问题可以在 TestFlight 里截屏反馈。"}
locs=$("$asc" GET "/v1/builds/$build_id/betaBuildLocalizations")
loc=$(printf %s "$locs" | jq -r '.data[] | select(.attributes.locale == "zh-Hans") | .id')
if [ -n "$loc" ]; then
	"$asc" PATCH "/v1/betaBuildLocalizations/$loc" "$(jq -nc --arg id "$loc" --arg t "$notes" \
		'{data: {type: "betaBuildLocalizations", id: $id, attributes: {whatsNew: $t}}}')" >/dev/null
else
	"$asc" POST /v1/betaBuildLocalizations "$(jq -nc --arg id "$build_id" --arg t "$notes" \
		'{data: {type: "betaBuildLocalizations", attributes: {locale: "zh-Hans", whatsNew: $t}, relationships: {build: {data: {type: "builds", id: $id}}}}}')" >/dev/null
fi
echo "处理完了。「${group_name}」里的人打开 TestFlight 就能装 $version ($number)"
