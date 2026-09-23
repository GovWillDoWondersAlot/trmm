"""Tests for the physical-desktop transport; no Windows agent is imported."""
import asyncio
import ast
import json
import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace, ModuleType
import unittest

def load_module(name, relative_path):
    if relative_path == 'agent_client/mirror_transport.py':
        tree = ast.parse((Path(__file__).resolve().parents[1] / relative_path).read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not isinstance(node, ast.ClassDef) or node.name == 'MirrorUplink']
        module = ModuleType(name)
        exec(compile(tree, relative_path, 'exec'), module.__dict__)
        return module
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uplink = load_module('mirror_uplink_test', 'agent_client/mirror_transport.py')
delivery = load_module('mirror_delivery_test', 'master_hub/mirror_delivery.py')
MirrorUplink, pack_frame, unpack_frame = uplink.MirrorUplink, uplink.pack_frame, uplink.unpack_frame
LowLatencyMirrorViewerSession = delivery.LowLatencyMirrorViewerSession


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_quality_modes_preserve_idle_text(self):
        flow = MirrorUplink(None, None, True)
        flow.profile = 2
        self.assertEqual(flow.capture_settings(False), (0, 85, 0))
        self.assertEqual(flow.capture_settings(True)[:2], (1280, 60))
        flow.set_quality_mode('sharp')
        self.assertEqual(flow.capture_settings(True), (0, 85, 0))
        flow.set_quality_mode('fast')
        self.assertEqual(flow.capture_settings(False), flow.FAST_PROFILES[2])
        flow.set_quality_mode('invalid')
        self.assertEqual(flow.quality_mode, 'fast')

    async def test_idle_refinement_without_more_input(self):
        owner = SimpleNamespace(is_mirroring=True, last_input_time=time.time())
        def capture(active, width, quality, budget):
            return f'{width}:{quality}'.encode()
        owner.mirror_capture = SimpleNamespace(capture_frame=capture, _width=1920, _height=1080)
        flow = MirrorUplink(owner, None, True)
        self.start(flow.capture())
        await asyncio.sleep(.1)
        self.assertEqual(flow.pending[0], b'1920:70')
        await asyncio.sleep(.6)
        self.assertEqual(flow.pending[0], b'0:85')

    async def asyncSetUp(self):
        self.tasks = []
        self.packets = asyncio.Queue()
        self.closed = []

        async def send_bytes(packet):
            await self.packets.put(packet)

        async def close(**kwargs):
            self.closed.append(kwargs)

        self.session = LowLatencyMirrorViewerSession('test', SimpleNamespace(send_bytes=send_bytes, close=close))

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def push(self, jpeg):
        self.session.push_frame(jpeg, {'native_width': 1920, 'native_height': 1080})

    async def packet(self):
        return unpack_frame(await asyncio.wait_for(self.packets.get(), 1), b'MRR2')

    def test_envelope_validation(self):
        packet = pack_frame(b'MRA2', {'sequence': 1}, b'jpeg')
        self.assertEqual(unpack_frame(packet, b'MRA2'), ({'sequence': 1}, b'jpeg'))
        self.assertEqual(delivery.unpack_frame(packet, b'MRA2'), ({'sequence': 1}, b'jpeg'))
        self.assertEqual(delivery.pack_frame(b'MRA2', {'sequence': 1}, b'jpeg'), packet)
        for invalid in (b'', packet[:5], packet[:-4], b'MRA2\xff\xff{}jpeg', b'MRA2\x00\x02[]jpeg'):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                unpack_frame(invalid, b'MRA2')

    async def test_two_frames_then_latest_and_no_duplicate_idle(self):
        self.start(self.session.run_sender())
        self.push(b'first')
        first, _ = await self.packet()
        self.push(b'second')
        second, _ = await self.packet()
        for i in range(100):
            self.push(str(i).encode())
        await asyncio.sleep(.02)
        self.assertTrue(self.packets.empty())
        self.assertEqual(len(self.session.outstanding), 2)
        self.assertIsNone(self.session.acknowledge(999))
        self.session.acknowledge(first['sequence'])
        final, jpeg = await self.packet()
        self.assertEqual(jpeg, b'99')
        self.session.acknowledge(second['sequence'])
        self.session.acknowledge(final['sequence'])
        self.push(b'99')
        await asyncio.sleep(.02)
        self.assertTrue(self.packets.empty())
        self.session.refresh()
        _, jpeg = await self.packet()
        self.assertEqual(jpeg, b'99', 'visible tab must recover an unchanged screen')

    async def test_byte_window_and_decode_recovery(self):
        self.start(self.session.run_sender())
        self.push(b'a' * 40000)
        first, _ = await self.packet()
        self.push(b'b' * 40000)
        await asyncio.sleep(.02)
        self.assertTrue(self.packets.empty(), 'two large frames exceeded byte window')
        self.session.acknowledge(first['sequence'])
        current, _ = await self.packet()
        for _ in range(3):
            self.session.acknowledge(current['sequence'], decoded=False)
            if self.session.decode_failures < 3:
                current, jpeg = await self.packet()
                self.assertEqual(jpeg, b'b' * 40000)
        await asyncio.wait_for(self.tasks[0], 1)
        self.assertEqual(self.closed[0]['code'], 1011)

    async def test_missing_ack_expires(self):
        self.session.TIMEOUT = .01
        self.push(b'frame')
        await asyncio.wait_for(self.session.run_sender(), 1)
        self.assertEqual(self.closed[0]['code'], 1013)

    async def test_native_geometry_change_is_not_suppressed(self):
        self.start(self.session.run_sender())
        self.push(b'identical-preview')
        first, _ = await self.packet()
        self.session.acknowledge(first['sequence'])
        self.session.push_frame(b'identical-preview', {'native_width': 2560, 'native_height': 1440})
        updated, _ = await self.packet()
        self.assertEqual(updated['native_width'], 2560)

    async def test_capture_continues_during_blocked_write_and_keeps_latest(self):
        owner = SimpleNamespace(is_mirroring=True, last_input_time=time.time())
        capture_count = 0
        latest = b'first'
        started = asyncio.Event()
        release = asyncio.Event()
        received = asyncio.Queue()

        def capture(*args):
            nonlocal capture_count
            capture_count += 1
            return latest

        async def send(packet):
            if isinstance(packet, bytes):
                started.set()
                await release.wait()
                await received.put(unpack_frame(packet[1:], b'MRA2'))

        owner.mirror_capture = SimpleNamespace(capture_frame=capture, _width=1920, _height=1080)
        flow = MirrorUplink(owner, SimpleNamespace(send=send), True)
        self.start(flow.run())
        await asyncio.wait_for(started.wait(), 1)
        count_before = capture_count
        latest = b'folder-open'
        flow.capture_event.set()
        await asyncio.sleep(.15)
        self.assertGreater(capture_count, count_before)
        self.assertEqual(flow.pending[0], b'folder-open')
        release.set()
        first, _ = await asyncio.wait_for(received.get(), 1)
        final, jpeg = await asyncio.wait_for(received.get(), 1)
        self.assertEqual(jpeg, b'folder-open')
        flow.acknowledge({'stream_id': 'old-session', 'sequence': first['sequence']})
        self.assertEqual(len(flow.outstanding), 2)
        for frame in (first, final):
            flow.acknowledge(frame)
        await asyncio.sleep(.15)
        self.assertTrue(received.empty(), 'unchanged capture must not consume network bandwidth')
        flow.refresh()
        _, jpeg = await asyncio.wait_for(received.get(), 1)
        self.assertEqual(jpeg, b'folder-open')

    def test_adaptation_has_hysteresis(self):
        flow = MirrorUplink(None, None, True)
        flow.feedback(700)
        self.assertEqual(flow.profile, 1)
        flow.feedback(700)
        self.assertEqual(flow.profile, 1, 'profile changed too quickly')
        flow.last_adjustment = 0
        flow.feedback(700)
        self.assertEqual(flow.profile, 2)
        flow.last_adjustment = 0
        for _ in range(8):
            flow.feedback(100)
        self.assertEqual(flow.profile, 1)


if __name__ == '__main__':
    unittest.main()
