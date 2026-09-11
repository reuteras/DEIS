#!/usr/bin/env bash

set -eu
set -o pipefail

# shellcheck disable=SC1091
source "${BASH_SOURCE[0]%/*}"/lib.sh

# --------------------------------------------------------
# Users declarations

# Only the users this project actually runs something as, which is now just
# kibana_system. The Beats accounts (metricbeat/filebeat/heartbeat/
# monitoring/beats_system) came with the docker-elk setup and went with
# extensions/; logstash_internal went with logstash/ itself. Neither set was
# inert: their roles were created unconditionally, and 'deis init' fills
# every "changeme" in .env with a real generated secret, so every DEIS
# instance ended up with live, credentialed accounts holding write roles for
# tooling that was never wired up.
#
# kibana_system is an Elasticsearch built-in, so there is no custom role to
# create and no account to create - only a password to set. That is why the
# role machinery (setup/roles/, lib.sh's ensure_role) is gone as well.
declare -A users_passwords
users_passwords=(
    [kibana_system]="${KIBANA_SYSTEM_PASSWORD:-}"
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

for user in "${!users_passwords[@]}"; do
    log "User '$user'"
    if [[ -z "${users_passwords[$user]:-}" ]]; then
        sublog 'No password defined, skipping'
        continue
    fi

    # Every user left here is an Elasticsearch built-in, so it is always
    # present already - its absence means the cluster is not in the state
    # this script assumes, which is worth failing on rather than papering
    # over by creating an account with a guessed role.
    declare -i user_exists=0
    user_exists="$(check_user_exists "$user")"

    if ((user_exists)); then
        sublog 'Setting password'
        set_user_password "$user" "${users_passwords[$user]}"
    else
        suberr "  Built-in user '$user' does not exist - is this really an Elasticsearch cluster with security enabled?"
        exit 1
    fi
done

elasticsearch_host="${ELASTICSEARCH_HOST:-elasticsearch}"

# Item 32's language detection, stored once and referenced by id from both
# places that need it: the cbor-attachment pipeline below (tagging documents
# as they are indexed) and the _update_by_query backfill further down
# (tagging documents indexed before the pipeline existed). Those two ran
# identical ~1.5 KB copies of this logic inline before, which had to stay
# byte-for-byte in sync by hand or the backfill would quietly start
# disagreeing with the pipeline.
#
# The two call sites differ only in where the document lives: an ingest
# processor's ctx *is* the source, while _update_by_query's ctx wraps it in
# ctx._source. Resolving that here with a containsKey check is what lets one
# script serve both.
#
# It is a stopword *presence* count, not a frequency count - ten distinctive
# function words per language, one point each, needing more than two hits to
# claim a language at all. Deliberately crude: it only has to separate
# English from Swedish well enough to pick a stopword list and drive a
# Kibana filter, and anything it is unsure about lands in "unknown", which
# is a correct and useful answer for the numeric/tabular documents that make
# up much of a leak dump. See docs/IMPROVEMENTS.md item 32.
log 'Add language-detection stored script'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_scripts/deis-detect-language?pretty" -H 'Content-Type: application/json' -d'
{
    "script" : {
        "lang" : "painless",
        "source" : "def doc = ctx.containsKey(\"_source\") ? ctx._source : ctx; if (doc.attachment == null || doc.attachment.content == null) { doc.language = \"unknown\"; return; } String content = \" \" + ((String) doc.attachment.content).toLowerCase() + \" \"; def english = [\"the\", \"and\", \"that\", \"with\", \"for\", \"this\", \"from\", \"have\", \"are\", \"was\"]; def swedish = [\"och\", \"det\", \"att\", \"som\", \"med\", \"inte\", \"den\", \"vara\", \"har\", \"till\"]; int en = 0; int sv = 0; for (def word : english) { if (content.contains(\" \" + word + \" \")) { en++; } } for (def word : swedish) { if (content.contains(\" \" + word + \" \")) { sv++; } } if (en >= sv && en > 2) { doc.language = \"english\"; } else if (sv >= en && sv > 2) { doc.language = \"swedish\"; } else { doc.language = \"unknown\"; }"
    }
}
' > /dev/null && sublog 'Done'

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
        },
        {
            "script" : {
                "description" : "item 32: tag detected language by stopword presence, so the notebook can stop applying both stopword lists indiscriminately and Kibana can filter by language",
                "id" : "deis-detect-language"
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
#
# attachment.modifier/publisher are explicit for the same reason
# extraction_status/language/duplicate_cluster are: the exported dashboards
# (export.ndjson) have panels that reference them by name, but the
# ingest-attachment pipeline only creates a field dynamically the first time
# some document actually has that Tika metadata populated - a fresh index,
# or a corpus that never happens to contain a document with a "Modified By"/
# "Publisher" property, would otherwise leave those two fields entirely
# absent (not just empty), which Kibana reports as "field not found" rather
# than showing an empty result.
# top_folder's script reads filename's 4th path segment
# (extracted/files/<archive-sha256>/<segment>/...), not its 3rd: every
# archive - nested or not - extracts flat into a directory named after its
# own sha256 (see unpack/start.sh's process_zip_like/process_pst), so the
# 3rd segment is always that hash, never anything from the leak dump
# itself. The 4th segment is the first real subfolder an archive's own
# internal structure had, when it had one - a genuinely meaningful "top
# folder" for Kibana's "Top folders" panel to group by. Files with no such
# subfolder (sitting directly inside an archive, or never archived at all)
# fall into a single "(ungrouped)" bucket instead of each becoming its own
# one-file "folder".
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
                "language" : { "type" : "keyword" },
                "duplicate_cluster" : { "type" : "keyword" },
                "attachment" : {
                    "properties" : {
                        "content" : {
                            "type" : "text",
                            "fielddata" : true
                        },
                        "modifier" : {
                            "type" : "text",
                            "fields" : { "keyword" : { "type" : "keyword", "ignore_above" : 256 } }
                        },
                        "publisher" : {
                            "type" : "text",
                            "fields" : { "keyword" : { "type" : "keyword", "ignore_above" : 256 } }
                        }
                    }
                },
                "pii" : {
                    "properties" : {
                        "personnummer" : { "type" : "keyword" },
                        "emails" : { "type" : "keyword" },
                        "phone_numbers" : { "type" : "keyword" },
                        "ibans" : { "type" : "keyword" },
                        "card_numbers" : { "type" : "keyword" },
                        "has_pii" : { "type" : "boolean" }
                    }
                },
                "entities" : {
                    "properties" : {
                        "persons" : { "type" : "keyword" },
                        "organizations" : { "type" : "keyword" },
                        "locations" : { "type" : "keyword" },
                        "has_entities" : { "type" : "boolean" }
                    }
                },
                "source_chain" : {
                    "properties" : {
                        "url" : { "type" : "keyword" },
                        "sha256s" : { "type" : "keyword" },
                        "filenames" : { "type" : "keyword" },
                        "archive_types" : { "type" : "keyword" }
                    }
                }
            },
            "runtime" : {
                "top_folder" : {
                    "type" : "keyword",
                    "script" : {
                        "source" : "def parts = doc['"'"'filename'"'"'].value.splitOnToken('"'"'/'"'"'); if (parts.length > 4) { emit(parts[3]); } else { emit('"'"'(ungrouped)'"'"'); }"
                    }
                }
            }
        }
    }
}
' > /dev/null && sublog 'Done'

# Item 21's structured-data-as-rows piece (.csv only for now): one document
# per CSV row rather than one flattened text blob per file, so a precise
# per-column query becomes possible instead of only a full-text match
# against attachment.content. Deliberately a separate index rather than a
# field on the file's own leakdata-index-000001 document - a single CSV can
# have thousands of rows. "leakdata-rows-*" already matches the "leakdata-*"
# Kibana index pattern above, so no new one is needed there.
#
# "row" is "flattened" rather than given explicit per-column mappings: CSV
# column names are arbitrary and unbounded across hundreds of differently
# shaped source files, so mapping each one individually would eventually
# blow the leakdata template's own total_fields.limit above (2000) even in
# a separate index. flattened stores/indexes arbitrary JSON keys as
# searchable keyword pairs (dot-path queryable, e.g. row.SomeColumn: value)
# without each one becoming its own mapped field.
#
# "priority": 100 is required, not optional: "leakdata-rows-000001" also
# matches the "leakdata-*" template above, which sets no priority of its
# own (defaults to 0). Composable index templates apply exactly one
# template - highest priority wins - so without an explicit higher priority
# here, this index would silently inherit the wrong (Tika/pii-shaped)
# mapping instead, and "row" would fall back to dynamic mapping instead of
# flattened.
log 'Add leakdata-rows index template (item 21 CSV-as-rows)'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/_index_template/leakdata-rows?pretty" -H 'Content-Type: application/json' -d'
{
    "index_patterns" : ["leakdata-rows-*"],
    "priority" : 100,
    "template" : {
        "settings" : {
            "number_of_replicas" : 0
        },
        "mappings" : {
            "properties" : {
                "source_sha256" : { "type" : "keyword" },
                "source_filename" : { "type" : "keyword" },
                "row_number" : { "type" : "integer" },
                "row" : { "type" : "flattened" }
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
log 'Backfill top_folder runtime field, extraction_status, content fielddata, attachment.modifier/publisher, pii, entities, source_chain, language and duplicate_cluster onto the existing leakdata index'
curl -s -X PUT "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/leakdata-index-000001/_mapping?pretty" -H 'Content-Type: application/json' -d'
{
    "properties" : {
        "extraction_status" : { "type" : "keyword" },
        "language" : { "type" : "keyword" },
        "duplicate_cluster" : { "type" : "keyword" },
        "attachment" : {
            "properties" : {
                "content" : {
                    "type" : "text",
                    "fielddata" : true
                },
                "modifier" : {
                    "type" : "text",
                    "fields" : { "keyword" : { "type" : "keyword", "ignore_above" : 256 } }
                },
                "publisher" : {
                    "type" : "text",
                    "fields" : { "keyword" : { "type" : "keyword", "ignore_above" : 256 } }
                }
            }
        },
        "pii" : {
            "properties" : {
                "personnummer" : { "type" : "keyword" },
                "emails" : { "type" : "keyword" },
                "phone_numbers" : { "type" : "keyword" },
                "ibans" : { "type" : "keyword" },
                "card_numbers" : { "type" : "keyword" },
                "has_pii" : { "type" : "boolean" }
            }
        },
        "entities" : {
            "properties" : {
                "persons" : { "type" : "keyword" },
                "organizations" : { "type" : "keyword" },
                "locations" : { "type" : "keyword" },
                "has_entities" : { "type" : "boolean" }
            }
        },
        "source_chain" : {
            "properties" : {
                "url" : { "type" : "keyword" },
                "sha256s" : { "type" : "keyword" },
                "filenames" : { "type" : "keyword" },
                "archive_types" : { "type" : "keyword" }
            }
        }
    },
    "runtime" : {
        "top_folder" : {
            "type" : "keyword",
            "script" : {
                "source" : "def parts = doc['"'"'filename'"'"'].value.splitOnToken('"'"'/'"'"'); if (parts.length > 4) { emit(parts[3]); } else { emit('"'"'(ungrouped)'"'"'); }"
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

# Same reasoning as extraction_status above: the cbor-attachment pipeline's
# language-detection processor (item 32) only tags documents indexed after
# it existed. Runs the same stored script by id instead of reindexing, so
# pre-existing documents get a language tag too - and so the backfill cannot
# drift away from what the pipeline does.
log 'Backfill language onto documents indexed before language detection existed'
curl -s -X POST "http://elastic:${ELASTIC_PASSWORD}@${elasticsearch_host}:9200/leakdata-index-000001/_update_by_query?conflicts=proceed&pretty" -H 'Content-Type: application/json' -d'
{
    "query" : { "bool" : { "must_not" : { "exists" : { "field" : "language" } } } },
    "script" : { "id" : "deis-detect-language" }
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

# The "Leaked data" dashboard's own default range (its saved object's
# timeRestore/timeFrom, imported above from export.ndjson) survives every
# setup/restore because it's a real saved object, version-controlled here.
# Discover's default range is not a saved object at all - it's the global
# "timepicker:timeDefaults" advanced setting, which lives only in Kibana's
# own .kibana index. Setting that by hand in the UI doesn't survive a
# saved-objects reset the way the dashboard's own setting does, so it's
# set here instead, every run, the same way the rest of this script treats
# every other piece of default configuration. Same instant as the
# dashboard's own timeFrom above, for consistency between the two.
#
# The legacy /api/kibana/settings endpoint (still in Kibana's docs) 400s on
# this version - "exists but is not available with the current
# configuration" - so this uses /internal/kibana/settings instead, the one
# actually confirmed live against this stack (9.5.3): it needs the
# x-elastic-internal-origin header or Kibana rejects it as an unlabeled
# internal API call.
log 'Set Discover default time range to match the dashboard'
curl -s -X POST "http://elastic:${ELASTIC_PASSWORD}@${kibana_host}:5601/internal/kibana/settings" \
    -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -H 'x-elastic-internal-origin: Kibana' -d'
{
    "changes" : {
        "timepicker:timeDefaults" : "{\"from\":\"1969-12-31T23:00:00.000Z\",\"to\":\"now\"}"
    }
}
' > /dev/null && sublog 'Done'
