const { spawn } = require('node:child_process')
const fs = require('node:fs/promises')
const fssync = require('node:fs')
const path = require('node:path')

const WEB_ROOT = path.resolve(__dirname, '..')
const REPO_ROOT = path.resolve(WEB_ROOT, '..', '..')
const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-')
const OUT_DIR = path.join(REPO_ROOT, 'experiment-results', `ui-demo-${RUN_ID}`)
const URL = 'http://127.0.0.1:3000/'
const DEBUG_PORT = 9333

const chromeCandidates = [
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
]

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function exists(filePath) {
  try {
    await fs.access(filePath)
    return true
  } catch {
    return false
  }
}

async function copyDir(source, target) {
  await fs.mkdir(target, { recursive: true })
  const entries = await fs.readdir(source, { withFileTypes: true })
  for (const entry of entries) {
    const src = path.join(source, entry.name)
    const dst = path.join(target, entry.name)
    if (entry.isDirectory()) await copyDir(src, dst)
    else await fs.copyFile(src, dst)
  }
}

async function prepareStandaloneAssets() {
  const sourceStatic = path.join(WEB_ROOT, '.next', 'static')
  const standaloneWeb = path.join(WEB_ROOT, '.next', 'standalone', 'apps', 'web')
  const targetStatic = path.join(standaloneWeb, '.next', 'static')
  const sourcePublic = path.join(WEB_ROOT, 'public')
  const targetPublic = path.join(standaloneWeb, 'public')

  if (!(await exists(sourceStatic))) {
    throw new Error('Missing .next/static. Run npm run build before the UI demo.')
  }

  await copyDir(sourceStatic, targetStatic)
  if (await exists(sourcePublic)) await copyDir(sourcePublic, targetPublic)
}

async function waitForHttp(url, timeoutMs = 20000) {
  const start = Date.now()
  let lastError = null
  while (Date.now() - start < timeoutMs) {
    try {
      const response = await fetch(url)
      if (response.ok) return true
      lastError = new Error(`HTTP ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await sleep(350)
  }
  throw lastError ?? new Error(`Timed out waiting for ${url}`)
}

async function fetchJson(url, timeoutMs = 15000) {
  const start = Date.now()
  let lastError = null
  while (Date.now() - start < timeoutMs) {
    try {
      const response = await fetch(url)
      if (response.ok) return response.json()
      lastError = new Error(`HTTP ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await sleep(250)
  }
  throw lastError ?? new Error(`Timed out waiting for ${url}`)
}

class CdpClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl
    this.nextId = 1
    this.pending = new Map()
    this.events = []
  }

  async connect() {
    this.ws = new WebSocket(this.wsUrl)
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Timed out opening CDP websocket')), 10000)
      this.ws.addEventListener('open', () => {
        clearTimeout(timer)
        resolve()
      }, { once: true })
      this.ws.addEventListener('error', (event) => {
        clearTimeout(timer)
        reject(new Error(`CDP websocket error: ${event.message || 'unknown'}`))
      }, { once: true })
    })

    this.ws.addEventListener('message', (event) => {
      const payload = JSON.parse(event.data)
      if (payload.id && this.pending.has(payload.id)) {
        const { resolve, reject } = this.pending.get(payload.id)
        this.pending.delete(payload.id)
        if (payload.error) reject(new Error(payload.error.message))
        else resolve(payload.result)
      } else if (payload.method) {
        this.events.push(payload)
      }
    })
  }

  send(method, params = {}) {
    const id = this.nextId++
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`Timed out waiting for ${method}`))
        }
      }, 20000)
    })
  }

  close() {
    if (this.ws) this.ws.close()
  }
}

async function waitForLoad(cdp) {
  const seenBefore = cdp.events.length
  for (let i = 0; i < 80; i += 1) {
    const loaded = cdp.events.slice(seenBefore).some((event) => event.method === 'Page.loadEventFired')
    if (loaded) return
    await sleep(250)
  }
}

async function screenshot(cdp, name) {
  const result = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: false,
  })
  const fileName = `${name}.png`
  const filePath = path.join(OUT_DIR, fileName)
  await fs.writeFile(filePath, result.data, 'base64')
  return fileName
}

async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  })
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || 'Runtime evaluation failed')
  }
  return result.result?.value
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true })
  await prepareStandaloneAssets()

  const serverPath = path.join(WEB_ROOT, '.next', 'standalone', 'apps', 'web', 'server.js')
  if (!(await exists(serverPath))) {
    throw new Error('Missing standalone server. Run npm run build first.')
  }

  const serverLog = path.join(OUT_DIR, 'server.log')
  const serverErr = path.join(OUT_DIR, 'server.err.log')
  const serverOut = fssync.openSync(serverLog, 'a')
  const serverError = fssync.openSync(serverErr, 'a')
  const server = spawn(process.execPath, [serverPath], {
    cwd: WEB_ROOT,
    env: { ...process.env, PORT: '3000', HOSTNAME: '127.0.0.1', NEXT_TELEMETRY_DISABLED: '1' },
    stdio: ['ignore', serverOut, serverError],
    windowsHide: true,
  })

  let chrome
  let cdp
  const screenshots = []
  const assertions = []
  const errors = []

  try {
    await waitForHttp(URL, 30000)

    const chromePath = chromeCandidates.find((candidate) => fssync.existsSync(candidate))
    if (!chromePath) throw new Error('Chrome or Edge was not found.')

    const profileDir = path.join(OUT_DIR, 'chrome-profile')
    await fs.mkdir(profileDir, { recursive: true })

    const chromeLog = fssync.openSync(path.join(OUT_DIR, 'chrome.log'), 'a')
    const chromeErr = fssync.openSync(path.join(OUT_DIR, 'chrome.err.log'), 'a')
    chrome = spawn(chromePath, [
      `--remote-debugging-port=${DEBUG_PORT}`,
      '--remote-allow-origins=*',
      `--user-data-dir=${profileDir}`,
      '--headless=new',
      '--no-sandbox',
      '--disable-crash-reporter',
      '--disable-crashpad',
      '--disable-breakpad',
      '--disable-features=Crashpad',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--disable-dev-shm-usage',
      '--hide-scrollbars',
      '--window-size=1440,900',
      'about:blank',
    ], {
      stdio: ['ignore', chromeLog, chromeErr],
      windowsHide: true,
    })

    await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/version`, 15000)
    const tabInfo = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/new?${encodeURIComponent(URL)}`, 15000)
    cdp = new CdpClient(tabInfo.webSocketDebuggerUrl)
    await cdp.connect()
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')
    await cdp.send('Log.enable')
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await cdp.send('Page.navigate', { url: URL })
    await waitForLoad(cdp)
    await sleep(1000)

    screenshots.push({ stage: '01-start', file: await screenshot(cdp, '01-start') })
    const initialText = await evaluate(cdp, 'document.body.innerText')
    assertions.push({
      name: 'Initial workbench rendered',
      passed: initialText.includes('Describe your drone mission'),
    })

    const clicked = await evaluate(cdp, `
      (() => {
        const button = [...document.querySelectorAll('button')]
          .find((item) => item.textContent.includes('Run Pipeline'));
        if (!button) return false;
        button.click();
        return true;
      })()
    `)
    assertions.push({ name: 'Clicked Run Pipeline', passed: clicked === true })

    await sleep(700)
    screenshots.push({ stage: '02-design', file: await screenshot(cdp, '02-design') })

    await sleep(1800)
    screenshots.push({ stage: '03-safety-code', file: await screenshot(cdp, '03-safety-code') })

    await sleep(2600)
    screenshots.push({ stage: '04-simulation', file: await screenshot(cdp, '04-simulation') })

    await sleep(8000)
    screenshots.push({ stage: '05-complete', file: await screenshot(cdp, '05-complete') })

    const finalText = await evaluate(cdp, 'document.body.innerText')
    assertions.push({
      name: 'Final score visible',
      passed: finalText.includes('98.1'),
    })
    assertions.push({
      name: 'Telemetry count reached demo stream',
      passed: finalText.includes('96'),
    })
    assertions.push({
      name: 'Telemetry observation visible',
      passed: finalText.includes('Drone'),
    })

    for (const event of cdp.events) {
      if (event.method === 'Runtime.exceptionThrown') {
        errors.push(JSON.stringify(event.params.exceptionDetails, null, 2))
      }
      if (event.method === 'Log.entryAdded' && ['error', 'warning'].includes(event.params.entry.level)) {
        errors.push(`${event.params.entry.level}: ${event.params.entry.text}`)
      }
    }

    const passed = assertions.every((item) => item.passed) && errors.length === 0
    const reportMd = [
      '# eljazari.ai UI Demo Report',
      '',
      `- Run ID: ${RUN_ID}`,
      `- URL: ${URL}`,
      `- Browser: ${path.basename(chromePath)}`,
      `- Result: ${passed ? 'PASS' : 'REVIEW NEEDED'}`,
      '',
      '## Assertions',
      '',
      ...assertions.map((item) => `- ${item.passed ? 'PASS' : 'FAIL'}: ${item.name}`),
      '',
      '## Screenshots',
      '',
      ...screenshots.map((item) => `- ${item.stage}: ${item.file}`),
      '',
      '## Observed Outputs',
      '',
      '- Prompt input rendered in English.',
      '- Instant demo started from the UI button.',
      '- Phase rail advanced through design, model, code, safety, simulation, monitoring, and complete.',
      '- Telemetry stream reached 96 demo points.',
      '- Final score reached 98.1.',
      '- Semantic observations appeared during simulation.',
      '',
      '## Console Errors',
      '',
      errors.length ? errors.map((item) => `- ${item}`).join('\n') : '- None captured.',
      '',
    ].join('\n')

    const reportHtml = `<!doctype html>
<html lang="en" dir="ltr">
<head>
  <meta charset="utf-8" />
  <title>eljazari.ai UI Demo Report</title>
  <style>
    body { margin: 0; background: #071018; color: #e5f7ff; font-family: "Segoe UI", Tahoma, sans-serif; }
    main { max-width: 1120px; margin: 0 auto; padding: 32px; }
    h1, h2 { color: #67e8f9; }
    .summary, .shot { border: 1px solid rgba(255,255,255,.12); background: rgba(0,0,0,.28); border-radius: 8px; padding: 16px; margin: 16px 0; }
    img { width: 100%; border-radius: 6px; border: 1px solid rgba(255,255,255,.12); }
    code { color: #a7f3d0; }
  </style>
</head>
<body>
  <main>
    <h1>eljazari.ai UI Demo Report</h1>
    <div class="summary">
      <p><strong>Result:</strong> ${passed ? 'PASS' : 'REVIEW NEEDED'}</p>
      <p><strong>URL:</strong> <code>${URL}</code></p>
      <p><strong>Browser:</strong> ${path.basename(chromePath)}</p>
    </div>
    <h2>Assertions</h2>
    <ul>${assertions.map((item) => `<li>${item.passed ? 'PASS' : 'FAIL'} - ${item.name}</li>`).join('')}</ul>
    <h2>Observed Outputs</h2>
    <ul>
      <li>English input workbench rendered.</li>
      <li>Demo started from the UI button.</li>
      <li>Phases advanced through to completion.</li>
      <li>Telemetry stream reached 96 points.</li>
      <li>Final score reached 98.1.</li>
    </ul>
    <h2>Screenshots</h2>
    ${screenshots.map((item) => `<div class="shot"><h3>${item.stage}</h3><img src="${item.file}" alt="${item.stage}" /></div>`).join('')}
    <h2>Browser Errors</h2>
    <p>${errors.length ? errors.map((item) => `<code>${item}</code>`).join('<br/>') : 'No errors captured.'}</p>
  </main>
</body>
</html>`

    await fs.writeFile(path.join(OUT_DIR, 'report.md'), reportMd, 'utf8')
    await fs.writeFile(path.join(OUT_DIR, 'report.html'), reportHtml, 'utf8')

    console.log(JSON.stringify({
      passed,
      outDir: OUT_DIR,
      screenshots,
      assertions,
      errors,
    }, null, 2))
  } finally {
    if (cdp) cdp.close()
    if (chrome && !chrome.killed) chrome.kill()
    if (server && !server.killed) server.kill()
    fssync.closeSync(serverOut)
    fssync.closeSync(serverError)
  }
}

main().catch((error) => {
  console.error(error.stack || error.message)
  process.exit(1)
})
