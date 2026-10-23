// AI 套图工作台 采集助手：向本机工作台领取采集任务，在一个最小化的窗口里打开 1688 / 拼多多 / Ozon 页面读取数据。
// 用的是你自己的 Chrome 和登录状态；遇到登录/人机验证时把窗口弹出来让你处理，处理完自动缩回去继续。

const SERVER = 'http://127.0.0.1:8765';
const VERSION = chrome.runtime.getManifest().version;
const OFFER_RE = /(?:offer\/|offerId=|offer_id=)(\d{8,15})/g;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand = (a, b) => a + Math.random() * (b - a);

// 过程日志写进工作台的运行日志（data/logs），出问题时一起导出
function elog(msg, data = {}) {
  fetch(SERVER + '/api/ext/log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ msg: String(msg).slice(0, 500), data }) }).catch(() => {});
}

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

async function closeWorker() {
  const { win } = await chrome.storage.session.get(['win']);
  if (win) elog('任务都做完了，关闭采集窗口');
  await chrome.storage.session.remove(['win', 'tab']);
  if (win) await chrome.windows.remove(win).catch(() => {});
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
  const t0 = Date.now();
  const loaded = waitLoaded(tabId);
  await chrome.tabs.update(tabId, { url });
  const ok = await loaded;
  elog(ok ? '打开页面' : '页面 45 秒没加载完，继续尝试读取', { url: url.slice(0, 300), ms: Date.now() - t0 });
  await sleep(rand(1200, 2200));
}

async function exec(tabId, func, args = [], ms = 150000) {
  // 页面里的脚本最多等 ms：页面卡住时报错交回，不会让采集助手一直卡在一个任务上、后面的任务都排不到
  let timer;
  const run = chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func, args });
  const limit = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('页面没有响应（超过 ' + Math.round(ms / 1000) + ' 秒）')), ms); });
  try {
    const [res] = await Promise.race([run, limit]);
    return res ? res.result : undefined;
  } finally { clearTimeout(timer); }
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
  // 完整规格：普通规格区（webAspects）只列出和当前规格相邻的一部分，Ozon 会把一些 SKU 藏起来；
  // 「所有颜色」弹窗（/modal/aspectsNew → webAspectsModal）能列出更多，但结果也受入口 SKU 影响。
  // 所以：对每个新发现的 SKU 都探测弹窗并取并集（最多 40 次、每批 4 个），再逐个读取每个 SKU 自己的页面（每批 6 个、最多 400 个），
  // 读到的页面里又发现新 SKU 就继续，直到集合不再增加。
  const KEEP = ['webProductHeading', 'webGallery', 'webPrice', 'webAspects', 'webCharacteristics', 'webDescription'];
  const LEAF = ['webGallery', 'webPrice', 'webAspects'];  // 其他规格页只要图、价格、规格（说明和特征只取入口页，数据量小很多）
  const trim = (j, keep = KEEP) => {
    const ws = {};
    for (const [k, v] of Object.entries((j && j.widgetStates) || {})) if (keep.includes(k.split('-')[0])) ws[k] = v;
    return { widgetStates: ws, layoutTrackingInfo: j && j.layoutTrackingInfo, seo: j && j.seo ? { title: j.seo.title } : null };
  };
  const get = async path => {
    const r = await fetch('/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent(path), { credentials: 'include' });
    if (!r.ok) throw new Error('Ozon 接口返回 ' + r.status);
    return r.json();
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const known = new Map();          // sku → 页面链接
  const modalLinks = new Set();
  const add = (sku, link) => {
    sku = String(sku || '');
    if (!/^\d{5,}$/.test(sku)) return;
    if (!known.has(sku) || (!known.get(sku) && link)) known.set(sku, link ? String(link).split('?')[0] : '');
  };
  const walk = (o, depth = 0) => {      // 在任意 JSON 结构里找 {sku, link}
    if (!o || depth > 12) return;
    if (typeof o === 'string') {
      if (/modal\/aspects/i.test(o) && o.length < 600) modalLinks.add(o.startsWith('/') || o.startsWith('http') ? o : '/' + o);
      for (const m of o.matchAll(/\/product\/[\w%-]*?-?(\d{6,})\/?/g)) add(m[1], m[0]);
      if (o.length > 2 && (o[0] === '{' || o[0] === '[')) { try { walk(JSON.parse(o), depth + 1); } catch (e) { /* 不是 JSON */ } }
      return;
    }
    if (Array.isArray(o)) { for (const x of o) walk(x, depth + 1); return; }
    if (typeof o === 'object') {
      if (o.sku && (o.link || o.data)) add(o.sku, o.link);
      for (const v of Object.values(o)) walk(v, depth + 1);
    }
  };
  const aspectsOf = j => {
    for (const [k, v] of Object.entries((j && j.widgetStates) || {})) {
      if (/^webAspects/.test(k)) walk(v);
      else if (typeof v === 'string' && /modal\/aspects/i.test(v)) {   // 「所有颜色」入口可能在别的组件里
        for (const m of v.matchAll(/[\w/]*modal\/aspects\w*\?[^"\\\s]*/gi)) modalLinks.add(m[0].startsWith('/') ? m[0] : '/' + m[0]);
      }
    }
  };
  // 页面上实际显示的规格（和人看到的一样）：规格区里的链接；再点开「所有颜色 / Ещё」读弹窗里的链接
  const domScan = async () => {
    const grab = root => {
      for (const a of root.querySelectorAll('a[href*="/product/"]')) {
        const m = (a.getAttribute('href') || '').match(/\/product\/[\w%-]*?-?(\d{6,})\/?/);
        if (m) add(m[1], m[0]);
      }
    };
    const zones = () => document.querySelectorAll('[data-widget^="webAspects"], [data-widget*="spects"]');
    for (const z of zones()) grab(z);
    let clicked = 0;
    for (const z of zones()) {
      for (const el of z.querySelectorAll('button, [role="button"], span, div')) {
        const t = (el.textContent || '').trim();
        if (t.length > 40 || !/^(все|ещё|еще|показать)/i.test(t)) continue;
        try { el.click(); clicked++; } catch (e) { /* 忽略 */ }
        await sleep(1500);
        for (const d of document.querySelectorAll('[data-widget*="AspectsModal"], [data-widget*="aspectsModal"], [role="dialog"], [class*="modal"]')) grab(d);
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await sleep(300);
        if (clicked >= 3) break;
      }
      if (clicked >= 3) break;
    }
    diag.dom_clicked = clicked;
  };
  const entry = (location.pathname.match(/(\d{5,})\/?$/) || location.pathname.match(/-(\d{5,})/) || [])[1] || '';
  const first = await get(location.pathname);
  const main = trim(first);
  if (first.nextPage) { // 说明、特征常在第二页
    try { const more = await get(first.nextPage); Object.assign(main.widgetStates, trim(more).widgetStates); } catch (e) { /* 忽略 */ }
  }
  const entrySku = String((JSON.parse(first.layoutTrackingInfo || '{}').sku) || entry);
  add(entrySku, location.pathname);
  aspectsOf(first);
  const pages = new Map([[entrySku, main]]);
  const probed = new Set();
  const diag = { modal: 0, modal_ok: 0, failed: 0, dom_clicked: 0 };
  try { await domScan(); } catch (e) { diag.dom_error = String(e).slice(0, 120); }
  diag.after_dom = known.size;
  const probe = async sku => {
    probed.add(sku);
    diag.modal++;
    const own = [...modalLinks].filter(l => l.includes(sku));
    const other = [...modalLinks].filter(l => !l.includes(sku)).map(l => l.replace(/(product_id|sku|id)=\d+/i, `$1=${sku}`));
    const tries = [...new Set([...own, ...other, `/modal/aspectsNew?product_id=${sku}`, `/modal/aspectsNew?sku=${sku}`])].slice(0, 4);
    for (const path of tries) {
      try {
        const j = await get(path.startsWith('http') ? new URL(path).pathname + new URL(path).search : path);
        const before = known.size;
        for (const [k, v] of Object.entries(j.widgetStates || {})) if (/aspects/i.test(k)) walk(v);
        diag.modal_ok++;
        if (known.size >= before) return;
      } catch (e) { /* 换一种写法 */ }
    }
  };
  for (let round = 0; round < 60; round++) {
    const toProbe = [...known.keys()].filter(k => !probed.has(k)).slice(0, 4);
    if (toProbe.length && diag.modal < 40) { await Promise.allSettled(toProbe.map(probe)); continue; }
    const toFetch = [...known.keys()].filter(k => !pages.has(k)).slice(0, 6);
    if (!toFetch.length || pages.size >= 400) break;
    await Promise.allSettled(toFetch.map(async sku => {
      try {
        const j = await get(known.get(sku) || `/product/${sku}/`);
        pages.set(sku, trim(j, LEAF));
        aspectsOf(j);
      } catch (e) { pages.set(sku, null); diag.failed++; }
    }));
    await sleep(300 + Math.random() * 400);
  }
  diag.discovered = known.size;
  diag.modal_links = [...modalLinks].slice(0, 5);
  diag.skus = [...known.keys()].slice(0, 80);
  diag.fetched = [...pages.values()].filter(Boolean).length;
  return { pages: [...pages.values()].filter(Boolean), url: location.href, diag };
}

// WB：读 WB 公开的 card.json（图片服务器上）和价格接口；同一商品的其他颜色一起读（最多 20 个）。
// WB 的图片服务器编号（basket-NN）和价格接口会变：优先用页面自己刚请求过的地址，其次并行探测，所有请求都有超时，不会卡死。
async function pageWb() {
  const T0 = Date.now(), BUDGET = 75000;
  const nm = Number((location.pathname.match(/catalog\/(\d+)/) || [])[1]);
  const vp = n => ({ vol: Math.floor(n / 1e5), part: Math.floor(n / 1e3) });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const tfetch = async (url, ms = 6000) => {
    const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
    try { return await fetch(url, { signal: c.signal }); } finally { clearTimeout(t); }
  };
  const diag = { tried: 0, from_page: false, detail: '' };
  const hosts = {};           // vol → host
  let detailUrl = '';         // 页面自己用的价格接口
  const scan = () => {
    for (const e of performance.getEntriesByType('resource')) {
      const m = e.name.match(/https:\/\/([\w-]+\.wbbasket\.ru|[\w-]+\.wbcontent\.net|[\w-]+\.wb\.ru)\/vol(\d+)\//);
      if (m) { hosts[m[2]] = m[1]; diag.from_page = true; }
      if (/\/cards\/v\d+\/(list|detail)\?/.test(e.name) && /nm=/.test(e.name)) detailUrl = e.name;
    }
  };
  for (let i = 0; i < 16 && !hosts[vp(nm).vol]; i++) { scan(); if (!hosts[vp(nm).vol]) await sleep(500); } // 页面还在加载：等一会儿
  const tryHost = async (h, n) => {
    const { vol, part } = vp(n);
    diag.tried++;
    try {
      const r = await tfetch(`https://${h}/vol${vol}/part${part}/${n}/info/ru/card.json`, 5000);
      if (r.ok) { const card = await r.json(); return { nm: n, host: h, vol, part, card }; }
    } catch (e) { /* 换下一个服务器 */ }
    return null;
  };
  const get = async n => {
    const { vol } = vp(n);
    if (hosts[vol]) { const r = await tryHost(hosts[vol], n); if (r) return r; }
    const near = Object.entries(hosts).sort((a, b) => Math.abs(a[0] - vol) - Math.abs(b[0] - vol)).map(x => +x[1].match(/\d+/)[0]);
    const order = [...new Set([...near.flatMap(k => [k, k + 1, k - 1, k + 2, k - 2]), ...Array.from({ length: 80 }, (_, i) => i + 1)])]
      .filter(k => k >= 1 && k <= 99);
    for (let i = 0; i < order.length && Date.now() - T0 < BUDGET; i += 10) { // 每批并行 10 个
      const res = await Promise.all(order.slice(i, i + 10).map(k => tryHost(`basket-${String(k).padStart(2, '0')}.wbbasket.ru`, n)));
      const ok = res.find(Boolean);
      if (ok) { hosts[vol] = ok.host; return ok; }
    }
    return null;
  };
  const first = await get(nm);
  if (!first) return { cards: [], error: `没找到这个商品的 card.json（探测了 ${diag.tried} 次）`, diag };
  const ids = [nm];
  for (const c of first.card.full_colors || []) if (c && c.nm_id && !ids.includes(c.nm_id)) ids.push(c.nm_id);
  for (const c of first.card.colors || []) if (typeof c === 'number' && !ids.includes(c)) ids.push(c);
  const cards = [first];
  const rest = await Promise.all(ids.slice(1, 20).map(n => get(n)));
  for (const c of rest) if (c) cards.push(c);
  // 价格：先用页面自己请求过的价格接口（换上我们要的 nm），再试几个已知版本
  scan();
  const nmList = ids.slice(0, 20).join(';');
  const urls = [];
  if (detailUrl) urls.push(detailUrl.replace(/([?&]nm=)[^&]*/, '$1' + nmList));
  for (const host of ['card.wb.ru', 'u-card.wb.ru']) for (const v of ['v4', 'v2']) {
    urls.push(`https://${host}/cards/${v}/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm=${nmList}`);
  }
  let detail = null;
  for (const u of urls) {
    if (Date.now() - T0 > BUDGET + 15000) break;
    try {
      const r = await tfetch(u, 6000);
      if (!r.ok) continue;
      const j = await r.json();
      if ((j.products || (j.data || {}).products || []).length) { detail = j; diag.detail = u.split('?')[0]; break; }
    } catch (e) { /* 换下一个 */ }
  }
  // 价格接口都不通：从页面上读当前商品的价格（只有这一个颜色的价，够做市场参考）
  let domPrice = null;
  if (!detail) {
    const el = document.querySelector('[class*="price-block__final-price"], ins[class*="final-price"], [class*="priceBlockFinalPrice"], meta[itemprop="price"]');
    const txt = el ? (el.getAttribute('content') || el.textContent || '') : '';
    domPrice = Number(txt.replace(/[^\d,.]/g, '').replace(',', '.')) || null;
  }
  return { cards, detail, dom_price: domPrice, diag };
}

// 以图搜 1688 同款：从搜索结果里提取候选（商品链接周围的图、标题、价格）。自包含，页面里和解析 HTML 文本时都能用
function pageCandidates(html) {
  const doc = html ? new DOMParser().parseFromString(html, 'text/html') : document;
  const out = [], seen = new Set();
  const ID = /(?:offer\/|offerId=|offer_id=|offerid["':=\s]+|data-offer-?id=["']?)(\d{8,15})/i;
  const imgOf = box => {
    const img = box.querySelector('img');
    let src = img ? (img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy-src') ||
      img.getAttribute('data-lazy') || img.getAttribute('data-url') || (img.getAttribute('srcset') || '').split(/[\s,]/)[0] || img.getAttribute('src') || '') : '';
    if (!src || src.startsWith('data:')) {
      const bg = box.querySelector('[style*="background"]');
      const m = bg && (bg.getAttribute('style') || '').match(/url\(["']?([^"')]+)/);
      src = m ? m[1] : (src.startsWith('data:') ? '' : src);
    }
    try { return src ? new URL(src, location.href).href : ''; } catch (e) { return src; }
  };
  const push = (id, box, a) => {
    if (seen.has(id) || out.length >= 20) return;
    seen.add(id);
    const text = (box.innerText || box.textContent || '').replace(/\s+/g, ' ').trim();
    const price = (text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/) || [])[1];
    const titleEl = box.querySelector('[class*=title], [title]');
    out.push({ offer_id: id, image: imgOf(box), price: price ? Number(price) : null,
               title: ((titleEl && (titleEl.getAttribute('title') || titleEl.innerText || titleEl.textContent)) || (a && a.getAttribute('title')) || text).trim().slice(0, 80) });
  };
  for (const a of doc.querySelectorAll('a[href]')) {
    const m = (a.getAttribute('href') || '').match(ID);
    if (!m) continue;
    let box = a;
    for (let i = 0; i < 6 && box.parentElement; i++) {
      if (box.querySelector('img') && (box.innerText || box.textContent || '').length > 8) break;
      box = box.parentElement;
    }
    push(m[1], box, a);
  }
  if (!out.length) {  // 链接不是 1688 地址（站内跳转、点击事件）：在每个结果卡片的属性里找 1688 商品编号
    for (const box of doc.querySelectorAll('.sbi-item, [class*="item"], li')) {
      const m = (box.outerHTML || '').slice(0, 5000).match(ID);
      if (m && box.querySelector('img')) push(m[1], box, null);
    }
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
  if (u.includes('wildberries.') && /Почти готово|Проверяем браузер|Подождите/i.test(document.title)) return 'verify';
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
  elog('页面需要' + (b === 'login' ? '登录' : '人机验证') + '，已弹出窗口等待处理', { url: (await chrome.tabs.get(tabId)).url.slice(0, 300) });
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
  const raw = await exec(tab, { pdd: pagePdd, ozon: pageOzon, wb: pageWb }[task.platform], [], 240000);
  if (raw && raw.error && !((raw.cards || raw.pages || []).length)) throw new Error(raw.error);
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { raw } });
}

async function doImgSearch(task) {
  // 先用 AiPrice，没结果再用 1688 自己的以图搜。已经在 AiPrice 页面上就不重新打开（快很多）
  const { win, tab } = await workerTab();
  let candidates = [], source = '', note = '', html = '';
  try {
    const cur = (await chrome.tabs.get(tab)).url || '';
    if (!cur.includes('aiprice.com')) {
      await navigate(tab, 'https://www.aiprice.com/img/search');
      await waitUnblocked(win, tab);
    }
    const r = await exec(tab, pageAiprice, [task.image], 60000);
    if (r && r.html) { html = r.html; candidates = await exec(tab, pageCandidates, [r.html]); }
    else note = (r && r.error) || '';
    source = 'aiprice';
  } catch (e) { note = 'AiPrice：' + e.message; }
  if (!candidates || !candidates.length) {
    if (html) debugSave('aiprice-' + (task.unit || ''), html);   // 有页面但一个候选都没解析出来：存下来给开发者看格式
    try {
      await navigate(tab, 'https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress=' + encodeURIComponent(task.image));
      await waitUnblocked(win, tab);
      await exec(tab, pageScrollAndCollect, [], 30000).catch(() => {});
      candidates = await exec(tab, pageCandidates, [null], 30000);
      source = '1688';
      if (!candidates.length) debugSave('1688search-' + (task.unit || ''), await exec(tab, () => document.documentElement.outerHTML.slice(0, 400000)).catch(() => ''));
    } catch (e) { note = (note ? note + '；' : '') + '1688 以图搜：' + e.message; }
  }
  elog('以图搜结果', { unit: task.unit, source, count: (candidates || []).length, note, image: task.image });
  await post('/api/ext/result', { task_id: task.id, ok: true, data: { candidates: candidates || [], source, note } });
}

function debugSave(name, html) {
  fetch(SERVER + '/api/ext/debug', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, html: String(html || '').slice(0, 400000) }) }).catch(() => {});
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
    let idle = 0;
    for (;;) {
      let r;
      try { r = await (await fetch(`${SERVER}/api/ext/task?v=${VERSION}`)).json(); } catch (e) { break; } // 工作台没开
      if (!r || !r.task) {
        // 工作台还在处理刚交回的结果（比如找同款时核对完会马上派下一个）：在同一个窗口里等，不关窗口
        if (r && r.busy && idle < 150) { idle++; await sleep(2000); continue; }
        await closeWorker(); // 没有任务了：关掉采集窗口，下次有任务再开一个
        break;
      }
      idle = 0;
      const task = r.task, delay = Math.max(Number(r.delay) || 3, 1);
      // 处理任务期间每 15 秒报到一次：工作台知道采集助手还在忙，不会误报「没连接」
      const beat = setInterval(() => post('/api/ext/status', { status: '', message: '' }).catch(() => {}), 15000);
      try {
        if (task.kind === 'offer') {
          await doOffer(task);
          await sleep(rand(delay, delay * 2) * 1000); // 像人一样，看完一个再看下一个
        } else if (task.kind === 'page') {
          await doPage(task);
          await sleep(rand(delay, delay * 2) * 1000);
        } else if (task.kind === 'imgsearch') {
          await doImgSearch(task);
          await sleep(rand(400, 1000)); // AiPrice 是接口请求，不用像翻 1688 页面那样停很久
        } else if (task.kind === 'shop') await doShop(task, delay);
        else if (task.kind === 'tabs') await doTabs(task);
        else throw new Error('采集助手不认识这个任务（' + task.kind + '），请更新采集助手');
      } catch (e) {
        elog('任务出错：' + String((e && e.message) || e), { kind: task.kind, id: task.id, url: task.url || task.image || '' });
        await post('/api/ext/result', { task_id: task.id, ok: false, data: { error: String((e && e.message) || e) } }).catch(() => {});
      }
      clearInterval(beat);
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
