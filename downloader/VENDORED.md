# Pinned dependencies

## v2ray-core

`install-release.sh` downloads the v2ray release archive fresh from GitHub at image build
time (it is a real binary, not something practical to vendor into git), but pins the version
and checks the download against a hash recorded here, rather than the original script's
behaviour: fetching a `.dgst` checksum file from the very same release it was verifying the
archive against, which is no real protection against a compromised/MITM'd download - and,
worse, a `verification_v2ray()` function that computed that (weak) check but was never
actually called from `main()`, so the download was installed with no integrity check of any
kind at all. Both fixed together.

Only `linux-64` (x86_64) and `linux-arm64-v8a` (aarch64) are pinned - the only architectures
this project's own Docker builds actually target. `install-release.sh` still detects other
architectures (unchanged, informational), but refuses to install on one with no pinned hash
rather than proceeding unverified.

- Source: <https://github.com/v2fly/v2ray-core>
- Version: `v5.53.0`
- Pinned on: 2026-09-10
- sha256:
  - `v2ray-linux-64.zip`: `6bbb8aee65a57d0b12599b4b7c842b3ad0daca4436e661d94015c447cb31b4fa`
  - `v2ray-linux-arm64-v8a.zip`: `2bda03a3d6b93122cb418504dc1c9ada10f99ae6be58a1fcc4a5ca1a01e12a30`
- Cross-checked against the published `.dgst` files' own `SHA2-256` lines for the same
  release (independently downloaded, not trusted blindly - both matched the sha256 computed
  directly from the downloaded archive bytes).

To upgrade: read the release notes at
<https://github.com/v2fly/v2ray-core/releases>, download both
`v2ray-linux-64.zip` and `v2ray-linux-arm64-v8a.zip` for the new version, compute their
sha256 yourself (`sha256sum`), and update `VERSION` and `SHA256_BY_MACHINE` in
`install-release.sh` together with the values here in the same change.
