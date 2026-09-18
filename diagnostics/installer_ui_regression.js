// Run only address detection functions; no remote-control UI code is executed.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('master_hub/static/app.js', 'utf8');
const start = source.indexOf('function initDefaultServerUrl()');
const end = source.indexOf('/**', source.indexOf('async function detectLanIp(', start) + 1);
assert.equal((source.match(/function detectLanIp\(/g) || []).length, 1, 'duplicate detector');
async function detect(host, protocol, ips = ['192.168.1.125']) {
    const input = { value: '' };
    let requests = 0;
    const context = vm.createContext({
        window: { location: { host, hostname: host.split(':')[0], protocol } },
        document: { getElementById: () => input }, showToast() {},
        fetch: async () => { requests++; return { ok: true, json: async () => ({ local_ips: ips, default_port: 8000 }) }; }
    });
    vm.runInContext(source.slice(start, end), context);
    await context.detectLanIp();
    return { value: input.value, requests };
}
(async () => {
    assert.deepEqual(await detect('hub.swiftvtu.com', 'https:'), { value: 'wss://hub.swiftvtu.com', requests: 0 });
    assert.deepEqual(await detect('192.168.1.125:8000', 'http:'), { value: 'ws://192.168.1.125:8000', requests: 0 });
    assert.deepEqual(await detect('localhost:8000', 'http:'), { value: 'ws://192.168.1.125:8000', requests: 1 });
    assert.deepEqual(await detect('localhost:8000', 'http:', []), { value: 'ws://localhost:8000', requests: 1 });
    assert.ok(source.includes('Setup EXE Build Failed'));
    new vm.Script(source); // Parse the full script without executing other features.
    console.log('PASS: public/LAN/loopback URL detection, single detector, explicit partial-build status, syntax');
})().catch(error => { console.error(error); process.exitCode = 1; });
