"""Versioned transport for physical-desktop mirroring only."""
import asyncio
import json
import time
import uuid


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


class MirrorUplink:
    """Capture keeps running while a bounded writer sends only the newest image."""
    BYTE_WINDOW = 64 * 1024
    TIMEOUT = 30.0
    PROFILES = ((1280, 40, 48 * 1024), (960, 35, 36 * 1024), (768, 30, 28 * 1024))

    def __init__(self, owner, ws, preview):
        self.owner, self.ws, self.preview = owner, ws, preview
        self.stream_id = uuid.uuid4().hex[:16]
        self.pending = None
        self.outstanding = {}
        self.sequence = 0
        self.capture_sequence = 0
        self.latest_metadata = {}
        self.event = asyncio.Event()
        self.capture_event = asyncio.Event()
        self.force = True
        self.profile = 0
        self.good_acks = 0
        self.last_adjustment = 0.0
        self.uplink_ack_ms = 0.0
        self.viewer_ack_ms = 0.0

    def acknowledge(self, message):
        if message.get("stream_id") == self.stream_id and type(message.get("sequence")) is int:
            sent = self.outstanding.pop(message.get("sequence"), None)
            if sent:
                self.uplink_ack_ms = (time.monotonic() - sent[0]) * 1000
                self.adapt(max(self.uplink_ack_ms, self.viewer_ack_ms))
            self.event.set()

    def feedback(self, delivery_ms):
        if not isinstance(delivery_ms, (float, int)) or not 0 <= delivery_ms <= 60000:
            return
        self.viewer_ack_ms = delivery_ms
        self.adapt(max(self.uplink_ack_ms, self.viewer_ack_ms))

    def adapt(self, delivery_ms):
        now = time.monotonic()
        self.good_acks = self.good_acks + 1 if delivery_ms < 180 else 0
        if now - self.last_adjustment < 2:
            return
        previous = self.profile
        if delivery_ms > 450:
            self.profile = min(2, self.profile + 1)
        elif self.good_acks >= 8:
            self.profile = max(0, self.profile - 1)
            self.good_acks = 0
        if previous != self.profile:
            self.last_adjustment = now
            self.refresh()

    def refresh(self):
        self.force = True
        self.capture_event.set()

    async def capture(self):
        loop = asyncio.get_running_loop()
        previous = None
        previous_geometry = None
        while self.owner.is_mirroring:
            self.capture_event.clear()
            active = time.time() - self.owner.last_input_time < 3
            width, quality, budget = self.PROFILES[self.profile] if self.preview else (0, 0, 0)
            started = time.perf_counter()
            input_id = getattr(self.owner, "mirror_input_id", 0)
            force = self.force
            self.force = False
            jpeg = await loop.run_in_executor(None, self.owner.mirror_capture.capture_frame,
                                              active, width, quality or None, budget)
            self.capture_sequence += 1
            self.latest_metadata = {
                "stream_id": self.stream_id, "capture_seq": self.capture_sequence,
                "native_width": self.owner.mirror_capture._width,
                "native_height": self.owner.mirror_capture._height,
                "input_id": input_id, "capture_ms": round((time.perf_counter() - started) * 1000, 2),
                "profile": self.profile,
                "uplink_ack_ms": round(self.uplink_ack_ms, 2),
                "viewer_ack_ms": round(self.viewer_ack_ms, 2),
            }
            geometry = (self.latest_metadata['native_width'], self.latest_metadata['native_height'])
            if jpeg and (jpeg != previous or geometry != previous_geometry or force):
                self.pending = (jpeg, dict(self.latest_metadata), time.monotonic())
                previous = jpeg
                previous_geometry = geometry
                self.event.set()
            elif not jpeg and force:
                self.force = True
            try:
                await asyncio.wait_for(self.capture_event.wait(), 1 / (15 if active else 5))
            except asyncio.TimeoutError:
                pass

    async def run(self):
        producer = asyncio.create_task(self.capture())
        producer.add_done_callback(lambda _: self.event.set())
        last_health = 0.0
        try:
            while self.owner.is_mirroring:
                self.event.clear()
                if producer.done():
                    await producer
                    return
                now = time.monotonic()
                if any(now - sent > self.TIMEOUT for sent, _ in self.outstanding.values()):
                    raise TimeoutError("Mirror uplink acknowledgement timed out")
                if now - last_health >= 2:
                    await asyncio.wait_for(self.ws.send(json.dumps({"type": "mirror_health", **self.latest_metadata})), self.TIMEOUT)
                    last_health = time.monotonic()
                if self.pending is not None and len(self.outstanding) < 2:
                    jpeg, metadata, queued_at = self.pending
                    used = sum(size for _, size in self.outstanding.values())
                    if not self.outstanding or used + len(jpeg) <= self.BYTE_WINDOW:
                        self.pending = None
                        self.sequence += 1
                        metadata.update(sequence=self.sequence, uplink_queue_ms=round((now - queued_at) * 1000, 2))
                        self.outstanding[self.sequence] = (time.monotonic(), len(jpeg))
                        await asyncio.wait_for(self.ws.send(b"\x02" + pack_frame(b"MRA2", metadata, jpeg)), self.TIMEOUT)
                        continue
                try:
                    await asyncio.wait_for(self.event.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
