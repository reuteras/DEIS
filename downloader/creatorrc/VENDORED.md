# Vendored: hephaest0s/creatorrc

Generates a TOR `torrc` tuned for either speed (`--speetor`, what DEIS uses), anonymity
(`--sector`), or a specific exit region (`--evator`).

- Source: <https://github.com/hephaest0s/creatorrc>
- License: GPL-3.0 (see `LICENSE` in this directory)
- Vendored from commit: `72561675576c10f0256f6e6b420cd352a80e4fbc` (creatorrc.py),
  `c0091e791850f533471e34b9b8dacd91ab127c10` (guard_country_resolver.py) — both files' last
  content-changing commits as of vendoring; the repository itself has had no further commits
  since 2016.
- Vendored on: 2026-09-04
- sha256:
  - `creatorrc.py`: `e98ccf921d56f24fcf3284d678cb9f1e85ce94fbf1e70f0d85a67fc7b6d4d7d7`
  - `guard_country_resolver.py`: `fe48ddb911c20ec79c50437828a0d04aa4387b00a2aa617a04bf72425be95bcf`

Previously the Dockerfile fetched these two files over HTTPS at every image build with no
pin and no checksum. Vendoring them means a build no longer depends on GitHub being up or
unchanged, and a supply-chain compromise of the upstream repository can no longer reach a
DEIS build silently.

To pick up an upstream update: diff the new file against this one, read it, and replace both
the `.py` file and the commit hash and hashes above in the same change.
