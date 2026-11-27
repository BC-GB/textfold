import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { setTimeout as pause } from 'node:timers/promises';

const executable = process.argv[2];
assert(executable, 'Pass the installed TextFold executable');
const temporary = await mkdtemp(path.join(tmpdir(), 'textfold-desktop-smoke-'));
const data = path.join(temporary, 'library');
const child = spawn(executable, ['--remote-debugging-port=0', '--disable-gpu'], {
  env: {
    ...process.env,
    TEXTFOLD_DATA: data,
    TEXTFOLD_SETTINGS_DIR: path.join(temporary, 'settings'),
    TEXTFOLD_NO_MODELS: '1',
  },
  stdio: 'ignore',
  windowsHide: true,
});

let socket;
let launchError;
child.on('error', (error) => { launchError = error; });

async function waitFor(label, read) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (launchError) throw launchError;
    if (child.exitCode !== null) throw new Error('The installed app exited before it was ready');
    try {
      const value = await read();
      if (value) return value;
    } catch {
      // The app and its renderer publish these endpoints after they start.
    }
    await pause(200);
  }
  throw new Error('Timed out waiting for ' + label);
}

try {
  const port = await waitFor('the desktop debugger', async () => {
    const file = await readFile(path.join(data, 'DevToolsActivePort'), 'utf8');
    return Number(file.split('\n')[0]);
  });
  const origin = await waitFor('the local reader', async () => {
    const info = JSON.parse(await readFile(path.join(data, 'server.json'), 'utf8'));
    return /^http:\/\/127\.0\.0\.1:\d{1,5}$/.test(info.url) && info.url;
  });
  const request = await fetch(origin + '/api/papers', { signal: AbortSignal.timeout(5000) });
  assert.equal(request.status, 401, 'direct requests need local authorization');
  const url = await waitFor('the TextFold browser page', async () => {
    const response = await fetch('http://127.0.0.1:' + port + '/json/list', { signal: AbortSignal.timeout(1000) });
    const pages = await response.json();
    return pages.find((page) => page.type === 'page' && page.url === origin + '/')?.webSocketDebuggerUrl;
  });
  socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('The renderer did not finish the fixture')), 15000);
    socket.addEventListener('message', (event) => {
      const response = JSON.parse(event.data);
      if (response.id !== 1) return;
      clearTimeout(timer);
      if (response.error || response.result?.exceptionDetails) {
        const detail = response.result?.exceptionDetails?.exception?.description || response.error?.message || '';
        reject(new Error('The installed renderer could not open the fixture. ' + detail.split('\n')[0]));
      } else {
        resolve(response.result.result.value);
      }
    });
    socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: {
      awaitPromise: true,
      returnByValue: true,
      expression: "(" + String(async () => {
        const imported = await fetch('/api/import', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'fixture.md', text: '# Windows fixture\n\nA sample paragraph.' }),
        });
        if (!imported.ok) return { status: imported.status };
        const doc = await imported.json();
        const page = await fetch('/p/' + encodeURIComponent(doc.paper));
        return { status: page.status, title: (await page.text()).includes('Windows fixture') };
      }) + ")()" } }));
  });
  assert.deepEqual(result, { status: 200, title: true });
  console.log('Installed Electron app opened a private study document and rejected a request without credentials.');
} finally {
  socket?.close();
  if (child.pid && process.platform === 'win32') {
    spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
  } else {
    child.kill();
  }
  await rm(temporary, { recursive: true, force: true, maxRetries: 4 });
}
