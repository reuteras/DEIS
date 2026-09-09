# Pinned dependencies

## 7-Zip

`install.sh` downloads the 7-Zip console build fresh from 7-zip.org at image build time (it
is a real binary, not something practical to vendor into git), but pins the version and
checks the download against a hash recorded here, rather than trusting whatever the
download returns.

- Source: <https://www.7-zip.org/download.html>
- Version: 26.03
- Pinned on: 2026-09-04
- sha256:
  - `7z2603-linux-x64.tar.xz`: `dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695`
  - `7z2603-linux-arm64.tar.xz`: `2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996`

To upgrade: read the 7-Zip changelog, download both architectures' tarballs from
`https://www.7-zip.org/a/7z<version>-linux-<arch>.tar.xz`, record their sha256 here and in
`VERSION`/`SHA256` in `install.sh`, and verify the change is reviewed rather than automated -
7-zip.org does not publish independent per-file checksums, so this hash is only as
trustworthy as the download that produced it.

## msoffcrypto-tool

Password-cracking for individually-encrypted Office documents (as opposed to encrypted
*archives*, which 7-Zip already handles) - see `deis.cfg.default`'s
`document_decrypt_office` key. Installed via `pip` into its own venv
(`/opt/msoffcrypto-venv`) at image build time, since Debian trixie has no package for it and
enforces PEP 668 (no unmanaged system-wide `pip install`); `python3-venv` itself is
build-time only and autoremoved afterwards, same as `wget`/`xz-utils`.

- Source: <https://pypi.org/project/msoffcrypto-tool/>
- Version: 6.0.0
- Pinned on: 2026-09-09

To upgrade: read the changelog on <https://github.com/nolze/msoffcrypto-tool/releases>,
check what its own dependencies (`cryptography`, `olefile`) pull in at the new version, and
bump the pinned version in `Dockerfile`'s `pip install` line.
