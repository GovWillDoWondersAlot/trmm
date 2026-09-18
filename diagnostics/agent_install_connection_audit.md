# Compiled agent connection audit — 2026-09-18

## Conclusion and limits

A reproducible connection-address defect exists in the dashboards served by both
localhost and `hub.swiftvtu.com`. On the live hub, a freshly generated diagnostic
package contains the defective address. Both hubs reject the resulting WebSocket
path with HTTP 403, while the correct path successfully registers a temporary
test endpoint and exposes it as online through the authenticated agents API.

This establishes an actual cause of newly generated agents failing to appear
online. It does not establish the exact cause of an unidentified older installer
on a particular target. The failed installer filename and that target's startup
log were requested but were not available during this audit. No installer or
physical-desktop/backstage code was executed by these registration probes.

There is a second confirmed live build problem: fresh generation returned
`success: true` with `exe_filename: null` and `exe_download_url: null`. The ZIP
contains the native runtime, but no standalone setup EXE was returned. Its
compiler-level cause remains unverified because the available SSH key was denied
access to the AWS host. Missing compiler tools must not be asserted as the cause
without the server's compiler error output.

## Reproduction chain

1. `master_hub/static/app.js` defines `detectLanIp` twice, at lines 83 and 604.
   The later declaration overrides the earlier one. Opening the generator calls
   it at line 545. The effective implementation adds `/ws/agent` to the host.
2. The public live dashboard therefore fills in
   `wss://hub.swiftvtu.com/ws/agent`.
3. The generator's URL normalization (`master_hub/generator.py:72`) only removes
   a trailing `/ws`. It preserves `/ws/agent` in `config.json`.
4. The agent's connection routine (`agent_client/client.py:425`) also only
   removes a trailing `/ws`, then appends `/ws/agent/<agent_id>`.
5. The actual connection becomes
   `wss://hub.swiftvtu.com/ws/agent/ws/agent/<agent_id>`.
   The registered route is `/ws/agent/{agent_id}`; the other compatibility route,
   `/ws/ws/agent/{agent_id}`, does not match this duplicated path.

The same normalization was verified in the compiled
`dist/TRMM_Agent/TRMM_Agent.exe`, by reading the PyInstaller archive and examining
only the connection routine's URL-building instructions. The executable was not
launched. Native executable metadata:

- Length: 14,161,195 bytes.
- Architecture: PE machine `0x8664` (x64).
- SHA-256: `914bbd3ab4c927a61f73059e8178b9760daa93ad49af63f464b060476c7e8119`.

## Tests against both running hubs

| Test | Localhost | Live AWS hub |
| --- | --- | --- |
| Authenticated dashboard access | Passed | Passed |
| Dashboard contains duplicate `detectLanIp` declarations | Yes | Yes |
| `/ws/agent/ws/agent/<diagnostic_id>` | HTTP 403 | HTTP 403 |
| `/ws/agent/<diagnostic_id>` | Upgrade succeeded | Upgrade succeeded |
| Send minimal labelled diagnostic handshake | API showed online | API showed online |
| Remove only the temporary test record | Confirmed | Confirmed |

The labelled probes did not impersonate a real target, send remote commands, or
run desktop features. At the initial authenticated status check, neither hub
listed an actual connected target. The synthetic test proves registration and
the agents API work; it does not prove every browser's dashboard is rendering
correctly or that the failed target can reach the hub from its own network.

## Fresh dashboard-generated package results

### Localhost

Generated tag: `ConnectionAudit_local_1789757144`.

- Submitted the effective dashboard default:
  `ws://127.0.0.1:8000/ws/agent`.
- The backend's loopback fallback rewrote this to
  `ws://192.168.1.125:8000` in the generated `config.json`.
- Native executable present: 14,161,195 bytes.
- ZIP produced: 42,159,554 bytes.
- Setup EXE produced: 51,497,856 bytes.

Consequently, a build made through localhost does **not necessarily** retain the
duplicate path: the backend's fallback repairs that case. But the resulting LAN
address requires a route to this machine's private network. It cannot reach a
hub on another network without appropriate routing/VPN. Opening the dashboard
through a LAN IP produces the `/ws/agent` suffix without triggering the loopback
fallback, so that case retains the duplicate-path defect.

Existing local ZIPs also include `127.0.0.1`, `localhost`, and
`192.168.1.125` configurations. Loopback addresses refer to the target itself,
not the administrator's machine. These configurations are observed artifacts,
not proof that the unspecified failed installer used one of them.

### Live

Generated tag: `ConnectionAudit_live_1789757221`; agent ID: `9a303a13`.

- Submitted dashboard default: `wss://hub.swiftvtu.com/ws/agent`.
- The generated `config.json` preserved that exact invalid base URL.
- ZIP produced: 41,002,641 bytes.
- Native executable present: 14,161,195 bytes; Python runtime DLLs present.
- API returned `success: true`, but both EXE fields were null.

The bootstrap inherits the same invalid base URL. Its embedded ZIP download
address begins `https://hub.swiftvtu.com/ws/agent/api/agents/download/`.
The generated one-liner requests a bootstrap under
`/ws/agent/api/agents/bootstrap/`; that address returned HTTP 404. The correct
`/api/agents/bootstrap/` URL returned HTTP 200.

Thus a bootstrap deployment can fail before launching the agent, whereas an
offline ZIP installation can launch an agent which then calls the wrong
WebSocket route. A native runtime is present in both fresh ZIPs; missing EXE
payload is not supported as an explanation for those ZIPs.

## Why the UI can indicate success

`generate_agent` returns package success even when the compiler returns no EXE.
The frontend then announces successful agent generation while hiding the absent
EXE link and offering other artifacts. Installer launch/success messages also
do not establish that the agent completed its WebSocket handshake. Compilation,
file extraction, process startup, and successful registration are separate stages.

## Required correction

1. Use one dashboard address-detection implementation. The configuration field
   should contain a hub base URL such as `wss://hub.swiftvtu.com`, without the
   `/ws/agent` route.
2. Normalize/validate the base URL consistently in package generation and agent
   startup. Build WebSocket and HTTP download paths from that validated base.
   Cover loopback, LAN, public HTTPS, trailing slashes, and old suffixed inputs.
3. Distinguish a working ZIP-only build from a successful Windows setup EXE build
   in the API and UI; surface the actual compiler failure. Inspect the live
   service's compiler logs before choosing a compiler fix.
4. Regenerate affected installers or correct their installed `config.json`, then
   verify startup logs and actual target registration. Source/OTA changes alone
   cannot repair an offline agent that cannot reach its update server.

No production source fixes or deployments were performed as part of this audit.
Diagnostic packages were labelled and retained for review. Credentials and
session cookies remained in memory. Raw JSON evidence is under
`diagnostics/mirror_live/agent-install-audit/` (ignored by Git).

For the exact failed target, obtain its installer filename and the startup log
at `%TEMP%\trmm_agent.log` for the Windows account running the agent. Check the
`Loaded config`, `Dialing Master Hub`, connection error, and import error entries.
If a service account runs the process, its temporary directory may differ from
the interactive user's. That evidence can identify a different or additional
failure without guessing.

## Fix follow-up

The TRMM deployment key subsequently provided SSH access. The compiler log showed
`makensis error: Error: deflateToFile fwrite(65536) failed`, followed by a failed
Linux fallback (`No module named PyInstaller`). NSIS is installed. The root
filesystem was 98% full, with approximately 178 MiB free; an inactive NSIS test
directory under `/tmp/debug_nsis_test` occupied 122 MiB. The directory is being
archived locally before reclaiming its temporary space. A successful fresh build
after reclaiming space is the final confirmation of this resource diagnosis.

The implemented correction adds shared base-address validation, bundles it in
both installers and OTA packages, removes the duplicate dashboard detector, and
uses the correct PowerShell temporary-directory syntax in generated one-liners.
The hub retains a compatibility route for previously generated duplicated paths,
allowing those installations to reconnect before their configuration is updated.

Compiler errors now reach the API/UI as an explicit EXE failure while retaining
available ZIP downloads. Linux builds do not attempt a Windows PyInstaller
fallback. Disk-space checks run before compilation, and build work runs in a
thread with a shared build lock so registration is not blocked during generation.
Tests cover URL variants, invalid addresses, fixture package contents/download
URLs, missing native runtime, low disk space, Linux compiler errors, and dashboard
public/LAN/loopback address selection. No desktop feature runs in these tests.
