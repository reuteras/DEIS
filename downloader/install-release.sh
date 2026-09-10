#!/usr/bin/env bash

set -euxo pipefail

# Identify architecture
case "$(arch -s)" in
'i386' | 'i686')
    MACHINE='32'
    ;;
'amd64' | 'x86_64')
    MACHINE='64'
    ;;
'armv5tel')
    MACHINE='arm32-v5'
    ;;
'armv6l')
    MACHINE='arm32-v6'
    grep Features /proc/cpuinfo | grep -qw 'vfp' || MACHINE='arm32-v5'
    ;;
'armv7' | 'armv7l')
    MACHINE='arm32-v7a'
    grep Features /proc/cpuinfo | grep -qw 'vfp' || MACHINE='arm32-v5'
    ;;
'armv8' | 'aarch64')
    MACHINE='arm64-v8a'
    ;;
'mips')
    MACHINE='mips32'
    ;;
'mipsle')
    MACHINE='mips32le'
    ;;
'mips64')
    MACHINE='mips64'
    ;;
'mips64le')
    MACHINE='mips64le'
    ;;
'ppc64')
    MACHINE='ppc64'
    ;;
'ppc64le')
    MACHINE='ppc64le'
    ;;
'riscv64')
    MACHINE='riscv64'
    ;;
's390x')
    MACHINE='s390x'
    ;;
*)
    echo "error: The architecture is not supported."
    exit 1
    ;;
esac

# Pinned rather than "releases/latest": the original script downloaded a
# checksum (.dgst) from the very same GitHub release it was verifying the
# archive against - no real protection against a compromised/MITM'd
# download, since an attacker controlling that response controls both
# files. Bumping VERSION means downloading both archives fresh, computing
# their real sha256 yourself, and updating both VERSION and the table
# below in the same change - see downloader/VENDORED.md.
VERSION='v5.53.0'
declare -A SHA256_BY_MACHINE=(
    ['64']='6bbb8aee65a57d0b12599b4b7c842b3ad0daca4436e661d94015c447cb31b4fa'
    ['arm64-v8a']='2bda03a3d6b93122cb418504dc1c9ada10f99ae6be58a1fcc4a5ca1a01e12a30'
)

TMP_DIRECTORY="$(mktemp -d)/"
ZIP_FILE="${TMP_DIRECTORY}v2ray-linux-$MACHINE.zip"
DOWNLOAD_LINK="https://github.com/v2fly/v2ray-core/releases/download/$VERSION/v2ray-linux-$MACHINE.zip"

download_v2ray() {
    if ! curl -L -H 'Cache-Control: no-cache' -o "$ZIP_FILE" "$DOWNLOAD_LINK" -#; then
        echo 'error: Download failed! Please check your network or try again.'
        exit 1
    fi
}

verification_v2ray() {
    local pinned="${SHA256_BY_MACHINE[$MACHINE]:-}"
    if [[ -z "$pinned" ]]; then
        echo "error: No pinned sha256 for architecture '$MACHINE' - see downloader/VENDORED.md."
        echo "Download both v2ray-linux-$MACHINE.zip and its .dgst from the pinned release," \
            "confirm the .dgst's SHA2-256 matches what you compute yourself, then add it to" \
            "SHA256_BY_MACHINE above before using v2ray on this architecture."
        exit 1
    fi
    local actual
    actual="$(sha256sum "$ZIP_FILE" | sed 's/ .*//')"
    if [[ "$actual" != "$pinned" ]]; then
        echo 'error: sha256 mismatch against the pinned value - refusing to install.'
        exit 1
    fi
}

decompression() {
    unzip -q "$ZIP_FILE" -d "$TMP_DIRECTORY"
}

install_v2ray() {
    install -m 755 "${TMP_DIRECTORY}v2ray" "/usr/local/bin/v2ray"
    install -d /usr/local/lib/v2ray/
    install -m 755 "${TMP_DIRECTORY}geoip.dat" "/usr/local/lib/v2ray/geoip.dat"
    install -m 755 "${TMP_DIRECTORY}geosite.dat" "/usr/local/lib/v2ray/geosite.dat"
}

information() {
    echo 'installed: /usr/local/bin/v2ray'
    echo 'installed: /usr/local/lib/v2ray/geoip.dat'
    echo 'installed: /usr/local/lib/v2ray/geosite.dat'
    rm -r "$TMP_DIRECTORY"
    echo "removed: $TMP_DIRECTORY"
    echo "You may need to execute a command to remove dependent software: apk del curl unzip"
    echo "info: V2Ray is installed."
}

main() {
    download_v2ray
    # verification_v2ray was defined but never actually called here - the
    # download was installed with no integrity check of any kind, not
    # even the weak same-host one the original script's dead code
    # implemented. Found while fixing item 10.
    verification_v2ray
    decompression
    install_v2ray
    information
}

main
