// 印标工作台 采集助手：向本机的印标工作台领取采集任务，在一个最小化的窗口里打开 1688 页面读取数据。
// 用的是你自己的 Chrome 和登录状态；遇到登录/人机验证时把窗口弹出来让你处理，处理完自动缩回去继续。

const SERVER = 'http://127.0.0.1:8765';
const VERSION = chrome.runtime.getManifest().version;
const OFFER_RE = /(?:offer\/|offerId=|offer_id=)(\d{8,15})/g;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand = (a, b) => a + Math.random() * (b - a);

async function post(path, body) {
  const r = await fetch(SERVER + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return r.json().catch(() => ({}));
}

// ---------------- 采集窗口 ----------------
async function workerTab() {
  const { win, tab } = await chrome.storage.session.get(['win', 'tab']);
  if (tab) {
    try {
      const t = await chrome.tabs.get(tab);
      if (t.windowId === win) return { win, tab };
    } catch (e) { /* 窗口被关掉了，重新开一个 */ }
  }
  const w = await chrome.windows.create({ url: 'about:blank', focused: false, state: 'minimized' });
  const ids = { win: w.id, tab: w.tabs[0].id };
  await chrome.storage.session.set(ids);
  return ids;
}

function waitLoaded(tabId, timeout = 45000) {
  return new Promise(resolve => {
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve(ok);
    };
    const listener = (id, info) => { if (id === tabId && info.status === 'complete') finish(true); };
    chrome.tabs.onUpdated.addListener(listener);
    const timer = setTimeout(() => finish(false), timeout);
  });
}

async function navigate(tabId, url) {
  const loaded = waitLoaded(tabId);
  await chrome.tabs.update(tabId, { url });
  await loaded;
  await sleep(rand(1200, 2200));
}

async function exec(tabId, func, args = []) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func, args });
  return res ? res.result : undefined;
}

// ---------------- 页面内执行的函数（必须自包含） ----------------
function pageExtract() {
  const seen = new WeakSet();
  const safe = v => JSON.parse(JSON.stringify(v, (k, val) => {
    if (typeof val === 'function') return undefined;
    if (val && typeof val === 'object') { if (seen.has(val)) return undefined; seen.add(val); }
    return val;
  }));
  const globals = {};
  for (const k of ['context', '__INIT_DATA', '__GLOBAL_DATA', 'iDetailData', 'iDetailConfig', '__STORE_DATA', 'runParams']) {
    try { if (window[k]) globals[k] = safe(window[k]); } catch (e) { /* 忽略 */ }
  }
  const gallery = [...document.querySelectorAll(
    '.detail-gallery-turn img, .img-list-wrapper img, .detail-gallery-img, #dt-tab img, [class*="gallery"] img')]
    .map(i => i.getAttribute('src') || i.getAttribute('data-lazy-src') || '').filter(Boolean);
  const titleEl = document.querySelector('.title-text, .d-title, .od-pc-offer-title-contain h1, h1');
  return {
    globals, gallery,
    h1: titleEl ? titleEl.innerText.trim() : '',
    title: document.title,
    // 数据对象都没找到时才带上整页 HTML，供服务端兜底解析
    html: Object.keys(globals).length ? '' : document.documentElement.outerHTML.slice(0, 4000000),
  };
}

function pageBlocked() {
  const u = location.href;
  if (u.includes('login.') || u.includes('/member/signin')) return 'login';
  if (u.includes('punish') || u.includes('_____tmd_____')) return 'verify';
  for (const sel of ['#nc_1_n1z', '#baxia-dialog-content', '.baxia-dialog', "iframe[src*='punish']"]) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) return 'verify';
  }
  return '';
}

async function pageScrollAndCollect() {
  for (let i = 0; i < 10; i++) {
    window.scrollBy(0, 400 + Math.random() * 400);
    await new Promise(r => setTimeout(r, 300 + Math.random() * 400));
  }
  return document.documentElement.outerHTML;
}

// ---------------- 登录 / 验证 ----------------
// 检查页面是否停在登录/验证页。页面还在加载时脚本可能注入失败，多试几次，不要误判成验证
async function blockedState(tabId) {
  let lastErr;
  for (let i = 0; i < 15; i++) {
    try { return await exec(tabId, pageBlocked); } catch (e) { lastErr = e; await sleep(1000); }
  }
  throw new Error('页面打不开：' + ((lastErr && lastErr.message) || '加载失败'));
}

async function waitUnblocked(win, tabId) {
  let b = await blockedState(tabId);
  if (!b) return false;
  await post('/api/ext/status', {
    status: b, message: b === 'login' ? '请在弹出的 Chrome 窗口中登录 1688' : '请在弹出的 Chrome 窗口中完成人机验证',
  });
  await chrome.windows.update(win, { state: 'normal', focused: true });
  const t0 = Date.now();
  for (let i = 1; Date.now() - t0 < 14 * 60 * 1000; i++) {
    await sleep(2000);
    try { await chrome.tabs.get(tabId); } catch (e) { throw new Error('采集窗口被关闭了'); }
    b = await exec(tabId, pageBlocked).catch(() => b); // 验证过程中页面会跳转，注入失败时沿用上次状态
    if (!b) break;
    if (i % 5 === 0) await post('/api/ext/status', { status: b, message: '' }).catch(() => {}); // 定时报到
  }
  if (b) throw new Error('等待登录/验证超时');
  await post('/api/ext/status', { status: '', message: '验证已通过，继续采集' });
  await sleep(rand(3000, 6000));
  await chrome.windows.update(win, { state: 'minimized' }).catch(() => {});
  return true;
}

// ---------------- 任务 ----------------
async function doOffer(task) {
  const { win, tab } = await workerTab();
  await navigate(tab, task.url);
  if (await waitUnblocked(win, tab)) {
    // 验证后 1688 有时会跳回首页，确认还在这个商品页
    const t = await chrome.tabs.get(tab);
    if (!t.url.includes(task.offer_id)) {
      await navigate(tab, task.url);
      await waitUnblocked(win, tab);
    }
  }
  const raw = await exec(tab, pageExtract);
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { raw } });
}

async function doShop(task, delay) {
  const { win, tab } = await workerTab();
  let base = task.url.trim();
  if (!base.startsWith('http')) base = 'https://' + base;
  const origin = base.match(/^https?:\/\/[^/]+/)[0];
  const listUrl = base.includes('offerlist') ? base : origin + '/page/offerlist.htm';
  const ids = [];
  for (let page = 1; page <= task.max_pages; page++) {
    const url = /pageNum=\d+/.test(listUrl)
      ? listUrl.replace(/pageNum=\d+/, 'pageNum=' + page)
      : listUrl + (listUrl.includes('?') ? '&' : '?') + 'pageNum=' + page;
    await navigate(tab, url);
    await waitUnblocked(win, tab);
    const html = await exec(tab, pageScrollAndCollect);
    let added = 0;
    for (const m of (html || '').matchAll(OFFER_RE)) {
      if (!ids.includes(m[1])) { ids.push(m[1]); added++; }
    }
    if (!added) break;
    await sleep(rand(delay, delay * 2) * 1000);
  }
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { ids } });
}

async function doTabs(task) {
  const tabs = await chrome.tabs.query({ url: '*://detail.1688.com/offer/*' });
  const pages = [];
  for (const t of tabs) {
    const m = t.url.match(/offer\/(\d{8,15})/);
    if (!m) continue;
    try {
      if (await exec(t.id, pageBlocked)) continue;
      pages.push({ offer_id: m[1], raw: await exec(t.id, pageExtract) });
    } catch (e) { /* 页面还没加载完之类，跳过 */ }
  }
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { pages } });
}

let running = false;
async function loop() {
  if (running) return;
  running = true;
  try {
    for (;;) {
      let r;
      try { r = await (await fetch(`${SERVER}/api/ext/task?v=${VERSION}`)).json(); } catch (e) { break; } // 工作台没开
      if (!r || !r.task) break;
      const task = r.task, delay = Math.max(Number(r.delay) || 3, 1);
      try {
        if (task.kind === 'offer') {
          await doOffer(task);
          await sleep(rand(delay, delay * 2) * 1000); // 像人一样，看完一个再看下一个
        } else if (task.kind === 'shop') await doShop(task, delay);
        else if (task.kind === 'tabs') await doTabs(task);
      } catch (e) {
        await post('/api/ext/result', { task_id: task.id, ok: false, data: { error: String((e && e.message) || e) } }).catch(() => {});
      }
    }
  } finally {
    running = false;
  }
}

// ---------------- 事件 ----------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'kick') { loop(); sendResponse({ ok: true }); return; }
  if (msg.type === 'collect-page' && sender.tab) {
    (async () => {
      const m = sender.tab.url.match(/offer\/(\d{8,15})/);
      if (!m) return { ok: false, message: '不是商品详情页' };
      const raw = await exec(sender.tab.id, pageExtract);
      const r = await post('/api/ext/collect', { offer_id: m[1], raw });
      return { ok: true, message: r.message || '已发送' };
    })().then(sendResponse, e => sendResponse({ ok: false, message: '发送失败，请确认印标工作台已启动（' + e.message + '）' }));
    return true;
  }
});

chrome.alarms.create('poll', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => loop());
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
