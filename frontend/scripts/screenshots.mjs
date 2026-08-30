// screenshot.mjs — headless Chrome screenshots via CDP
import { spawn } from 'node:child_process';
import { writeFile, mkdir, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import http from 'node:http';
import { WebSocket } from 'ws';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(__dirname, '..', 'screenshots');
const PORT = 9222;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE || 'http://localhost:5174';

const SHOTS = [
  { name: '01-login-dark',         route: '/login',        theme: 'dark',  width: 1440, height: 900, login: false },
  { name: '02-dialer-dark',        route: '/dialer',       theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '03-voicemail-dark',     route: '/voicemail',    theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '04-recordings-dark',    route: '/recordings',   theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '05-power-dialer-dark',  route: '/power-dialer', theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '06-tenants-dark',       route: '/tenants',      theme: 'dark',  width: 1440, height: 900, login: true, admin: true },
  { name: '07-calls-dark',         route: '/calls',        theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '08-messages-dark',      route: '/messages',     theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '09-campaigns-dark',     route: '/campaigns',    theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '10-contacts-dark',      route: '/contacts',     theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '11-settings-dark',      route: '/settings',     theme: 'dark',  width: 1440, height: 900, login: true },
  { name: '01-login-light',        route: '/login',        theme: 'light', width: 1440, height: 900, login: false },
  { name: '02-dialer-light',       route: '/dialer',       theme: 'light', width: 1440, height: 900, login: true },
  { name: '03-voicemail-light',    route: '/voicemail',    theme: 'light', width: 1440, height: 900, login: true },
  { name: '04-recordings-light',   route: '/recordings',   theme: 'light', width: 1440, height: 900, login: true },
  { name: '06-tenants-light',      route: '/tenants',      theme: 'light', width: 1440, height: 900, login: true, admin: true },
];

function httpJson(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(new Error('Bad JSON: ' + data.slice(0, 100))); } });
    }).on('error', reject);
  });
}

async function waitForChrome() {
  for (let i = 0; i < 60; i++) {
    try { await httpJson('/json/version'); return; } catch { await new Promise(r => setTimeout(r, 500)); }
  }
  throw new Error('Chrome did not start');
}

class Tab {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.on('message', (raw) => {
      let m; try { m = JSON.parse(raw.toString()); } catch { return; }
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        if (m.error) reject(new Error(m.error.message));
        else resolve(m.result);
      }
    });
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

async function main() {
  await mkdir(OUT, { recursive: true });

  const userDataDir = join(__dirname, '..', '.chrome-profile');
  await rm(userDataDir, { recursive: true, force: true }).catch(() => {});

  const args = [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    '--hide-scrollbars',
    '--disable-dev-shm-usage',
    '--mute-audio',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${userDataDir}`,
    'about:blank',
  ];
  const chrome = spawn(CHROME, args, { stdio: 'ignore', detached: false });
  process.on('exit', () => { try { process.kill(chrome.pid); } catch {} });

  await waitForChrome();
  // Connect to the browser-level WS (use the first tab's debuggerUrl as a gateway)
  const tabs = await httpJson('/json');
  const browserWsUrl = tabs[0]?.webSocketDebuggerUrl;
  if (!browserWsUrl) throw new Error('no browser websocket');
  const browserWs = new WebSocket(browserWsUrl);
  await new Promise((r) => browserWs.once('open', r));
  const browser = new Tab(browserWs);

  for (const s of SHOTS) {
    // Create a fresh target
    const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
    // Attach
    const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
    // Open a per-tab WS by reusing a per-page debugger? Easiest: use the session on the browser WS.
    // The Tab class above doesn't track sessions; extend it.
    const sess = makeSession(browserWs, sessionId);

    await sess.send('Page.enable');
    await sess.send('Runtime.enable');
    await sess.send('Emulation.setDeviceMetricsOverride', {
      width: s.width, height: s.height, deviceScaleFactor: 1, mobile: false,
    });

    const userJson = s.admin
      ? `{email:'admin@admin.com',role:'admin',name:'Admin'}`
      : `{email:'demo@agentops.app',name:'Demo'}`;
    const seed = `
      try {
        localStorage.setItem('agentops.theme', JSON.stringify({ theme: '${s.theme}' }));
        ${s.login ? `localStorage.setItem('agentops.auth', JSON.stringify({ token: 'demo', user: ${userJson} }));` : ''}
      } catch (e) {}
      true;
    `;
    await sess.send('Page.addScriptToEvaluateOnNewDocument', { source: seed });

    await sess.send('Page.navigate', { url: BASE + '/#' + s.route });
    await new Promise(r => setTimeout(r, 2800));
    // Force theme attr + route after load
    await sess.send('Runtime.evaluate', {
      expression: `document.documentElement.setAttribute('data-theme','${s.theme}'); window.location.hash = '${s.route}'; 1+1`,
    });
    await new Promise(r => setTimeout(r, 1200));

    const res = await sess.send('Page.captureScreenshot', { format: 'png' });
    const outPath = join(OUT, `${s.name}.png`);
    await writeFile(outPath, Buffer.from(res.data, 'base64'));
    const sz = (Buffer.byteLength(res.data, 'base64') / 1024).toFixed(0);
    console.log(`✓ ${s.name}.png (${sz} KB)`);

    // Close target
    try { await browser.send('Target.closeTarget', { targetId }); } catch {}
  }

  browserWs.close();
  try { process.kill(chrome.pid); } catch {}
  console.log('done');
  process.exit(0);
}

function makeSession(ws, sessionId) {
  let id = 0;
  const pending = new Map();
  const onMsg = (raw) => {
    let m; try { m = JSON.parse(raw.toString()); } catch { return; }
    if (m.sessionId !== sessionId) return;
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) reject(new Error(m.error.message));
      else resolve(m.result);
    }
  };
  ws.on('message', onMsg);
  return {
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const myId = ++id;
        pending.set(myId, { resolve, reject });
        ws.send(JSON.stringify({ sessionId, id: myId, method, params }));
      });
    },
  };
}

main().catch((e) => { console.error('ERR', e); process.exit(1); });
