#!/bin/bash

VERSION="2301"

if [[ $(uname -m) == "aarch64" ]]; then
    DOWNLOAD_ARCH="arm64"
else
    DOWNLOAD_ARCH="x64"
fi

cd / || exit
wget "https://www.7-zip.org/a/7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar.xz"
unxz "7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar.xz"
tar xvf "7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar"
rm -rf MANUAL "7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar" /7zzs
