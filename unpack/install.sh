#!/bin/bash

set -euo pipefail

VERSION="2603"

if [[ "$(uname -m)" == "aarch64" ]]; then
    DOWNLOAD_ARCH="arm64"
    SHA256="2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996"
else
    DOWNLOAD_ARCH="x64"
    SHA256="dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695"
fi

archive="7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar.xz"

cd / || exit
wget "https://www.7-zip.org/a/${archive}"

# 7-zip.org does not publish per-file checksums, so this pins against the
# hash of the file as downloaded when this version was pinned here. Bumping
# VERSION means updating these hashes in the same change - see
# unpack/VENDORED.md for how.
echo "${SHA256}  ${archive}" | sha256sum -c -

unxz "${archive}"
tar xvf "7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar"
rm -rf MANUAL "7z${VERSION}-linux-${DOWNLOAD_ARCH}.tar" /7zzs
