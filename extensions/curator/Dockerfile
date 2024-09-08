#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_8
FROM untergeek/curator:8.0.16

# hadolint ignore=DL3002
USER root

RUN >>/var/spool/cron/crontabs/nobody \
    echo '* * * * * /curator/curator /.curator/delete_log_files_curator.yml'

ENTRYPOINT ["crond"]
CMD ["-f", "-d8"]
