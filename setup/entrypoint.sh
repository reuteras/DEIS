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
# Only cbor-attachment is used - ingest.py posts CBOR-encoded documents, not
# JSON, so a JSON-only "attachment" pipeline would never be reachable anyway.
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

# attachment.content.fielddata is needed for the notebook's word-cloud
# aggregation (a plain terms aggregation over an analyzed text field
# requires it). The cost is real - fielddata loads every distinct term into
# heap - but it scales with vocabulary size, not corpus size, which is a lot
# smaller than pulling every document's full content client-side, the
# alternative this replaced.
log 'Add leakdata index template (top_folder runtime field, explicit mapping)'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_index_template/leakdata?pretty" -H 'Content-Type: application/json' -d'
{
    "index_patterns" : ["leakdata-*"],
    "template" : {
        "settings" : {
            "number_of_replicas" : 0,
            "index.mapping.total_fields.limit" : 2000,
            "index.highlight.max_analyzed_offset" : 2000000000
        },
        "mappings" : {
            "properties" : {
                "sha256" : { "type" : "keyword" },
                "filename" : { "type" : "keyword" },
                "extraction_status" : { "type" : "keyword" },
                "attachment" : {
                    "properties" : {
                        "content" : {
                            "type" : "text",
                            "fielddata" : true
                        }
                    }
                }
            },
            "runtime" : {
                "top_folder" : {
                    "type" : "keyword",
                    "script" : {
                        "source" : "def parts = doc['"'"'filename'"'"'].value.splitOnToken('"'"'/'"'"'); if (parts.length > 2) { emit(parts[2]); } else { emit('"'"'(root)'"'"'); }"
                    }
                }
            }
        }
    }
}
' > /dev/null && sublog 'Done'

# The template above only applies to indices created from now on - a live
# mapping cannot change an existing field type (sha256/filename from
# text+keyword to keyword, dropping attachment.content.keyword) without a
# reindex, and nothing currently creates a new leakdata-* index (there is no
# ILM/rollover policy), so those specific changes only take effect once one
# does. The rest is dynamic, so it is also patched onto the existing index
# directly: replicas (was 1 on a single node, so the cluster was permanently
# yellow with an unassigned shard for no benefit), total_fields.limit, and
# highlight.max_analyzed_offset (previously a manual Dev Tools step in the
# README - not a mapping change, so it applies immediately either way).
log 'Apply replicas/total_fields.limit/max_analyzed_offset to the existing leakdata index'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/leakdata-index-000001/_settings?pretty" -H 'Content-Type: application/json' -d'
{
    "index" : {
        "number_of_replicas" : 0,
        "mapping.total_fields.limit" : 2000,
        "highlight.max_analyzed_offset" : 2000000000
    }
}
' > /dev/null && sublog 'Done'

# The template above only applies to indices created from now on. Also patch
# the runtime field onto the index directly, so it shows up for data already
# ingested before this template existed (adding a runtime field to an
# existing index's mapping doesn't require a reindex). This index's filename
# field is still text+keyword (see above), so its script still reads
# filename.keyword, unlike the template's version above.
log 'Backfill top_folder runtime field, extraction_status and content fielddata onto the existing leakdata index'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/leakdata-index-000001/_mapping?pretty" -H 'Content-Type: application/json' -d'
{
    "properties" : {
        "extraction_status" : { "type" : "keyword" },
        "attachment" : {
            "properties" : {
                "content" : {
                    "type" : "text",
                    "fielddata" : true
                }
            }
        }
    },
    "runtime" : {
        "top_folder" : {
            "type" : "keyword",
            "script" : {
                "source" : "def parts = doc['"'"'filename.keyword'"'"'].value.splitOnToken('"'"'/'"'"'); if (parts.length > 2) { emit(parts[2]); } else { emit('"'"'(root)'"'"'); }"
            }
        }
    }
}
' > /dev/null && sublog 'Done'

# Documents indexed before extraction_status existed have no value for it at
# all (a mapping update only affects documents indexed after it, same
# reasoning as the field-type changes above), so the "Extraction status"
# dashboard panel would show nothing for them. Backfill "ok" onto every
# document missing the field - unpack only ever explicitly flags
# encrypted/corrupt content, so anything not flagged already meant "ok".
log 'Backfill extraction_status=ok onto documents indexed before this field existed'
curl -s -X POST "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/leakdata-index-000001/_update_by_query?conflicts=proceed&pretty" -H 'Content-Type: application/json' -d'
{
    "query" : { "bool" : { "must_not" : { "exists" : { "field" : "extraction_status" } } } },
    "script" : { "source" : "ctx._source.extraction_status = '"'"'ok'"'"'" }
}
' > /dev/null && sublog 'Done'

# One document per ingest run (ingest.py's index_run_summary, item 25) -
# separate from leakdata-* so a run's reconciliation counts are never mixed
# into a content search. @timestamp as an explicit date field (rather than
# relying on dynamic mapping to guess it right) is what lets a saved search
# sort by it.
log 'Add deis-ingest-runs index template'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_index_template/deis-ingest-runs?pretty" -H 'Content-Type: application/json' -d'
{
    "index_patterns" : ["deis-ingest-runs*"],
    "template" : {
        "settings" : {
            "number_of_replicas" : 0
        },
        "mappings" : {
            "properties" : {
                "@timestamp" : { "type" : "date" },
                "files_looked_at" : { "type" : "long" },
                "internal_files" : { "type" : "long" },
                "unique_files" : { "type" : "long" },
                "duplicate_copies" : { "type" : "long" },
                "indexed_this_run" : { "type" : "long" },
                "already_indexed" : { "type" : "long" },
                "failed" : { "type" : "long" },
                "elasticsearch_document_count" : { "type" : "long" }
            }
        }
    }
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
