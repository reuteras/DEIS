FROM alpine:3.19

RUN mkdir -p /deis/bin

COPY *.sh /deis/bin/
COPY crontab.txt /deis/

RUN apk add --no-cache \
        bash \
        curl \
        dcron \
        jq && \
    /usr/bin/crontab /deis/crontab.txt

ENV OWNER_UID=1000
ENV OWNER_GID=1000

CMD ["/deis/bin/dcron.sh", "-f", "-L", "/dev/stdout"]
