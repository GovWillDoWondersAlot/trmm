// Exercise only the Take Control renderer, with delayed JPEG decoding.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../master_hub/static/viewer.html'), 'utf8');
const start = html.indexOf('        function acknowledgeMirrorFrame(');
const source = html.slice(start, html.indexOf('        function resizeDisplay()', start));
const draws = [];
const decodes = [];
const acknowledgements = [];
const socket = { readyState: 1, send(text) { acknowledgements.push(JSON.parse(text)); } };
const context = vm.createContext({
    sessionMode: 'mirror', pendingFrameBlob: { id: 1, mirrorSequence: 1, mirrorSocket: socket }, isRenderingFrame: false,
    WebSocket: { OPEN: 1 },
    performance, console, URLSearchParams, location: { search: '' },
    document: { hidden: false }, window: { createImageBitmap: true },
    hvncCanvas: { width: 100, height: 100 }, resizeDisplay() {},
    hvncCtx: { drawImage(bitmap) { draws.push(bitmap.id); } },
    createImageBitmap(blob) {
        return new Promise(resolve => decodes.push(() => resolve({
            id: blob.id, width: 100, height: 100, close() {}
        })));
    },
    requestAnimationFrame() { throw new Error('Mirror must drain without another animation callback'); }
});
vm.runInContext(source, context);
(async () => {
    const first = context.processNextFrame();
    context.pendingFrameBlob = { id: 2 };
    await context.processNextFrame(); // Duplicate callback while the first decode is pending.
    assert.equal(decodes.length, 1, 'overlapping decode');
    context.pendingFrameBlob = { id: 3, mirrorSequence: 3, mirrorSocket: socket }; // Latest frame wins during slow decode.
    assert.equal(acknowledgements.length, 0, 'acknowledged before decode');
    decodes.shift()();
    await first;
    assert.equal(decodes.length, 1, 'final pending frame failed to drain');
    decodes.shift()();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(draws, [1, 3]);
    assert.deepEqual(acknowledgements.map(ack => ack.sequence), [1, 3]);
    assert.equal(context.isRenderingFrame, false);
    assert.equal(context.pendingFrameBlob, null);
    class FakeSocket {
        static OPEN = 1;
        constructor(url) { this.url = url; this.readyState = 1; this.sent = []; }
        send(text) { this.sent.push(JSON.parse(text)); }
    }
    Object.assign(context, { WebSocket: FakeSocket, Blob, ArrayBuffer, DataView });
    context.window.location = { protocol: 'https:', host: 'test.invalid' };
    const connectStart = html.indexOf('        function connectViewer(targetId)');
    vm.runInContext(html.slice(connectStart, html.indexOf('        function handleDiagnosticMessage(', connectStart)), context);
    context.connectViewer('test-agent');
    assert.ok(context.viewerWs.url.endsWith('?mode=mirror&mirror_ack=1'));
    assert.equal(context.viewerWs.binaryType, 'arraybuffer');
    const packet = new Uint8Array([77, 82, 82, 49, 0, 0, 0, 7, 255, 216, 255, 217]);
    context.document.hidden = true;
    context.viewerWs.onmessage({ data: packet.buffer, target: context.viewerWs });
    assert.equal(context.viewerWs.sent[0].sequence, 7, 'hidden tab failed to release flow control');
    context.document.hidden = false;
    context.isRenderingFrame = true;
    context.viewerWs.onmessage({ data: packet.buffer, target: context.viewerWs });
    assert.equal(context.pendingFrameBlob.size, 4, 'envelope was not removed from JPEG');
    assert.equal(context.pendingFrameBlob.mirrorSequence, 7);
    console.log('PASS: serialized rendering, final-frame drain, render ACKs, wire envelope, hidden-tab ACK');
})().catch(error => { console.error(error); process.exitCode = 1; });
