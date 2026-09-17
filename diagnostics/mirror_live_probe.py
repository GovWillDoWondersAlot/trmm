"""Authenticated Take Control probe. Credentials arrive on stdin and stay in memory.

First stdin line: JSON username/password. Subsequent lines: sample, click x y,
doubleclick x y, or quit. Each sample records 12 seconds without sending input.
"""
import asyncio
import hashlib
import http.cookiejar
import io
import json
from pathlib import Path
import statistics
import sys
import time
import urllib.request

from PIL import Image
import websockets

BASE = sys.argv[1].rstrip('/')
AUTH_BASE = 'http://127.0.0.1:8000' if '--local-auth' in sys.argv[2:] else BASE
OUTPUT = (Path(__file__).resolve().parent / 'mirror_live'
          / ('wan' if BASE.startswith('https:') else 'local') / str(time.time_ns()))


async def main():
    credentials = json.loads(sys.stdin.readline())
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(cookies))

    def request(path, body=None):
        headers = {'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true'}
        if cookies and AUTH_BASE == 'http://127.0.0.1:8000':
            headers['Cookie'] = '; '.join(f'{cookie.name}={cookie.value}' for cookie in cookies)
        req = urllib.request.Request(AUTH_BASE + path,
            data=json.dumps(body).encode() if body else None,
            headers=headers)
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    request('/api/auth/login', credentials)
    del credentials
    agents = request('/api/agents')['agents']
    print('AUTHENTICATED; agents:', json.dumps([
        {key: agent.get(key) for key in ['agent_id', 'status', 'hostname', 'is_online', 'last_seen', 'version']}
        for agent in agents]), flush=True)
    online = [agent for agent in agents if agent.get('status') == 'online' or agent.get('is_online')]
    if len(online) != 1:
        print('Expected exactly one online agent; stopping.', flush=True)
        return
    agent_id = online[0]['agent_id']
    headers = {'Cookie': '; '.join(f'{cookie.name}={cookie.value}' for cookie in cookies),
               'ngrok-skip-browser-warning': 'true'}
    url = BASE.replace('https://', 'wss://').replace('http://', 'ws://')
    url += f'/ws/viewer/{agent_id}?mode=mirror'
    if '--ack' in sys.argv[2:]:
        url += '&mirror_ack=1'
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with websockets.connect(url, additional_headers=headers, proxy=None,
                                  max_size=16 * 1024 * 1024, max_queue=1) as ws:
        try:
            pong = await ws.ping()
            latency = await asyncio.wait_for(pong, 5)
            print(f'WEBSOCKET PING RTT: {latency * 1000:.1f} ms', flush=True)
        except asyncio.TimeoutError:
            print('WEBSOCKET PING: no pong within 5 seconds', flush=True)
        records = []
        latest = None

        async def receive():
            nonlocal latest
            async for message in ws:
                if not isinstance(message, bytes):
                    try:
                        event = json.loads(message)
                        print('TEXT EVENT:', json.dumps({k: event[k] for k in ('type', 'message', 'error') if k in event}), flush=True)
                    except (ValueError, TypeError):
                        print('Non-JSON text event', flush=True)
                    continue
                started = time.perf_counter()
                sequence = None
                if message.startswith(b'MRR1'):
                    sequence = int.from_bytes(message[4:8], 'big')
                    message = message[8:]
                with Image.open(io.BytesIO(message)) as frame:
                    frame.load()
                    size = frame.size
                records.append({'t': started, 'bytes': len(message),
                                'hash': hashlib.sha256(message).hexdigest()[:16],
                                'decode_ms': (time.perf_counter() - started) * 1000,
                                'size': size})
                latest = message
                if sequence is not None:
                    await ws.send(json.dumps({'type': 'mirror_frame_ack', 'sequence': sequence}))

        async def send_input(payload):
            await ws.send(json.dumps({'type': 'input', 'mode': 'mirror',
                                      'data': {'mode': 'mirror', 'stealth': False, **payload}}))

        receiver = asyncio.create_task(receive())
        sequence = 0
        try:
            while True:
                print('READY: sample | click x y | doubleclick x y | quit', flush=True)
                command = (await asyncio.to_thread(sys.stdin.readline)).strip().split()
                if not command or command[0] == 'quit':
                    break
                if command[0] not in ('sample', 'click', 'doubleclick'):
                    print('Unknown command', flush=True)
                    continue
                if receiver.done():
                    await receiver
                    raise RuntimeError('Stream ended')
                if command[0] != 'sample':
                    x, y = map(int, command[1:])
                    await send_input({'type': 'mousemove', 'x': x, 'y': y})
                    await asyncio.sleep(2)
                start = time.perf_counter()
                baseline = len(records)
                if command[0] != 'sample':
                    for _ in range(2 if command[0] == 'doubleclick' else 1):
                        for kind in ('mousedown', 'mouseup'):
                            await send_input({'type': kind, 'x': x, 'y': y, 'button': 'left'})
                            await asyncio.sleep(0.04)
                await asyncio.sleep(12)
                if receiver.done():
                    await receiver
                    raise RuntimeError('Stream ended during sample')
                rows = records[baseline:]
                gaps = [(b['t'] - a['t']) * 1000 for a, b in zip(rows, rows[1:])]
                sequence += 1
                if latest:
                    (OUTPUT / f'sample-{sequence}.jpg').write_bytes(latest)
                report = {'command': command, 'frames': len(rows),
                          'unique_jpegs': len({r['hash'] for r in rows}),
                          'first_frame_ms': (rows[0]['t'] - start) * 1000 if rows else None,
                          'gap_median_ms': statistics.median(gaps) if gaps else None,
                          'gap_max_ms': max(gaps) if gaps else None,
                          'total_bytes': sum(r['bytes'] for r in rows),
                          'decode_max_ms': max((r['decode_ms'] for r in rows), default=None),
                          'frames_detail': [{**r, 't': r['t'] - start} for r in rows]}
                (OUTPUT / f'sample-{sequence}.json').write_text(json.dumps(report, indent=2))
                print(json.dumps({k: v for k, v in report.items() if k != 'frames_detail'}), flush=True)
                if latest:
                    print('IMAGE:', str(OUTPUT / f'sample-{sequence}.jpg'), flush=True)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
