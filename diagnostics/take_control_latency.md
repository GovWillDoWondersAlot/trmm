# Take Control latency investigation

Changes are limited to physical-desktop mirroring. The updated hub is deployed and
the laptop's OTA code hash was verified. A live ngrok folder-open test now delivers
the opened-folder image without further mouse movement or reconnecting the viewer.
Browser presentation was covered by isolated JavaScript checks; the live probe
uses the same WebSocket protocol and decodes images with PIL.

## Findings and changes

- The agent polls at 30 FPS while active and 10 FPS when idle. Changed JPEGs
  already bypassed the 150 ms heartbeat, so input stopping did not disable capture.
- Identical frames were sent at 30 FPS during input. They now use a one-second
  heartbeat while changed frames still send immediately. Capture polling is unchanged.
- Send timing now uses a monotonic clock and records successful send completion.
  A completed send measures local WebSocket backpressure, not remote receipt.
- Capture explicitly flushes GDI before reading DIB memory and retries failed
  copies. The JPEG cache includes dimensions and quality and commits only after
  successful encoding, allowing an idle-quality refresh with unchanged pixels.
  Microsoft documents synchronization before direct DIB memory access:
  https://devblogs.microsoft.com/oldnewthing/20100923-00/?p=12773
- The standalone Take Control renderer now serializes decodes and immediately
  drains the newest pending frame. Previously a new message could start a decode
  before an already scheduled animation callback ran, allowing overlapping decodes.
- Updated Take Control viewers negotiate `mirror_ack=1`. The hub permits one
  unacknowledged JPEG and keeps only the latest pending frame. Frames have an
  eight-byte envelope (`MRR1` plus a big-endian uint32 sequence); the browser
  acknowledges after decode/draw, or immediately when dropping a background-tab
  frame. Stale acknowledgements cannot release newer frames. Status messages have
  a separate bounded queue, and a 30-second send/ACK timeout closes failed sessions.
- The hub also suppresses duplicate JPEGs for one second, protecting updated
  viewers while an older agent awaits OTA. Legacy viewers keep their existing
  raw-JPEG protocol; negotiation applies only to Take Control.

## Diagnostics

Set `$env:TRMM_MIRROR_DIAG = '1'` in the environment of the hub and target agent
before starting them. For the hub, use the normal launch command:

```powershell
python run_master.py --host 0.0.0.0 --port 8000
```

An already running or packaged laptop agent needs the updated code and environment
before its capture/send diagnostics or fixes can be evaluated. Do not start a
second hub on a port already used by the existing ngrok tunnel.

Open `/viewer/AGENT_ID?mode=mirror&mirrorDiag=1` through the authenticated tunnel.
Reproduce: position over a folder, stop for two seconds, double-click, then leave
the mouse stationary. Record the physical monitor result separately from the viewer.

Look for `MIRROR COPY`, `ENCODE`, `CAPTURE`, `SEND`, `RELAY QUEUE`, `RELAY START`,
`RELAY END`, `RECV`, and `RENDER END`. Capture duration includes cached captures;
encoding logs appear only when JPEG encoding actually occurs. Renderer completion
can follow a decode error; inspect accompanying browser errors before treating it
as a successful draw. Enqueue/send timestamps and byte lengths help compare stages
but are not unique frame IDs. Cross-host wall clocks must be synchronized before
estimating end-to-end latency. These logs alone do not prove presentation latency.

Remove the environment flag and URL parameter after collecting a short trace.

## Reproducible isolated checks

```powershell
python diagnostics/mirror_regression.py
node diagnostics/mirror_renderer_regression.js
```

These check stopped-input change delivery with a simulated slow send, newest-frame
retention in the relay, capture cache/flush/failure behavior with mocked GDI, and
serialized JPEG decoding plus final-frame drain with a mocked browser. They do not
substitute for a real GDI capture, browser presentation, or the ngrok folder test.

## Earlier baseline from the previous running build

Authenticated against the supplied hub and found the sole online laptop, agent
`f08602b3`. Credentials and session cookies were not written into probe files.

- Passive local runs received about 4.5 FPS, with median receive intervals around
  220 ms. Before the click, identical 178,866-byte JPEGs consumed 6.44 Mbps in one
  12-second sample. This is measured duplicate traffic, not proof of WAN saturation.
- Positioned over the desktop TrmmLogs folder, waited two seconds, double-clicked,
  and sent no subsequent mouse movement. The first changed JPEG arrived at 112 ms;
  the final opened-folder JPEG first arrived at 557 ms. The decoded final image
  visibly shows the folder open. This was the local viewer path to the real laptop,
  not a browser presentation measurement or an independent physical-monitor observation.
- Local WebSocket ping RTT measured 0.8 ms. PIL JPEG decoding reached about 21 ms
  in the pre-click sample; this does not measure browser createImageBitmap timing.
- Two ngrok viewer sessions completed HTTP 101 upgrades but yielded no frames
  during 12- and 24-second observations. Other ngrok HTTP requests intermittently
  ended with RemoteDisconnected. One attempt using a local-login cookie on the WAN
  viewer received HTTP 403; its cause was not established. Do not attribute these
  failures to the renderer: that code was not involved in the background probe.
- No WAN clicks were sent without a current WAN frame. The exact WAN folder test
  and the laptop's independent physical-monitor observation remain outstanding.

Those baseline samples preceded deployment and do not verify the fixes. The user
subsequently requested use of the existing OTA and GitHub workflows; deployment
and post-update validation are recorded below.

Raw local before/click/after measurements are in `mirror_live/local/sample-1.json`,
`sample-2.json`, and `sample-3.json`, with corresponding JPEGs. The WAN 24-second
run is in `mirror_live/wan/sample-1.json` and `sample-2.json`. Future probe runs use
unique timestamp directories to preserve previous measurements.

`mirror_live_probe.py` authenticates using a JSON username/password line on stdin;
it holds cookies in memory. Subsequent stdin lines accept `sample`, `click x y`,
`doubleclick x y`, or `quit`. Each sample runs for 12 seconds. Use only with an
authorized test endpoint and inspect the current frame before choosing coordinates.

## Deployment and post-update validation — 2026-09-18

Restarted the local hub with the updated relay. The previous reloader's child held
the listener after its parent exited, so both verified old hub processes were
stopped. The hub now runs directly through Uvicorn on port 8000.

Used the existing authenticated OTA endpoint for laptop `f08602b3`. The laptop
reported code hash `c1300b4ca4f8b503a08113e985c898d1ad86d74d2b5e776f9dd22880f894e061`,
matching the server's expected hash, with `has_update=false`.

The ngrok probe used `--local-auth --ack`: authentication was against the same
local hub; all viewer frames and input traversed the supplied public ngrok URL.

- WebSocket ping RTT: 805 ms during the folder test.
- Before clicking: 9 frames in 12 seconds, one unique JPEG, 0.902 Mbps. This is
  substantially below the earlier 6.44 Mbps observation, although desktop content
  differed between runs, so it is not a controlled compression comparison.
- Positioned over the Works desktop folder, waited two seconds, double-clicked,
  and sent no mouse input afterward. Changed JPEGs arrived at 0.868 s, 2.150 s,
  and 4.553 s after the click sequence began. The final JPEG visibly shows Works
  open. Earlier individual changed images were not saved, so the first timestamp
  cannot be claimed as the exact moment the opened folder became visible.
- The following 12-second sample continued receiving frames without input.
- Raw reports and final images: `mirror_live/wan/1789686747513148000/`.
- An earlier deployed idle sample also passed, receiving 10 frames in 12 seconds:
  `mirror_live/wan/1789686687350333100/`.

This confirms delivery resumes without continuous mouse movement or a page reload
in the tested WAN session. It does not promise localhost latency over a tunnel
with roughly 800 ms RTT. A single outstanding frame deliberately trades maximum
frame rate for bounded buffering on slow links.

Six isolated Python regressions and the JavaScript checks pass, covering final
frame delivery, duplicate suppression, relay backpressure, stale/missing ACKs,
capture cache/flush behavior, render serialization, envelope parsing, and hidden
tabs. No Backstage behavior was exercised or modified.
