#!/bin/sh
# Garage's setup (the `setup` container of the turvis-garage pod; local dev: turvis infra/garage/setup.sh, ADR-0012). It
# runs on every start of the pod and is safe to repeat:
#   1. the single-node layout (zone dc1) if none is applied yet;
#   2. the app's key (S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY, secret turvis-s3), imported; a key of that id with another
#      secret stops the setup with what to do, so the app never runs on a stale secret (Garage cannot change a key's
#      secret, nor import a deleted key id again);
#   3. the bucket S3_BUCKET, with read and write for that key on that bucket only: not owner, not allowed to make buckets,
#      so the app never holds an admin key. In Garage "read" includes list and "write" includes delete.
#   4. the backup key (BACKUP_ACCESS_KEY_ID / BACKUP_SECRET_ACCESS_KEY, secret turvis-garage-backup), imported the same way
#      as the app's (step 2), with read (and so list) on S3_BUCKET only: the nightly copy to R2 can neither write nor
#      delete a bild.
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

# 2. The keys. ensure_key <id> <secret> <name> <where the secret lives>: GetKeyInfo with showSecretKey has one
#    "secretAccessKey". The answer's status decides: 200 the key is there (its secret must match), 404 it is not (import
#    it), 401/403 the admin token is wrong, anything else stops the setup naming the status.
ensure_key() {
  info=$(curl -sS -o /tmp/key.json -w '%{http_code}' -H "$AUTH" "$ADMIN/v2/GetKeyInfo?id=$1&showSecretKey=true") ||
    fail "could not reach Garage's admin API for GetKeyInfo"
  case "$info" in
    200)
      secret=$(values secretAccessKey </tmp/key.json)
      [ -n "$secret" ] || fail "GetKeyInfo did not show the secret of the key $1"
      # Garage cannot give a key id a new secret, nor import a deleted id again (409): stop and say what to do.
      [ "$secret" = "$2" ] ||
        fail "the key $1 ($3) is in Garage with another secret than the one in secret $4. Put the old secret back, or delete secret $4 so it is made again with a new key id (for the app key: demo data only, delete the volume claim turvis-garage and the secret turvis-s3 and let Flux make both again)."
      ;;
    404)
      api -X POST "$ADMIN/v2/ImportKey" -d "{\"accessKeyId\":\"$1\",\"secretAccessKey\":\"$2\",\"name\":\"$3\"}" >/dev/null ||
        fail "could not import the key $1 (an id Garage has deleted cannot be imported again: make a new key id)"
      echo "garage-setup: key $3 imported"
      ;;
    401 | 403) fail "the admin token is wrong (GARAGE_ADMIN_TOKEN): Garage answered $info to GetKeyInfo" ;;
    *) fail "GetKeyInfo answered HTTP $info" ;;
  esac
  rm -f /tmp/key.json
}
ensure_key "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY" turvis-app turvis-s3
ensure_key "$BACKUP_ACCESS_KEY_ID" "$BACKUP_SECRET_ACCESS_KEY" turvis-photos-backup turvis-garage-backup

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
# 4. The backup key (the nightly copy to R2, ./photos-backup): read on the bucket, which in Garage includes list, and
#    nothing else. AllowBucketKey only adds what is true, so DenyBucketKey takes write and owner away on every run: the key
#    stays read-only even if someone once gave it more.
api -X POST "$ADMIN/v2/AllowBucketKey" \
  -d "{\"bucketId\":\"$id\",\"accessKeyId\":\"${BACKUP_ACCESS_KEY_ID}\",\"permissions\":{\"read\":true,\"write\":false,\"owner\":false}}" >/dev/null
api -X POST "$ADMIN/v2/DenyBucketKey" \
  -d "{\"bucketId\":\"$id\",\"accessKeyId\":\"${BACKUP_ACCESS_KEY_ID}\",\"permissions\":{\"read\":false,\"write\":true,\"owner\":true}}" >/dev/null
echo "garage-setup: ready"
