#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "${0}")/lib/common.sh"

export LOG_LEVEL="${LOG_LEVEL:-info}"

# See kubernetes/apps/external-secrets/bitwarden/app/clustersecretstore.yaml
readonly BITWARDEN_PROJECT_ID="b404372f-38ee-48f0-b66a-b30c00ab0414"

# The k8s ExternalSecret is named "wireguard" but it (along with the qbittorrent
# and qbittorrent-gluetun ExternalSecrets) actually extracts from this Bitwarden
# Secrets Manager item. See kubernetes/apps/downloads/qbittorrent/app/externalsecret.yaml
readonly BITWARDEN_SECRET_KEY="gluetun"

# Same machine-access-token the in-cluster ClusterSecretStore uses, see
# kubernetes/apps/external-secrets/bitwarden/app/clustersecretstore.yaml
readonly BITWARDEN_TOKEN_NAMESPACE="external-secrets"
readonly BITWARDEN_TOKEN_SECRET="bitwarden-secret"
readonly BITWARDEN_TOKEN_KEY="machine-access-token"

function fetch_cluster_access_token() {
    kubectl -n "${BITWARDEN_TOKEN_NAMESPACE}" get secret "${BITWARDEN_TOKEN_SECRET}" \
        -o jsonpath="{.data.${BITWARDEN_TOKEN_KEY}}" | base64 -d
}

# The k8s ExternalSecret/Secret consuming the "gluetun" Bitwarden item, see
# kubernetes/apps/downloads/qbittorrent/app/externalsecret.yaml. qbittorrent's
# Deployment already has reloader.stakater.com/auto: 'true' and references this
# Secret, so once it's updated Reloader restarts the pod on its own.
readonly WIREGUARD_EXTERNALSECRET_NAMESPACE="downloads"
readonly WIREGUARD_EXTERNALSECRET_NAME="wireguard"

function usage() {
    cat <<EOF
Usage: $(basename "${0}") [--apply] <path-to-wg.conf>

Updates the WIREGUARD_* fields on the Bitwarden Secrets Manager "${BITWARDEN_SECRET_KEY}"
secret from a WireGuard client config file, leaving its other fields
(VPN_SERVICE_PROVIDER, HEALTH_TARGET_ADDRESS, GLUETUN_API_KEY) untouched.

Without --apply, prints a diff and exits without writing anything.

Requires: bws CLI (mise install), jq, kubectl (with access to the cluster).
Uses BWS_ACCESS_TOKEN from the environment if set, otherwise fetches the
machine-access-token the cluster's own ClusterSecretStore uses from
${BITWARDEN_TOKEN_NAMESPACE}/${BITWARDEN_TOKEN_SECRET}.
EOF
}

function parse_wg_conf() {
    local conf="${1}"

    WG_PRIVATE_KEY=$(grep -m1 -E '^PrivateKey' "${conf}" | cut -d= -f2- | xargs)
    WG_ADDRESS=$(grep -m1 -E '^Address' "${conf}" | cut -d= -f2- | xargs)
    WG_PUBLIC_KEY=$(grep -m1 -E '^PublicKey' "${conf}" | cut -d= -f2- | xargs)

    local endpoint
    endpoint=$(grep -m1 -E '^Endpoint' "${conf}" | cut -d= -f2- | xargs)
    WG_ENDPOINT_HOST="${endpoint%:*}"

    if [[ -z "${WG_PRIVATE_KEY}" || -z "${WG_ADDRESS}" || -z "${WG_PUBLIC_KEY}" || -z "${WG_ENDPOINT_HOST}" ]]; then
        log error "Could not parse required fields from conf" "path=${conf}"
    fi
}

function resolve_endpoint_ip() {
    local host="${1}"

    if [[ "${host}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "${host}"
        return
    fi

    local ip=""
    if command -v dig &>/dev/null; then
        ip=$(dig +short A "${host}" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1)
    fi
    if [[ -z "${ip}" ]] && command -v getent &>/dev/null; then
        ip=$(getent ahostsv4 "${host}" | awk '{print $1; exit}')
    fi

    if [[ -z "${ip}" ]]; then
        log error "Could not resolve endpoint host to an IP" "host=${host}"
    fi
    echo "${ip}"
}

function main() {
    local apply="false"
    local conf=""

    while [[ $# -gt 0 ]]; do
        case "${1}" in
        --apply)
            apply="true"
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            conf="${1}"
            shift
            ;;
        esac
    done

    if [[ -z "${conf}" ]]; then
        usage
        log error "Missing path to wg conf file"
    fi
    if [[ ! -f "${conf}" ]]; then
        log error "File not found" "path=${conf}"
    fi

    check_cli bws jq kubectl

    if [[ -z "${BWS_ACCESS_TOKEN-}" ]]; then
        log info "BWS_ACCESS_TOKEN not set, fetching the in-cluster machine-access-token" \
            "namespace=${BITWARDEN_TOKEN_NAMESPACE}" "secret=${BITWARDEN_TOKEN_SECRET}"
        BWS_ACCESS_TOKEN=$(fetch_cluster_access_token)
        if [[ -z "${BWS_ACCESS_TOKEN}" ]]; then
            log error "Could not fetch machine-access-token from the cluster"
        fi
        export BWS_ACCESS_TOKEN
    fi

    parse_wg_conf "${conf}"

    log info "Resolving WireGuard endpoint" "host=${WG_ENDPOINT_HOST}"
    local endpoint_ip
    endpoint_ip=$(resolve_endpoint_ip "${WG_ENDPOINT_HOST}")
    log info "Resolved endpoint" "ip=${endpoint_ip}"

    log debug "Looking up secret" "key=${BITWARDEN_SECRET_KEY}" "project=${BITWARDEN_PROJECT_ID}"
    local secret_id
    secret_id=$(bws secret list "${BITWARDEN_PROJECT_ID}" | jq -r --arg key "${BITWARDEN_SECRET_KEY}" '.[] | select(.key == $key) | .id')
    if [[ -z "${secret_id}" || "${secret_id}" == "null" ]]; then
        log error "Could not find secret in Bitwarden project" "key=${BITWARDEN_SECRET_KEY}"
    fi

    # The secret's raw value is flat "KEY: value" lines, not JSON.
    local current_value
    current_value=$(bws secret get "${secret_id}" | jq -r '.value')

    local -A fields
    local -a order=()
    local line key value
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        key="${line%%: *}"
        value="${line#*: }"
        fields["${key}"]="${value}"
        order+=("${key}")
    done <<<"${current_value}"

    fields["WIREGUARD_PUBLIC_KEY"]="${WG_PUBLIC_KEY}"
    fields["WIREGUARD_PRIVATE_KEY"]="${WG_PRIVATE_KEY}"
    fields["WIREGUARD_ADDRESSES"]="${WG_ADDRESS}"
    fields["WIREGUARD_ENDPOINT_IP"]="${endpoint_ip}"

    local new_value=""
    for key in "${order[@]}"; do
        new_value+="${key}: ${fields[${key}]}"$'\n'
    done
    new_value="${new_value%$'\n'}"

    log info "Diff of ${BITWARDEN_SECRET_KEY} secret (private key masked):"
    diff \
        <(echo "${current_value}" | sed -E 's/^(WIREGUARD_PRIVATE_KEY: ).*/\1***/') \
        <(echo "${new_value}" | sed -E 's/^(WIREGUARD_PRIVATE_KEY: ).*/\1***/') || true

    if [[ "${apply}" != "true" ]]; then
        log warn "Dry run only, re-run with --apply to write to Bitwarden"
        exit 0
    fi

    bws secret edit "${secret_id}" --value "${new_value}" >/dev/null
    log info "Updated ${BITWARDEN_SECRET_KEY} secret in Bitwarden" "id=${secret_id}"

    kubectl -n "${WIREGUARD_EXTERNALSECRET_NAMESPACE}" annotate externalsecret "${WIREGUARD_EXTERNALSECRET_NAME}" \
        force-sync="$(date +%s)" --overwrite >/dev/null
    log info "Forced ExternalSecret to resync from Bitwarden" \
        "namespace=${WIREGUARD_EXTERNALSECRET_NAMESPACE}" "externalsecret=${WIREGUARD_EXTERNALSECRET_NAME}"
    log info "qbittorrent's Deployment has reloader.stakater.com/auto: 'true' and references this Secret, so it will restart automatically once the Secret updates"
}

main "$@"
