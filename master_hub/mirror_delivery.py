"""Low-latency, recovery-aware physical-desktop viewer delivery."""
import asyncio
import io
import json
import time

from PIL import Image
# Keep the small wire codec here as well as on the agent: importing agent_client
# executes its package initializer, which is unsuitable for a Linux-only hub.
def pack_frame(magic, metadata, jpeg):
    header = json.dumps(metadata, separators=(",", ":")).encode()
    if len(header) > 4096:
        raise ValueError("Mirror metadata is too large")
    return magic + len(header).to_bytes(2, "big") + header + jpeg


def unpack_frame(packet, magic):
    if not packet.startswith(magic) or len(packet) < 6:
        raise ValueError("Invalid mirror envelope")
    length = int.from_bytes(packet[4:6], "big")
    if not 0 < length <= 4096 or len(packet) <= 6 + length:
        raise ValueError("Invalid mirror metadata length")
    metadata = json.loads(packet[6:6 + length])
    if not isinstance(metadata, dict):
        raise ValueError("Invalid mirror metadata")
    return metadata, packet[6 + length:]


class LowLatencyMirrorViewerSession:
    protocol_version = 2
    BYTE_WINDOW = 64 * 1024
    TIMEOUT = 30.0

    def __init__(self, viewer_id, ws):
        self.viewer_id, self.ws = viewer_id, ws
        self.sender_task = None
        self.pending = None
        self.snapshot = None
        self.texts = asyncio.Queue(maxsize=32)
        self.event = asyncio.Event()
        self.outstanding = {}
        self.sequence = 0
        self.last_frame = None
        self.last_geometry = None
        self.force_refresh = False
        self.decode_failures = 0

    def push_frame(self, jpeg, metadata=None):
        self.snapshot = (jpeg, dict(metadata or {}), time.monotonic())
        self.pending = self.snapshot
        self.event.set()

    def push_text(self, text):
        if self.texts.full():
            self.texts.get_nowait()
        self.texts.put_nowait(text)
        self.event.set()

    def refresh(self):
        self.force_refresh = True
        if self.snapshot:
            self.pending = (self.snapshot[0], self.snapshot[1], time.monotonic())
        self.event.set()

    def acknowledge(self, sequence, decoded=True):
        if type(sequence) is not int or sequence not in self.outstanding:
            return None
        sent_at, _ = self.outstanding.pop(sequence)
        if decoded:
            self.decode_failures = 0
        else:
            self.decode_failures += 1
            self.refresh()
        self.event.set()
        return (time.monotonic() - sent_at) * 1000

    async def run_sender(self):
        try:
            while True:
                self.event.clear()
                now = time.monotonic()
                if self.decode_failures >= 3:
                    await self.ws.close(code=1011, reason="Take Control image decoding failed")
                    return
                if any(now - sent >= self.TIMEOUT for sent, _ in self.outstanding.values()):
                    await self.ws.close(code=1013, reason="Take Control delivery timed out")
                    return
                if not self.texts.empty():
                    await asyncio.wait_for(self.ws.send_text(self.texts.get_nowait()), self.TIMEOUT)
                    continue
                if self.pending is not None and len(self.outstanding) < 2:
                    jpeg, source_metadata, queued_at = self.pending
                    used = sum(size for _, size in self.outstanding.values())
                    if not self.outstanding or used + len(jpeg) <= self.BYTE_WINDOW:
                        self.pending = None
                        geometry = (source_metadata.get('native_width'), source_metadata.get('native_height'))
                        if jpeg == self.last_frame and geometry == self.last_geometry and not self.force_refresh:
                            continue
                        self.force_refresh = False
                        self.sequence = self.sequence % 0xffffffff + 1
                        metadata = dict(source_metadata)
                        if not metadata.get("native_width") or not metadata.get("native_height"):
                            with Image.open(io.BytesIO(jpeg)) as img:
                                metadata.update(native_width=img.width, native_height=img.height)
                        metadata.update(source_sequence=metadata.get("sequence"), sequence=self.sequence,
                                        hub_queue_ms=round((now - queued_at) * 1000, 2))
                        self.last_frame = jpeg
                        self.last_geometry = geometry
                        self.outstanding[self.sequence] = (time.monotonic(), len(jpeg))
                        await asyncio.wait_for(self.ws.send_bytes(pack_frame(b"MRR2", metadata, jpeg)), self.TIMEOUT)
                        continue
                try:
                    await asyncio.wait_for(self.event.wait(), 0.25)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.ws.close(code=1013, reason="Take Control delivery failed")
