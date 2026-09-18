"""Isolated Take Control checks. Does not import or run the agent or hub."""
import asyncio
import ctypes
import io
import logging
import os
from pathlib import Path
import textwrap
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def extract(path, start, end):
    source = (ROOT / path).read_text(encoding="utf-8")
    return textwrap.dedent(source[source.index(start):source.index(end, source.index(start))])


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_gets_latest_only_after_matching_ack(self):
        namespace = dict(asyncio=asyncio, Optional=Optional, WebSocket=object,
                         os=os, time=time, logger=logging.getLogger("test"))
        exec(extract("master_hub/agent_manager.py", "class ViewerSession:", "class ConnectedAgent:"), namespace)
        packets = asyncio.Queue()
        texts = asyncio.Queue()

        async def send_bytes(packet):
            await packets.put(packet)

        async def send_text(text):
            await texts.put(text)

        session = namespace["MirrorViewerSession"]("test", SimpleNamespace(send_bytes=send_bytes, send_text=send_text))
        task = asyncio.create_task(session.run_sender())
        try:
            session.push_frame(b"before-click")
            first = await asyncio.wait_for(packets.get(), 1)
            self.assertEqual(first, b"MRR1\x00\x00\x00\x01before-click")
            for i in range(100):
                session.push_frame(str(i).encode())
            session.push_frame(b"folder-open")
            session.push_text("status")
            self.assertEqual(await asyncio.wait_for(texts.get(), 1), "status")
            session.acknowledge(999)
            await asyncio.sleep(0.02)
            self.assertTrue(packets.empty())
            session.acknowledge(1)
            final = await asyncio.wait_for(packets.get(), 1)
            self.assertEqual(final, b"MRR1\x00\x00\x00\x02folder-open")
            session.acknowledge(1)  # A stale ACK must not release frame 2.
            self.assertEqual(session.inflight, 2)
            session.acknowledge(2)
            session.push_frame(b"folder-open")
            await asyncio.sleep(0.02)
            self.assertTrue(packets.empty(), "identical frame flooded the viewer")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_ack_closes_instead_of_accumulating_frames(self):
        namespace = dict(asyncio=asyncio, Optional=Optional, WebSocket=object,
                         os=os, time=time, logger=logging.getLogger("test"))
        exec(extract("master_hub/agent_manager.py", "class ViewerSession:", "class ConnectedAgent:"), namespace)
        sent = []
        closed = []

        async def send_bytes(packet):
            sent.append(packet)

        async def close(**kwargs):
            closed.append(kwargs)

        session = namespace["MirrorViewerSession"]("test", SimpleNamespace(send_bytes=send_bytes, close=close))
        session.ACK_TIMEOUT = 0.05
        session.push_frame(b"first")
        await asyncio.wait_for(session.run_sender(), 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(closed[0]['code'], 1013)

    async def test_active_input_does_not_flood_identical_jpegs(self):
        namespace = dict(asyncio=asyncio, os=os, time=time, logger=logging.getLogger("test"))
        exec(extract("agent_client/client.py", "    async def _mirror_stream_loop(", "    @staticmethod"), namespace)
        state = SimpleNamespace(is_mirroring=True, last_input_time=time.time())
        captures = 0
        sent = []

        def capture(active):
            nonlocal captures
            self.assertTrue(active)
            captures += 1
            if captures == 8:
                state.is_mirroring = False
            return b"unchanged-desktop"

        async def send(frame):
            sent.append(frame)

        state.mirror_capture = SimpleNamespace(capture_frame=capture)
        await asyncio.wait_for(namespace["_mirror_stream_loop"](state, SimpleNamespace(send=send)), 2)
        self.assertEqual(sent, [b"\x02unchanged-desktop"])

    async def test_changed_frame_after_input_stops_and_slow_send(self):
        namespace = dict(asyncio=asyncio, os=os, time=time, logger=logging.getLogger("test"))
        exec(extract("agent_client/client.py", "    async def _mirror_stream_loop(", "    @staticmethod"), namespace)
        state = SimpleNamespace(is_mirroring=True, last_input_time=0)
        captures = 0
        sent = []

        def capture(active):
            nonlocal captures
            self.assertFalse(active)
            captures += 1
            return b"folder-closed" if captures < 3 else b"folder-open"

        async def send(frame):
            # Artificial uplink backpressure; no further input is delivered.
            await asyncio.sleep(0.12)
            sent.append(frame)
            if frame == b"\x02folder-open":
                state.is_mirroring = False

        state.mirror_capture = SimpleNamespace(capture_frame=capture)
        await asyncio.wait_for(namespace["_mirror_stream_loop"](state, SimpleNamespace(send=send)), 2)
        self.assertEqual(sent[-1], b"\x02folder-open")

    async def test_relay_retains_latest_after_burst(self):
        namespace = dict(asyncio=asyncio, Optional=Optional, WebSocket=object,
                         os=os, time=time, logger=logging.getLogger("test"))
        exec(extract("master_hub/agent_manager.py", "class ViewerSession:", "class ConnectedAgent:"), namespace)
        session = namespace["ViewerSession"]("mirror-test", None)
        for i in range(100):
            session.push_frame(bytes([i]))
        self.assertEqual(session.queue.qsize(), 2)
        self.assertEqual(session.queue.get_nowait(), (bytes([98]), True))
        self.assertEqual(session.queue.get_nowait(), (bytes([99]), True))

    async def test_capture_flush_cache_quality_and_failure(self):
        pixels = ctypes.create_string_buffer(bytes([20, 40, 60, 0]) * 4)
        calls = []
        gdi = SimpleNamespace(BitBlt=lambda *args: calls.append("copy") or 1,
                              GdiFlush=lambda: calls.append("flush") or 1)
        namespace = dict(time=time, os=os, ctypes=ctypes, io=io, Image=Image, Optional=Optional,
                         SRCCOPY=0, logger=logging.getLogger("test"), gdi32=gdi,
                         user32=SimpleNamespace(OpenDesktopW=lambda *a: 0,
                                                GetDC=lambda *a: 1, ReleaseDC=lambda *a: 1))
        exec(extract("hvnc/mirror.py", "    def _capture_frame(", "    def render_frame("), namespace)
        capture = SimpleNamespace(_width=2, _height=2, _hdc_mem=1,
                                  _p_bits=SimpleNamespace(value=ctypes.addressof(pixels)),
                                  _last_raw_bytes=None, _last_frame_bytes=None,
                                  _refresh_screen_size=lambda: None, _ensure_buffer=lambda *a: True)
        fn = namespace["_capture_frame"]
        first = fn(capture, True)
        self.assertEqual(calls, ["copy", "flush"])
        self.assertIs(fn(capture, True), first)
        idle = fn(capture, False)
        self.assertEqual(capture._last_frame_key, (2, 2, 65, 0, 0))
        self.assertIsNot(idle, first)
        gdi.BitBlt = lambda *a: 0
        self.assertIsNone(fn(capture, False))


if __name__ == "__main__":
    unittest.main()
