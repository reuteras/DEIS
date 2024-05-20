#!/usr/bin/env bash

set -eu
set -o pipefail

# shellcheck disable=SC1091
source "${BASH_SOURCE[0]%/*}"/lib.sh

# --------------------------------------------------------
# Users declarations

declare -A users_passwords
users_passwords=(
    [logstash_internal]="${LOGSTASH_INTERNAL_PASSWORD:-}"
    [kibana_system]="${KIBANA_SYSTEM_PASSWORD:-}"
    [metricbeat_internal]="${METRICBEAT_INTERNAL_PASSWORD:-}"
    [filebeat_internal]="${FILEBEAT_INTERNAL_PASSWORD:-}"
    [heartbeat_internal]="${HEARTBEAT_INTERNAL_PASSWORD:-}"
    [monitoring_internal]="${MONITORING_INTERNAL_PASSWORD:-}"
    [beats_system]="${BEATS_SYSTEM_PASSWORD=:-}"
)

declare -A users_roles
users_roles=(
    [logstash_internal]='logstash_writer'
    [metricbeat_internal]='metricbeat_writer'
    [filebeat_internal]='filebeat_writer'
    [heartbeat_internal]='heartbeat_writer'
    [monitoring_internal]='remote_monitoring_collector'
)

# --------------------------------------------------------
# Roles declarations

declare -A roles_files
roles_files=(
    [logstash_writer]='logstash_writer.json'
    [metricbeat_writer]='metricbeat_writer.json'
    [filebeat_writer]='filebeat_writer.json'
    [heartbeat_writer]='heartbeat_writer.json'
)

# --------------------------------------------------------

log 'Waiting for availability of Elasticsearch. This can take several minutes.'

declare -i exit_code=0
wait_for_elasticsearch || exit_code=$?

if ((exit_code)); then
    case $exit_code in
    6)
        suberr 'Could not resolve host. Is Elasticsearch running?'
        ;;
    7)
        suberr 'Failed to connect to host. Is Elasticsearch healthy?'
        ;;
    28)
        suberr 'Timeout connecting to host. Is Elasticsearch healthy?'
        ;;
    *)
        suberr "Connection to Elasticsearch failed. Exit code: ${exit_code}"
        ;;
    esac

    exit $exit_code
fi

sublog 'Elasticsearch is running'

log 'Waiting for initialization of built-in users'

wait_for_builtin_users || exit_code=$?

if ((exit_code)); then
    suberr 'Timed out waiting for condition'
    exit $exit_code
fi

sublog 'Built-in users were initialized'

for role in "${!roles_files[@]}"; do
    log "Role '$role'"

    declare body_file
    body_file="${BASH_SOURCE[0]%/*}/roles/${roles_files[$role]:-}"
    if [[ ! -f "${body_file:-}" ]]; then
        sublog "No role body found at '${body_file}', skipping"
        continue
    fi

    sublog 'Creating/updating'
    ensure_role "$role" "$(< "${body_file}")"
done

for user in "${!users_passwords[@]}"; do
    log "User '$user'"
    if [[ -z "${users_passwords[$user]:-}" ]]; then
        sublog 'No password defined, skipping'
        continue
    fi

    declare -i user_exists=0
    user_exists="$(check_user_exists "$user")"

    if ((user_exists)); then
        sublog 'User exists, setting password'
        set_user_password "$user" "${users_passwords[$user]}"
    else
        if [[ -z "${users_roles[$user]:-}" ]]; then
            suberr '  No role defined, skipping creation'
            continue
        fi

        sublog 'User does not exist, creating'
        create_user "$user" "${users_passwords[$user]}" "${users_roles[$user]}"
    fi
done

elasticsearch_host="${ELASTICSEARCH_HOST:-elasticsearch}"
log 'Add attachment pipeline'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_ingest/pipeline/attachment?pretty" -H 'Content-Type: application/json' -d'
{
  "description" : "Extract attachment information",
  "processors" : [
    {
      "attachment" : {
        "field" : "data",
        "remove_binary": true,
        "indexed_chars": 200000
      },
      "date" : {
        "field" : "mtime",
        "target_field" : "timestamp",
        "formats" : ["UNIX"],
        "timezone" : "UTC"
      }
    }
  ]
}
' > /dev/null && sublog 'Done'

log 'Add cbor-attachment pipeline'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_ingest/pipeline/cbor-attachment?pretty" -H 'Content-Type: application/json' -d'
{
  "description" : "Extract attachment information",
  "processors" : [
    {
      "attachment" : {
        "field" : "data",
        "remove_binary": true,
        "indexed_chars": 200000
      },
      "date" : {
        "field" : "mtime",
        "target_field" : "timestamp",
        "formats" : ["UNIX"],
        "timezone" : "UTC"
      }
    }
  ]
}
' > /dev/null && sublog 'Done'

kibana_host="${KIBANA_HOST:-kibana}"
while ! curl -s -m5 "http://elastic:${ELASTIC_PASSWORD}@${kibana_host}:5601/" > /dev/null; do
    sleep 1
done
log 'Add defualt configuration and dashboards'
sleep 30
curl -s -X POST "http://elastic:${ELASTIC_PASSWORD}@${kibana_host}:5601/api/saved_objects/_import?overwrite=true" -H "kbn-xsrf: true" --form file=@/export.ndjson > /dev/null
sublog 'Done'
