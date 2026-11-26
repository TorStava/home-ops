#!/bin/sh
# Garage's setup (the `setup` container of the turvis-garage pod; local dev: turvis infra/garage/setup.sh, ADR-0012). It
# runs on every start of the pod and is safe to repeat:
#   1. the single-node layout (zone dc1) if none is applied yet;
#   2. the app's key (S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY, secret turvis-s3), imported; a key of that id with another
#      secret stops the setup with what to do, so the app never runs on a stale secret (Garage cannot change a key's
#      secret, nor import a deleted key id again);
#   3. the bucket S3_BUCKET, with read and write for that key on that bucket only: not owner, not allowed to make buckets,
#      so the app never holds an admin key. In Garage "read" includes list and "write" includes delete.
# Through Garage's admin API (v2) with curl: the Garage image has no shell, and the curl image has no JSON tool. So each
# answer is read for one field whose shape is known, and checked (below); an answer of another shape stops the setup.
# Buckets are private in Garage unless a website is turned on for them; nothing here turns one on.
set -eu
# GetKeyInfo's answer (it holds the key's secret) goes, however the setup ends.
trap 'rm -f /tmp/key.json' EXIT
ADMIN="${GARAGE_ADMIN_URL:-http://127.0.0.1:3903}"
AUTH="Authorization: Bearer ${GARAGE_ADMIN_TOKEN}"
api() { curl -sS -f -H "$AUTH" -H "Content-Type: application/json" "$@"; }
fail() { echo "garage-setup: $*" >&2; exit 1; }
# Every value of a JSON string field named $1 in the answer on stdin, one per line (the answers are compact JSON).
values() { tr ',{}[' '\n\n\n\n' | sed -n "s/^ *\"$1\" *: *\"\\([^\"]*\\)\" *$/\\1/p"; }
# The one value of field $1, which must be 64 hex digits (a node or bucket id); anything else stops the setup.
one_id() {
  v=$(values "$1")
  [ "$(printf '%s\n' "$v" | grep -c .)" = 1 ] || fail "expected one \"$1\" in the answer of $2, got: $(printf '%s' "$v" | tr '\n' ' ')"
  printf '%s' "$v" | grep -Eq '^[0-9a-f]{64}$' || fail "\"$1\" in the answer of $2 is not a Garage id"
  printf '%s' "$v"
}

i=0
until curl -s -o /dev/null "$ADMIN/health"; do
  i=$((i + 1)); [ "$i" -lt 120 ] || fail "Garage did not answer"; sleep 1
done

# The admin token first: every call below needs it, and a wrong one should say so, not fail on the first curl.
status=$(curl -sS -o /dev/null -w '%{http_code}' -H "$AUTH" "$ADMIN/v2/GetClusterStatus") || fail "could not reach Garage's admin API"
case "$status" in
  200) ;;
  401 | 403) fail "the admin token is wrong (GARAGE_ADMIN_TOKEN): Garage answered $status" ;;
  *) fail "GetClusterStatus answered HTTP $status" ;;
esac

# 1. Layout. GetClusterLayout's top-level "version" is 0 until a layout is applied. GetClusterStatus lists the nodes, each
#    with its "id"; this is one node, so exactly one "id".
version=$(api "$ADMIN/v2/GetClusterLayout" | tr ',{}[' '\n\n\n\n' | sed -n 's/^ *"version" *: *\([0-9][0-9]*\) *$/\1/p')
[ "$(printf '%s\n' "$version" | grep -c .)" = 1 ] || fail "expected one \"version\" in GetClusterLayout, got: $version"
if [ "$version" = 0 ]; then
  node=$(api "$ADMIN/v2/GetClusterStatus" | one_id id GetClusterStatus)
  api -X POST "$ADMIN/v2/UpdateClusterLayout" \
    -d "{\"roles\":[{\"id\":\"$node\",\"zone\":\"dc1\",\"capacity\":${LAYOUT_CAPACITY_BYTES:-1073741824},\"tags\":[\"turvis\"]}]}" >/dev/null
  api -X POST "$ADMIN/v2/ApplyClusterLayout" -d '{"version":1}' >/dev/null
  echo "garage-setup: layout applied"
fi

# 2. The app's key. GetKeyInfo with showSecretKey has one "secretAccessKey".
import_key() {
  api -X POST "$ADMIN/v2/ImportKey" \
    -d "{\"accessKeyId\":\"${S3_ACCESS_KEY_ID}\",\"secretAccessKey\":\"${S3_SECRET_ACCESS_KEY}\",\"name\":\"turvis-app\"}" >/dev/null
}
# The answer's status decides: 200 the key is there, 404 it is not (import it), 401/403 the admin token is wrong, anything
# else stops the setup naming the status.
info=$(curl -sS -o /tmp/key.json -w '%{http_code}' -H "$AUTH" "$ADMIN/v2/GetKeyInfo?id=${S3_ACCESS_KEY_ID}&showSecretKey=true") ||
  fail "could not reach Garage's admin API for GetKeyInfo"
case "$info" in
  200)
    secret=$(values secretAccessKey </tmp/key.json)
    [ -n "$secret" ] || fail "GetKeyInfo did not show the key's secret"
    # Garage cannot give a key id a new secret, nor import a deleted id again (409): stop and say what to do.
    [ "$secret" = "$S3_SECRET_ACCESS_KEY" ] ||
      fail "the key ${S3_ACCESS_KEY_ID} is in Garage with another secret than S3_SECRET_ACCESS_KEY (secret turvis-s3). Demo data only: delete the volume claim turvis-garage and the secret turvis-s3 and let Flux make both again, or put the old secret back."
    ;;
  404)
    import_key || fail "could not import the key ${S3_ACCESS_KEY_ID} (an id Garage has deleted cannot be imported again: make a new key id)"
    echo "garage-setup: app key imported"
    ;;
  401 | 403) fail "the admin token is wrong (GARAGE_ADMIN_TOKEN): Garage answered $info to GetKeyInfo" ;;
  *) fail "GetKeyInfo answered HTTP $info" ;;
esac

# 3. The bucket. GetBucketInfo's only "id" is the bucket's own (its keys are listed by "accessKeyId"); the id is checked
#    against the bucket's alias before it is used.
bucket="$S3_BUCKET"
if info=$(api "$ADMIN/v2/GetBucketInfo?globalAlias=$bucket" 2>/dev/null); then
  id=$(printf '%s' "$info" | one_id id GetBucketInfo)
else
  id=$(api -X POST "$ADMIN/v2/CreateBucket" -d "{\"globalAlias\":\"$bucket\"}" | one_id id CreateBucket)
  echo "garage-setup: bucket $bucket made"
fi
api "$ADMIN/v2/GetBucketInfo?id=$id" | grep -q "\"$bucket\"" || fail "bucket id $id is not $bucket"
api -X POST "$ADMIN/v2/AllowBucketKey" \
  -d "{\"bucketId\":\"$id\",\"accessKeyId\":\"${S3_ACCESS_KEY_ID}\",\"permissions\":{\"read\":true,\"write\":true,\"owner\":false}}" >/dev/null
echo "garage-setup: ready"
