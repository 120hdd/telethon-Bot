const tabs = await fetch('http://127.0.0.1:9222/json').then((response) => response.json());
const page = tabs.find((item) => item.type === 'page' && item.url.includes('docs-fa.html'));
if (!page) throw new Error('docs-fa.html tab not found');

const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});

let nextId = 0;
const pending = new Map();
socket.addEventListener('message', (event) => {
  const payload = JSON.parse(event.data);
  if (!payload.id || !pending.has(payload.id)) return;
  const { resolve, reject } = pending.get(payload.id);
  pending.delete(payload.id);
  if (payload.error) reject(new Error(payload.error.message));
  else resolve(payload.result);
});

function send(method, params = {}) {
  const id = ++nextId;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

await send('Emulation.setDeviceMetricsOverride', {
  width: 390,
  height: 844,
  deviceScaleFactor: 1,
  mobile: true,
});
await send('Page.reload', { ignoreCache: true });
await new Promise((resolve) => setTimeout(resolve, 500));

const expression = `(() => {
  const vw = document.documentElement.clientWidth;
  const offenders = [...document.querySelectorAll('body *')]
    .map((element) => {
      const rect = element.getBoundingClientRect();
      return {
        tag: element.tagName,
        cls: element.className || '',
        id: element.id || '',
        parent: element.parentElement ? element.parentElement.tagName + '.' + (element.parentElement.className || '') : '',
        text: (element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80),
        left: Math.round(rect.left),
        right: Math.round(rect.right),
        width: Math.round(rect.width),
        scrollWidth: element.scrollWidth,
      };
    })
    .filter((item) => item.right > vw + 1 || item.left < -1)
    .sort((a, b) => b.width - a.width)
    .slice(0, 25);
  return { innerWidth, clientWidth: vw, docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth, bodyRect: document.body.getBoundingClientRect().toJSON(), offenders };
})()`;
const result = await send('Runtime.evaluate', { expression, returnByValue: true });
console.log(JSON.stringify(result.result.value, null, 2));
socket.close();
