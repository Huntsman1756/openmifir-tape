# Security Policy

## Scope

OpenMiFIR Tape is an operator-run data acquisition and analysis pipeline. It
is not a network service: it exposes no listening ports, has no
authentication surface, and stores data only in the operator's local `data/`
directory.

The security-relevant surfaces are:

- **Acquisition** — outbound HTTP(S) fetches to the configured provider
  endpoints (`config/sources/`). The transport boundary
  (`collectors/net.py`) is fail-closed: only `http`/`https` schemes, only
  the per-source `allowed_hosts`, only ports 80/443, only addresses that
  resolve to public IPs (loopback/RFC1918/link-local/metadata endpoints are
  refused), and every redirect hop is re-validated before it is followed.
- **Parsing** — provider XML payloads are parsed with `defusedxml` to
  prevent XML entity-expansion attacks.
- **Storage integrity** — raw objects are write-once and content-addressed
  (SHA-256). A mismatch on an existing identity raises `WriteOnceConflict`.
- **Repository hygiene** — `tools/check_hygiene.py` runs in CI and checks
  tracked files for provider payloads, non-metadata evidence, and
  high-signal credential patterns (best-effort detection, not proof of
  absence).

## Supported versions

Only the latest state of the `main` branch is supported. The project is
pre-1.0; there are no back-ported security releases.

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub's
["Report a vulnerability"](https://github.com/Huntsman1756/openmifir-tape/security/advisories/new)
feature (Security tab → Advisories).

Do **not** open a public issue for a vulnerability.

Include:

- a description of the issue and its impact;
- steps to reproduce or a proof of concept;
- the commit hash you tested against.

We aim to acknowledge reports within 7 days. As a small project we cannot
commit to fixed remediation timelines, but genuine reports will be taken
seriously and credited if desired.

## Operational security notes

- Acquisition credentials, if ever needed, must be provided via environment
  variables — never committed. `.env` files are gitignored.
- The scheduled runner (`omt-g0a3`) writes only under the configured `data/`
  and `evidence/` directories; run it under a least-privilege OS account.
- The systemd units in `ops/systemd/` should be reviewed against your
  environment before installation.
