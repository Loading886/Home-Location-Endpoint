# Changelog

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
