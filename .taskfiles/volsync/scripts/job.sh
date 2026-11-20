#!/usr/bin/env bash
# Wait for a one-shot Job to finish, print its logs, and delete it when it succeeded.
# A failed Job is left in place (its ttlSecondsAfterFinished cleans it up) so it can be inspected.
#
# Usage: job.sh <job> <namespace> <context> <container> [timeout-seconds]
#
# This replaces wait.sh, which waited for the pod to be Pending and hung when a short Job
# went past Pending before the first poll.

set -uo pipefail

JOB="${1:?job name required}"
NAMESPACE="${2:?namespace required}"
CLUSTER="${3:?cluster (kube context) required}"
CONTAINER="${4:?container name required}"
TIMEOUT="${5:-600}"

k() { kubectl --context "${CLUSTER}" -n "${NAMESPACE}" "$@"; }

result=""
end=$((SECONDS + TIMEOUT))
while [ "${SECONDS}" -lt "${end}" ]; do
    # Complete/Failed are the final conditions; newer Kubernetes also sets
    # SuccessCriteriaMet/FailureTarget first, which are ignored here.
    result="$(k get job "${JOB}" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{"\n"}{end}' 2>/dev/null \
        | grep -xE 'Complete|Failed' | head -n1)"
    [ -n "${result}" ] && break
    sleep 2
done

k logs "job/${JOB}" --container "${CONTAINER}" || true

case "${result}" in
    Complete)
        k delete job "${JOB}" >/dev/null
        ;;
    Failed)
        echo "Job ${JOB} failed (left in namespace ${NAMESPACE} for inspection)" >&2
        exit 1
        ;;
    *)
        echo "Timed out after ${TIMEOUT}s waiting for job ${JOB}" >&2
        exit 1
        ;;
esac
