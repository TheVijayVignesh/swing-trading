// Browser QA pass for Swing Lab — screenshots + console/network error collection + interactions.
import { chromium } from '/Users/vijay/.local/share/fnm/node-versions/v24.18.0/installation/lib/node_modules/playwright/index.mjs';
import fs from 'fs';

const BASE = 'http://127.0.0.1:8787';
const OUT = 'qa';
fs.mkdirSync(OUT, { recursive: true });

const issues = [];
const consoleErrors = [];
const failedRequests = [];

async function newPage(ctx, label) {
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(`[${label}] ${m.text()}`); });
  page.on('pageerror', e => consoleErrors.push(`[${label}] PAGEERROR ${e.message}`));
  page.on('requestfailed', r => failedRequests.push(`[${label}] ${r.url()} ${r.failure()?.errorText}`));
  page.on('response', r => { if (r.status() >= 400) failedRequests.push(`[${label}] HTTP ${r.status()} ${r.url()}`); });
  return page;
}

const run = async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });

  // ---------- 1. Lab overview
  let page = await newPage(ctx, 'lab');
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/01_lab_overview.png`, fullPage: true });

  // session cards present?
  const cards = await page.locator('.session-card, [data-id]').count();
  console.log('session cards found:', cards);

  // ---------- 2. Create a session through the UI
  await page.goto(BASE + '/sessions/new', { waitUntil: 'networkidle' });
  await page.screenshot({ path: `${OUT}/02_session_new.png`, fullPage: true });
  const hasSymbolField = await page.locator('input[name="symbol"]:visible').count();
  console.log('visible symbol field (must be 0):', hasSymbolField);
  await page.fill('input[name="name"]', 'qa-browser-session');
  await page.fill('input[name="capital_initial"]', '15000');
  // submit via the form's own JS handler
  await page.click('button[type="submit"]');
  await page.waitForTimeout(1500);
  console.log('after create url:', page.url());
  await page.screenshot({ path: `${OUT}/03_after_create.png`, fullPage: true });

  // ---------- 3. Session detail of hybrid-main
  const summary = await (await fetch(BASE + '/api/lab/summary')).json();
  const main = summary.sessions.find(s => s.name === 'hybrid-main');
  if (!main) { issues.push('hybrid-main session missing from summary'); }
  else {
    page = await newPage(ctx, 'detail');
    await page.goto(`${BASE}/sessions/${main.id}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${OUT}/04_session_detail.png`, fullPage: true });
    // lifecycle controls
    for (const lbl of ['Pause', 'Resume', 'Stop']) {
      const n = await page.getByRole('button', { name: new RegExp(lbl, 'i') }).count();
      if (n === 0) issues.push(`detail page missing ${lbl} control`);
    }
    // pause then resume via UI clicks, waiting for the status chip to flip
    await page.getByRole('button', { name: /pause/i }).first().click();
    await page.waitForFunction(
      () => document.querySelector('[data-control="resume"]')?.disabled === false,
      null, { timeout: 15000 });
    const st1 = await (await fetch(`${BASE}/api/sessions/${main.id}`)).json();
    if (st1.status !== 'PAUSED') issues.push(`pause failed, status=${st1.status}`);
    await page.screenshot({ path: `${OUT}/05_paused.png` });
    await page.getByRole('button', { name: /resume/i }).first().click();
    await page.waitForFunction(
      () => document.querySelector('[data-control="pause"]')?.disabled === false,
      null, { timeout: 15000 });
    const st2 = await (await fetch(`${BASE}/api/sessions/${main.id}`)).json();
    if (st2.status !== 'RUNNING') issues.push(`resume failed, status=${st2.status}`);
    // config snapshot panel
    const html = await page.content();
    if (!/pullback-v1/.test(html)) issues.push('config snapshot missing strategy id');
  }

  // ---------- 4. Compare view
  const ids = summary.sessions.slice(0, Math.min(3, summary.sessions.length)).map(s => s.id).join(',');
  page = await newPage(ctx, 'compare');
  await page.goto(`${BASE}/compare?ids=${ids}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/06_compare.png`, fullPage: true });

  // ---------- 5. Dark mode
  page = await newPage(ctx, 'dark');
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: /theme|dark|☀|☾|☾|sun|moon/i }).first().click().catch(() => {});
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/07_dark_mode.png`, fullPage: true });

  // ---------- 6. Mobile viewport
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  page = await newPage(mctx, 'mobile');
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${OUT}/08_mobile_lab.png`, fullPage: true });
  const hscroll = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2);
  if (hscroll) issues.push('mobile: horizontal overflow on lab overview');

  // ---------- 7. Recovery: backend restart while session RUNNING
  const st = await (await fetch(`${BASE}/api/sessions/${main.id}`)).json();
  console.log('pre-restart status:', st.status);

  await browser.close();
  fs.writeFileSync(`${OUT}/qa_results.json`, JSON.stringify({ issues, consoleErrors, failedRequests }, null, 2));
  console.log('\nISSUES:', issues.length); issues.forEach(i => console.log(' -', i));
  console.log('CONSOLE ERRORS:', consoleErrors.length); consoleErrors.slice(0, 10).forEach(i => console.log(' -', i));
  console.log('FAILED REQUESTS:', failedRequests.length); failedRequests.slice(0, 10).forEach(i => console.log(' -', i));
};
run().catch(e => { console.error('QA CRASH:', e); process.exit(1); });
