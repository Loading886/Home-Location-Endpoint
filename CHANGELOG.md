# Changelog

## 0.1.2 - 2026-07-18

- Run Ubuntu/Debian package installation with `needrestart` in list-only mode.
  Ubuntu 24.04 otherwise automatically restarts unrelated daemons, which can
  bounce `systemd-networkd`, SSH, or other host services during a remote endpoint
  install. Pending host-level restarts are now left to the operator.

## 0.1.1 - 2026-07-18

- Wait for the apt/dpkg lock instead of failing when a background package
  operation holds it (Ubuntu runs a large `unattended-upgrades` on first boot):
  the installer now pauses with a clear message and a bounded, `HLE_APT_LOCK_WAIT`
  overridable timeout, and every `apt-get` call also passes `DPkg::Lock::Timeout`.
- Fix a full-mode install that intermittently failed at `render.py` with
  `argument --private-key: expected one argument` when the generated REALITY
  x25519 key happened to begin with `-` (base64url); the installer and the Xray
  integration test now pass those values with the unambiguous `--opt=value` form.
- Fall back to full mode instead of aborting under `set -e` when there is no
  controlling terminal (cloud-init, Ansible, cron, systemd, `nohup`): the mode
  prompt now probes `/dev/tty` by opening it, not with `[[ -r ]]`.
- Add `hle uninstall` (with `--yes` for automation): stops the services and
  removes every managed file, the scoped CA, and the low-privilege accounts,
  reusing the installer's own inventory; modifier-only never touches a proxy
  core it did not install.
- Guard the WifiTile rewrite against out-of-range / `(-180,-180)` no-fix markers
  so a mixed tile no longer raises or fabricates a fix (mirrors the gs-loc codec).
- Turn a proven all-sentinel WLOC batch into a deterministic, centered micro-cluster
  within 45 metres of the target; a single record uses the exact target, synthetic
  cellular fixes use at least 1000 m accuracy, and unknown/malformed batches still fail closed.
- Reject missing/null provider coordinates in `validate_ip_location` with the
  same clean `ValueError` as the other fields instead of `KeyError`/`TypeError`.
- Print the URI server address and, when it was auto-detected, a NAT/Realm
  override hint; correct the printed `hle verify`/`status` steps to use `sudo`.
- Validate an explicit `--server`/`--reality-sni` before the transaction so a
  typo fails in the first second instead of after a full install and rollback.
- Reject an invalid `--mode` value with a clear message even when a prior
  installation exists; add `${LOG_DIR}` to the install rollback inventory.
- Report an unreachable IP geolocation provider as one line instead of a raw
  Python traceback; broaden the CI secret guard to also catch PKCS#8 keys.
- Make uninstall destructive only for installer-owned resources: pre-existing or
  untracked accounts/groups and ambiguous UFW rules are preserved, active services
  block deletion, and partial failures now return a non-zero result.

## 0.1.0 - 2026-07-18

- Initial standalone Home-Location-Endpoint implementation.
- Debian 12/13 and Ubuntu 22.04/24.04 installer for amd64/arm64.
- VLESS + REALITY + Vision endpoint with scoped Apple location interception.
- Random point selection inside the detected egress-IP city boundary.
- Geometry-preserving WLOC and WifiTile translation with smooth micro-drift.
- iOS CA profile generation, systemd hardening, verification CLI, and Realm guide.
- Selectable full-endpoint and advanced modifier-only installation modes.
- Deduplicated REALITY SNI pool with randomized order and live TLS 1.3, HTTP/2, and certificate
  validation per run.
- Mode-aware CLI checks and an Xray integration fragment for user-managed proxy cores.
- Transactional install/upgrade rollback, concurrent-operation locking, strict managed-path checks,
  disk preflight, and delayed firewall/sysctl side effects.
- Deterministic CA profile generation in both modes, certificate-set/key validation, immediate CA
  private-key disposal, and expanded `hle verify` integrity checks.
- Bounded HTTP parsing, body/decompression/worker/log limits, safe upstream retries, and malformed
  provider/geometry validation.
- Explicit dual-stack Xray listeners where supported and scoped QUIC blocking for location domains.
- Randomized REALITY fallback rate limits and rejection of camouflage targets that resolve to
  non-public peers.
- Upgrade fallback to a validated existing coordinate when external geolocation services are
  temporarily unavailable.
- Early rejection of unsafe bootstrap/ownership inputs, bounded protobuf field counts, and clearer
  operator errors for damaged local state.
