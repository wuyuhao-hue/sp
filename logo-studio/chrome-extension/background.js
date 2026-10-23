// AI 套图工作台 采集助手：向本机工作台领取采集任务，在一个最小化的窗口里打开 1688 / 拼多多 / Ozon 页面读取数据。
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
  // 详情图：页面里所有图片（包括 Shadow DOM 里的），记下位置和大小，由服务端挑选
  const descSel = '#detailContentContainer img, .content-detail img, .detail-desc-module img, .html-description img, ' +
    '.offer-description img, .desc-lazyload-container img, [class*="description"] img, [class*="detail-content"] img';
  const imgSrc = i => i.getAttribute('data-lazyload-src') || i.getAttribute('data-lazy-src') || i.getAttribute('data-src') ||
    i.currentSrc || i.getAttribute('src') || '';
  const details = [], allImgs = [], roots = [document];
  for (let k = 0; k < roots.length && k < 500; k++) {
    const root = roots[k];
    root.querySelectorAll(descSel).forEach(i => details.push(imgSrc(i)));
    root.querySelectorAll('img').forEach(i => {
      const r = i.getBoundingClientRect();
      allImgs.push({ src: imgSrc(i), w: Math.round(r.width), nw: i.naturalWidth || 0, top: Math.round(r.top + window.scrollY),
                     shadow: root !== document });
    });
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) roots.push(el.shadowRoot); });
  }
  const titleEl = document.querySelector('.title-text, .d-title, .od-pc-offer-title-contain h1, h1');
  return {
    globals, gallery, details: details.filter(x => x && x.includes('alicdn')), allImgs: allImgs.filter(x => x.src.includes('alicdn')),
    h1: titleEl ? titleEl.innerText.trim() : '',
    title: document.title,
    // 数据对象都没找到时才带上整页 HTML，供服务端兜底解析
    html: Object.keys(globals).length ? '' : document.documentElement.outerHTML.slice(0, 4000000),
  };
}

// 拼多多：读页面里的商品数据对象（rawData），没有就在页面数据里有限地找一个带 goodsName + skus 的对象
function pagePdd() {
  const seen = new WeakSet();
  const safe = v => JSON.parse(JSON.stringify(v, (k, val) => {
    if (typeof val === 'function') return undefined;
    if (val && typeof val === 'object') { if (seen.has(val)) return undefined; seen.add(val); }
    return val;
  }));
  let goods = null;
  try { goods = window.rawData && window.rawData.store && window.rawData.store.initDataObj && window.rawData.store.initDataObj.goods; } catch (e) { /* 忽略 */ }
  if (!goods) {
    const queue = [[window.rawData || {}, 0], [window.__NEXT_DATA__ || {}, 0], [window.__INITIAL_STATE__ || {}, 0]];
    const visited = new WeakSet();
    let n = 0;
    while (queue.length && n < 80000 && !goods) {
      const [o, d] = queue.shift();
      if (!o || typeof o !== 'object' || visited.has(o) || d > 18) continue;
      visited.add(o); n++;
      try {
        if (o.goodsName && Array.isArray(o.skus)) { goods = o; break; }
        for (const k of Object.keys(o)) { const v = o[k]; if (v && typeof v === 'object') queue.push([v, d + 1]); }
      } catch (e) { /* 忽略 */ }
    }
  }
  return { goods: goods ? safe(goods) : null, title: document.title };
}

// Ozon：在页面里调用 Ozon 自己的页面数据接口（entrypoint），再逐个读取颜色/尺码变体（最多 30 个）
async function pageOzon() {
  const KEEP = ['webProductHeading', 'webGallery', 'webPrice', 'webAspects', 'webCharacteristics', 'webDescription'];
  const trim = j => {
    const ws = {};
    for (const [k, v] of Object.entries((j && j.widgetStates) || {})) if (KEEP.includes(k.split('-')[0])) ws[k] = v;
    return { widgetStates: ws, layoutTrackingInfo: j && j.layoutTrackingInfo, seo: j && j.seo ? { title: j.seo.title } : null };
  };
  const get = async path => {
    const r = await fetch('/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent(path), { credentials: 'include' });
    if (!r.ok) throw new Error('Ozon 接口返回 ' + r.status);
    return r.json();
  };
  const path = location.pathname;
  const first = await get(path);
  const main = trim(first);
  if (first.nextPage) { // 说明、特征常在第二页
    try { const more = await get(first.nextPage); Object.assign(main.widgetStates, trim(more).widgetStates); } catch (e) { /* 忽略 */ }
  }
  const pages = [main];
  const links = [];
  for (const [k, v] of Object.entries(main.widgetStates)) {
    if (!k.startsWith('webAspects')) continue;
    try {
      for (const a of JSON.parse(v).aspects || []) for (const x of a.variants || []) {
        if (x.link && !x.active && !links.includes(x.link)) links.push(x.link);
      }
    } catch (e) { /* 忽略 */ }
  }
  for (const link of links.slice(0, 30)) {
    await new Promise(r => setTimeout(r, 400 + Math.random() * 500));
    try { pages.push(trim(await get(link.split('?')[0]))); } catch (e) { /* 单个变体失败不影响其他 */ }
  }
  return { pages, url: location.href };
}

// WB：在商品页里读 WB 公开的 card.json（图片服务器上）和价格接口；同一商品的其他颜色一起读（最多 20 个）
async function pageWb() {
  const nm = Number((location.pathname.match(/catalog\/(\d+)/) || [])[1]);
  const vp = n => ({ vol: Math.floor(n / 1e5), part: Math.floor(n / 1e3) });
  const hosts = {};
  for (const e of performance.getEntriesByType('resource')) {
    const m = e.name.match(/https:\/\/(basket-\d+\.wbbasket\.ru)\/vol(\d+)\//);
    if (m) hosts[m[2]] = m[1];
  }
  const tryHost = async (h, n) => {
    const { vol, part } = vp(n);
    try {
      const r = await fetch(`https://${h}/vol${vol}/part${part}/${n}/info/ru/card.json`);
      if (r.ok) return { nm: n, host: h, vol, part, card: await r.json() };
    } catch (e) { /* 换下一个服务器 */ }
    return null;
  };
  const get = async n => {
    const { vol } = vp(n);
    if (hosts[vol]) { const r = await tryHost(hosts[vol], n); if (r) return r; }
    for (let i = 1; i <= 40; i++) {
      const h = `basket-${String(i).padStart(2, '0')}.wbbasket.ru`;
      const r = await tryHost(h, n);
      if (r) { hosts[vol] = h; return r; }
    }
    return null;
  };
  const first = await get(nm);
  if (!first) return { cards: [], error: '没找到这个商品的 card.json' };
  const ids = [nm];
  for (const c of first.card.full_colors || []) if (c && c.nm_id && !ids.includes(c.nm_id)) ids.push(c.nm_id);
  for (const c of first.card.colors || []) if (typeof c === 'number' && !ids.includes(c)) ids.push(c);
  const cards = [first];
  for (const n of ids.slice(1, 20)) { const c = await get(n); if (c) cards.push(c); }
  let detail = null;
  for (const v of ['v4', 'v2', 'v1']) {
    try {
      const r = await fetch(`https://card.wb.ru/cards/${v}/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm=${ids.slice(0, 20).join(';')}`);
      if (!r.ok) continue;
      const j = await r.json();
      if ((j.products || (j.data || {}).products || []).length) { detail = j; break; }
    } catch (e) { /* 换下一个版本 */ }
  }
  return { cards, detail };
}

// 以图搜 1688 同款：从搜索结果里提取候选（商品链接周围的图、标题、价格）。自包含，页面里和解析 HTML 文本时都能用
function pageCandidates(html) {
  const doc = html ? new DOMParser().parseFromString(html, 'text/html') : document;
  const out = [], seen = new Set();
  for (const a of doc.querySelectorAll('a[href]')) {
    const m = (a.getAttribute('href') || '').match(/(?:offer\/|offerId=|offer_id=)(\d{8,15})/);
    if (!m || seen.has(m[1])) continue;
    let box = a;
    for (let i = 0; i < 6 && box.parentElement; i++) {
      if (box.querySelector('img') && (box.innerText || box.textContent || '').length > 8) break;
      box = box.parentElement;
    }
    const img = box.querySelector('img');
    const src = img ? (img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('src') || '') : '';
    const text = (box.innerText || box.textContent || '').replace(/\s+/g, ' ').trim();
    const price = (text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/) || [])[1];
    const titleEl = box.querySelector('[class*=title], [title]');
    seen.add(m[1]);
    let abs = src;
    try { abs = src ? new URL(src, location.href).href : ''; } catch (e) { /* 保留原样 */ }
    out.push({ offer_id: m[1], image: abs, price: price ? Number(price) : null,
               title: ((titleEl && (titleEl.getAttribute('title') || titleEl.innerText)) || a.getAttribute('title') || text).trim().slice(0, 80) });
    if (out.length >= 20) break;
  }
  return out;
}

async function pageAiprice(img) {
  // AiPrice 站内接口（原项目实测可用）：返回 HTML，候选在 .sbi-items-list .sbi-item
  const r = await fetch('/img/search_detail?image=' + encodeURIComponent(img), { credentials: 'include' });
  if (!r.ok) return { candidates: [], error: 'AiPrice 返回 ' + r.status };
  return { html: (await r.text()).slice(0, 3000000) };
}

function platformOf(url) {
  let m = url.match(/detail\.1688\.com\/offer\/(\d{8,15})/);
  if (m) return { platform: '1688', id: m[1] };
  m = url.match(/(?:yangkeduo|pinduoduo)\.com\/.*goods_id=(\d{6,15})/);
  if (m) return { platform: 'pdd', id: m[1] };
  m = url.match(/ozon\.\w+\/product\/(?:[\w%-]+-)?(\d{5,12})/);
  if (m) return { platform: 'ozon', id: m[1] };
  m = url.match(/wildberries\.\w+\/catalog\/(\d{5,12})\//);
  if (m) return { platform: 'wb', id: m[1] };
  return null;
}

function pageBlocked() {
  const u = location.href;
  if (u.includes('yangkeduo.com/login') || u.includes('pinduoduo.com/login')) return 'login';
  if (u.includes('ozon.') && (document.title.includes('Доступ ограничен') || document.querySelector('#challenge, .challenge-form'))) return 'verify';
  if (u.includes('login.') || u.includes('/member/signin')) return 'login';
  if (u.includes('punish') || u.includes('_____tmd_____')) return 'verify';
  for (const sel of ['#nc_1_n1z', '#baxia-dialog-content', '.baxia-dialog', "iframe[src*='punish']"]) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) return 'verify';
  }
  return '';
}

async function pageScrollAndCollect() {
  // 慢慢滚到页面底部，让懒加载的详情图出来；最多约 12 秒
  const y0 = window.scrollY, t0 = Date.now();
  while (Date.now() - t0 < 12000) {
    const before = window.scrollY;
    window.scrollBy(0, 500 + Math.random() * 400);
    await new Promise(r => setTimeout(r, 350 + Math.random() * 300));
    if (window.scrollY === before && window.innerHeight + window.scrollY >= document.body.scrollHeight - 5) break;
  }
  const html = document.documentElement.outerHTML;
  window.scrollTo(0, y0); // 读完滚回原来的位置
  return html;
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
    status: b, message: b === 'login' ? '请在弹出的 Chrome 窗口中登录（1688 / 拼多多）' : '请在弹出的 Chrome 窗口中完成人机验证',
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
  await exec(tab, pageScrollAndCollect).catch(() => {}); // 滚到底，让详情图加载出来
  const raw = await exec(tab, pageExtract);
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { raw } });
}

async function doPage(task) {
  // 拼多多 / Ozon 商品页
  const { win, tab } = await workerTab();
  await navigate(tab, task.url);
  await waitUnblocked(win, tab);
  if (task.platform === 'pdd') await exec(tab, pageScrollAndCollect).catch(() => {});
  const raw = await exec(tab, { pdd: pagePdd, ozon: pageOzon, wb: pageWb }[task.platform]);
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { raw } });
}

async function doImgSearch(task) {
  // 先用 AiPrice，没结果再用 1688 自己的以图搜
  const { win, tab } = await workerTab();
  let candidates = [], source = '', note = '';
  try {
    await navigate(tab, 'https://www.aiprice.com/img/search');
    await waitUnblocked(win, tab);
    const r = await exec(tab, pageAiprice, [task.image]);
    if (r && r.html) candidates = await exec(tab, pageCandidates, [r.html]);
    else note = (r && r.error) || '';
    source = 'aiprice';
  } catch (e) { note = 'AiPrice：' + e.message; }
  if (!candidates || !candidates.length) {
    try {
      await navigate(tab, 'https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress=' + encodeURIComponent(task.image));
      await waitUnblocked(win, tab);
      await exec(tab, pageScrollAndCollect).catch(() => {});
      candidates = await exec(tab, pageCandidates, [null]);
      source = '1688';
    } catch (e) { note = (note ? note + '；' : '') + '1688 以图搜：' + e.message; }
  }
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { candidates: candidates || [], source, note } });
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
  const tabs = await chrome.tabs.query({ url: ['*://detail.1688.com/offer/*', '*://mobile.yangkeduo.com/goods*', '*://*.ozon.ru/product/*',
                                               '*://*.wildberries.ru/catalog/*'] });
  const pages = [], others = [];
  for (const t of tabs) {
    const pf = platformOf(t.url);
    if (!pf) continue;
    try {
      if (await exec(t.id, pageBlocked)) continue;
      if (pf.platform === '1688') pages.push({ offer_id: pf.id, raw: await exec(t.id, pageExtract) });
      else others.push({ platform: pf.platform, id: pf.id, raw: await exec(t.id, { pdd: pagePdd, ozon: pageOzon, wb: pageWb }[pf.platform]) });
    } catch (e) { /* 页面还没加载完之类，跳过 */ }
  }
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { pages, others } });
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
        } else if (task.kind === 'page') {
          await doPage(task);
          await sleep(rand(delay, delay * 2) * 1000);
        } else if (task.kind === 'imgsearch') {
          await doImgSearch(task);
          await sleep(rand(delay, delay * 2) * 1000);
        } else if (task.kind === 'shop') await doShop(task, delay);
        else if (task.kind === 'tabs') await doTabs(task);
        else throw new Error('采集助手不认识这个任务（' + task.kind + '），请更新采集助手');
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
      const pf = platformOf(sender.tab.url);
      if (!pf) return { ok: false, message: '不是商品详情页' };
      if (pf.platform === '1688' || pf.platform === 'pdd') await exec(sender.tab.id, pageScrollAndCollect).catch(() => {});
      const fn = { '1688': pageExtract, pdd: pagePdd, ozon: pageOzon, wb: pageWb }[pf.platform];
      const raw = await exec(sender.tab.id, fn);
      const r = await post('/api/ext/collect', { offer_id: pf.id, raw, platform: pf.platform });
      return { ok: true, message: r.message || '已发送' };
    })().then(sendResponse, e => sendResponse({ ok: false, message: '发送失败，请确认印标工作台已启动（' + e.message + '）' }));
    return true;
  }
});

chrome.alarms.create('poll', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => loop());
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
