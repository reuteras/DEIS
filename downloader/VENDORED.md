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
- Version: `v5.57.0` (prerelease, adopted inside the 1-week cooldown to clear OSV findings in the bundled binary)
- Pinned on: 2026-11-26
- sha256:
  - `v2ray-linux-64.zip`: `c17a3998748d967522d304fead3af68ae3b425a6cc59972938e1b5bdf4a446e1`
  - `v2ray-linux-arm64-v8a.zip`: `adc6e281a99b66e370d853e7f1b3d60bffc0652dc974770dd47438ac94340ced`
- Cross-checked against the published `.dgst` files' own `SHA2-256` lines for the same
  release (independently downloaded, not trusted blindly - both matched the sha256 computed
  directly from the downloaded archive bytes).

To upgrade: read the release notes at
<https://github.com/v2fly/v2ray-core/releases>, download both
`v2ray-linux-64.zip` and `v2ray-linux-arm64-v8a.zip` for the new version, compute their
sha256 yourself (`sha256sum`), and update `VERSION` and `SHA256_BY_MACHINE` in
`install-release.sh` together with the values here in the same change.
