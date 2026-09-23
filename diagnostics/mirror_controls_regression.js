// Isolated physical-desktop controls: no browser or remote input is used.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname, '../master_hub/static/viewer.html'), 'utf8');
function extract(name, end) {
    const start = html.indexOf(`        function ${name}(`);
    return html.slice(start, html.indexOf(end, start));
}
const sent = [];
const context = vm.createContext({
    sessionMode: 'mirror', screenCurtainActive: false, stealthMouseMode: false,
    WebSocket: { OPEN: 1 }, viewerWs: { readyState: 1, send: s => sent.push(JSON.parse(s)) },
    document: { getElementById: () => ({value: 'sharp', style: {}, classList: {add() {}, remove() {}}}) },
    showToast() {}, toggleStealthMouse() {},
    sendViewerInput() { throw Error('Duplicate curtain input sent'); }
});
vm.runInContext(extract('toggleScreenCurtain', '        window.addEventListener("beforeunload"'), context);
vm.runInContext(extract('setMirrorQuality', '        function acknowledgeMirrorFrame('), context);
context.toggleScreenCurtain();
context.toggleScreenCurtain();
assert.deepStrictEqual(sent, [{type: 'set_curtain', enabled: true}, {type: 'set_curtain', enabled: false}]);
context.setMirrorQuality();
assert.deepStrictEqual(sent[2], {type: 'mirror_quality', quality: 'sharp'});
context.viewerWs.readyState = 3;
context.setMirrorQuality();
assert.equal(sent.length, 3);
console.log('PASS: one curtain command per toggle and quality selection');
