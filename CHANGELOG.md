# Changelog

## Unreleased

## 0.3.0 - 2026-07-19

- Promote complete WifiTile 200 response supplementation to a minor-version
  milestone: recent phone-requested BSSIDs that lack a usable Apple location
  are added around the effective target without disturbing valid tile entries.
- Keep the bounded request-only identity cache separate from WLOC response
  observations, preserve protobuf unknown fields plus HTTP transport and cache
  semantics, and retain count-only operational logging.
- Validate the path with the full automated suite and a 9,639-device stress
  fixture, while documenting the intended single-operator cache boundary.

## 0.2.6 - 2026-07-19

- Supplement normal WifiTile 200 responses with recent phone-requested BSSIDs
  that are missing a usable location from Apple's tile.
- Keep request-only identities separate from the larger WLOC response cache,
  deduplicate additions, preserve the original protobuf and transport contract,
  and log only the injected count.
- Document the single-operator cache boundary and iOS negative-cache recovery
  procedure without weakening or restarting the server-side interceptor.

## 0.2.5 - 2026-07-19

- Keep the bounded recent Wi-Fi identity cache warm for 30 minutes so sparse
  no-fix recovery can converge after iOS eventually refreshes its device-side
  location cache.
- Make the recovery bounds explicit in the systemd unit and preserve Apple's
  cache policy and validators on rewritten responses.
- Lock in target-neutral template behavior: cached Apple tiles are retranslated
  to the current preset on every use and are not cleared during a location switch.

## 0.2.4 - 2026-07-19

- Uniformly compress valid WLOC and WifiTile response geometry into a 45 m
  radius around the effective target, preventing Apple kilometre-scale hotspot
  tiles from placing iOS hundreds of metres away from the selected location.
- Preserve relative hotspot structure and leave already-small geometry at its
  original scale; sentinel, malformed-response, and no-coverage fail-closed
  behavior remains unchanged.

## 0.2.3 - 2026-07-19

- Register a Telegram command menu beside the message field for one-tap access
  to the location controls and current status.
- Match the Apple Relay controller's semantic button palette: blue for ordinary
  choices, green for the current/positive action, and red for destructive
  actions. The selected color follows location and pause-state changes.
- Display all ten built-in location entries with concise Chinese names while
  retaining English addresses and compatibility with existing English-labeled
  location databases.
- Let the authorized advanced-mode Telegram chat retrieve the current VLESS or
  SS2022 URI and CA profile directly. Deliveries use validated bounded files and
  read-only handoff copies that do not expose the root credential store, Xray
  configuration, or leaf private key to the Bot.
- Require the configured Telegram operator to be a positive private-chat ID and
  verify both the chat and sender on every message and callback.
- Record supplemental group membership added to pre-existing Bot accounts so a
  failed install or later uninstall restores the host's original account state.
- Pin bootstrap defaults and every published install command to the immutable
  v0.2.3 release instead of following the mutable main branch.
- Redirect the project website to HTTPS and publish HSTS on secure responses.

## 0.2.2 - 2026-07-19

- Stabilize Antarctic and other sparse-coverage targets by supplementing only
  proven sentinel-only WLOC batches with recent real Wi-Fi identities held in a
  bounded ten-minute memory cache; unknown and malformed payloads still fail
  closed.
- Recover exact WifiTile 404 responses through a bounded in-memory complete-tile
  cache, a known-valid public Apple seed tile, then a minimal tile built only from
  recent real identities. If every optional recovery source is unavailable, the
  original Apple 404 is returned unchanged.
- Disable runtime micro-drift for the built-in Antarctic Kunlun Station preset,
  while retaining its per-install random center and deterministic 45 m batch
  geometry. No BSSID, coordinate, request body, or response body is persisted or
  added to operational logs.

## 0.2.1 - 2026-07-19

- Mark the Telegram controller healthy only after its first successful long
  poll, so a duplicated Bot token or broken Telegram path cannot pass install
  readiness on a startup-only heartbeat.
- Bound the advanced location catalog to 50 entries to keep Telegram inline
  keyboards and local state predictable under repeated operator input.
- Add repeat-safe Telegram workflow coverage that preserves pre-existing custom
  locations, plus a real one-time CA profile handoff test for MIME, content, and
  shutdown-after-download behavior.

## 0.2.0 - 2026-07-19

- Add an `advanced` installation mode between the beginner full endpoint and
  modifier-only integration. It installs a single-operator Telegram menu for
  switching, adding, deleting, and pausing virtual locations without restarting
  Xray or the interceptor.
- Seed nine independently randomized presets per installation: Los Angeles,
  Tokyo, Hong Kong, Singapore, Kuala Lumpur, Paris, Frankfurt, Reykjavík, and
  Antarctic Kunlun Station, in addition to the detected egress-IP city.
- Add a protocol selector in advanced mode: VLESS + REALITY + Vision remains the
  recommended default, while native `2022-blake3-aes-256-gcm` SS2022 is available
  with a standard SIP002 URI and TCP/UDP firewall handling.
- Isolate Telegram credentials and mutable state behind a dedicated unprivileged
  account. The Bot cannot read the node URI, proxy credentials, or leaf private
  key; atomic updates retain bounded local backups and only one Chat ID is served.
- Validate Bot/Chat credentials before managed changes and add a runtime Bot API
  heartbeat so an alive but disconnected polling process fails `hle verify`.
- Roll back low-privilege users/groups created by a failed transaction, reject
  in-place protocol changes early, and preserve advanced credentials and presets
  on same-mode reinstalls.
- Expand Linux end-to-end coverage to auto-detect VLESS/SS2022, verify real HTTPS
  egress and optional SOCKS5 UDP, exercise the complete Telegram location state
  machine against a loopback test API, and fault-inject a failed Xray startup.

## 0.1.10 - 2026-07-18

- Distinguish an operator closing the automatic CA profile handoff from a real
  startup failure. Ctrl+C/SIGTERM now reports that the endpoint remains active
  instead of printing the misleading "could not start" warning.
- Add reusable Windows and Linux VLESS + REALITY end-to-end checks. They verify
  the official Xray release digest, exercise a real SOCKS client connection,
  confirm the observed exit IP, and avoid persisting the node URI.

## 0.1.9 - 2026-07-18

- Add persistent `hle pause` and `hle resume` controls. Paused endpoints keep
  proxy traffic and scoped Apple requests working but return the original Apple
  location responses without coordinate rewriting. State changes apply to new
  requests without restarting Xray or the interceptor and survive reboots and
  installer upgrades.

## 0.1.8 - 2026-07-18

- Replace the randomized REALITY SNI pool with one installer-managed target.
  The installer still performs live
  certificate, TLS 1.3, HTTP/2, and public-address validation, but no longer
  falls back to another hostname or accepts SNI/target overrides.

## 0.1.7 - 2026-07-18

- Automatically start the one-time CA profile download after a successful
  interactive installation, so users no longer need to run `hle profile serve`
  manually. Non-interactive deployments skip the blocking handoff and print the
  command for later use; handoff failures never roll back a completed endpoint.

## 0.1.6 - 2026-07-18

- Silence the expected `systemctl is-active/is-enabled` stderr emitted while a
  first install records rollback state before its service units exist. Return
  codes are still retained, so transaction restoration behavior is unchanged.

## 0.1.5 - 2026-07-18

- Fix the installed `/usr/local/sbin/hle` failing at startup because v0.1.4
  imported a package module that is not present in the intentionally standalone
  `/opt/home-location-endpoint/cli.py` layout. Profile host validation is again
  self-contained, with an isolated-process regression test matching deployment.

## 0.1.4 - 2026-07-18

- Add `hle profile serve`, a tokenized one-download HTTP handoff for the iOS CA
  profile. It defaults to a 100-minute lifetime, closes after the first successful
  download, serves the correct Apple configuration-profile MIME type, prints the
  CA fingerprint, and shows a terminal QR code when the optional `qrencode`
  package is available. Failure to install that helper does not block the endpoint.
- Add `--host`, `--bind`, `--port`, `--timeout-minutes`, and `--no-qr` controls;
  document the temporary-HTTP and firewall/NAT boundaries in the bilingual result.

## 0.1.3 - 2026-07-18

- Add concise Chinese guidance alongside English for source download, interactive
  mode selection, and the final full/modifier-only installation summaries. Node
  URIs, paths, fingerprints, commands, and machine-readable behavior are unchanged.

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
