#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_8
FROM untergeek/curator:8.0.16

RUN echo '* * * * * /curator/curator /.curator/delete_log_files_curator.yml' >> /var/spool/cron/crontabs/nobody

RUN addgroup -g 1000 -S curator && adduser -u 1000 -S curator -G curator
USER curator

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curator --version || exit 1

ENTRYPOINT ["crond"]
CMD ["-f", "-d8"]
