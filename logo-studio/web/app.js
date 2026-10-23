const { createApp } = Vue;

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'), headers: {} };
  if (opts.form) { init.body = opts.form; init.method = 'POST'; }
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `请求失败 (${r.status})`);
  return data;
}

const LANG_NAME = { ru: '俄语', en: '英语', cn: '中文' };

createApp({
  data: () => ({
    tab: 'products',
    products: [], logos: [], current: null, queue: [],
    query: '', selected: [], skuSel: [], skuView: null, genLogo: null, genStyle: null, showRefs: false,
    styles: {}, batch: { logo_id: null, style: 'random', sku_scope: 'all' },
    settings: {}, shotTypes: {}, canLogoTypes: [], defaultShots: [], models: [],
    browserState: { state: 'idle', message: '' }, ext: {}, jobs: [], hiddenJobs: [],
    extInstalled: !!document.documentElement.getAttribute('data-logo-studio-ext'),
    showScrape: false, scrapeForm: { links: '', shops: '', max_pages: 5 },
    generating: false, analyzing: false, readingSize: false, lightbox: null, qSel: [],
    toastMsg: '', toastErr: false, dropOver: false, testMsg: '',
    // WB 上架
    listings: [], shops: [], shopCurrent: '', showShops: false, shopForm: { name: '', token: '' }, shopBusy: false,
    warehouses: {}, shopTest: {}, ed: null, qFilter: 'all', qLogo: '', diag: null, bulk: null, aiCalls: null, showCalls: false, listingTestMsg: '', pStage: 'all', pLogo: '', pSource: '', qShopF: '', qSubject: null, qQuery: '', qSource: '',
    dsel: { skus: [], main: [], detail: false }, matchUrl: '', mFilter: 'all', pick: null, iconBad: {}, showLogin: false, loginBusy: {}, dir: { colors: [], countries: [] },
  }),
  computed: {
    filtered() {
      const q = this.query.trim().toLowerCase();
      return this.products.filter(p => (p.mode || 'logo') === this.workMode
        && (!q || (p.title || '').toLowerCase().includes(q) || (p.offer_id || '').includes(q))
        && (this.pStage === 'all' || p.stage === this.pStage) && this.logoMatch(p) && (!this.pSource || p.source === this.pSource));
    },
    allSelected() { return this.filtered.length && this.filtered.every(p => this.selected.includes(p.id)); },
    skus() {
      const p = this.current;
      if (!p) return [];
      for (const prop of p.sku_props || []) {
        const vals = prop.values.filter(v => v.image);
        if (vals.length) return vals.map(v => ({ name: v.name, image: v.image }));
      }
      return p.main_images && p.main_images.length ? [{ name: '默认', image: p.main_images[0] }] : [];
    },
    setsSorted() {
      const all = (this.current && this.current.sets) || [];
      return all.filter(s => !this.skuView || s.sku === this.skuView).sort((a, b) => b.created - a.created);
    },
    workMode() { return this.settings.work_mode || 'logo'; },
    extOld() {
      const v = (this.ext.version || '').split('.').map(Number), need = [1, 7, 1];
      if (!this.ext.version) return false;
      for (let i = 0; i < 3; i++) if ((v[i] || 0) !== need[i]) return (v[i] || 0) < need[i];
      return false;
    },
    styleOptions() { return { ...this.styles, random: '随机风格' }; },
    aiInfo() { return (this.current && this.current.ai) || {}; },
    guessGender() { const t = (this.current && this.current.title) || ''; return /女/.test(t) ? 'female' : /男/.test(t) ? 'male' : 'unisex'; },
    sizeTableText() {
      const t = this.aiInfo.size_table;
      return t && t.header ? [t.header, ...t.rows].map(r => r.join(' | ')).join('\n') : '';
    },
    shotTypeOptions() { const o = { ...this.shotTypes }; delete o.size_chart; return o; },
    localPointsText() { return (this.aiInfo.points_local || []).map(p => p.ru).join('\n'); },
    lang() { return this.settings.copy_lang || 'ru'; },
    langName() { return LANG_NAME[this.lang]; },
    activeShots() { return (this.settings.shots || []).slice(0, this.settings.shot_count || 6); },
    apiReady() { return !!(this.settings.api_base && this.settings.api_key); },
    shownListings() { return this.listings.filter(l => this.qMatch(l)); },
    otherShops() { return this.shops.filter(s => s.id !== this.shopCurrent); },
    listingSubjects() { return [...new Set(this.listings.filter(l => (l.mode || 'logo') === this.workMode).map(l => l.subject_name || ''))].sort(); },
    // 采集上架：按采集来源（1688 / WB / 拼多多 / Ozon）切换
    productSources() { return this.sourcesOf(this.products.filter(p => (p.mode || 'logo') === this.workMode)); },
    listingSources() { return this.sourcesOf(this.listings.filter(l => (l.mode || 'logo') === this.workMode)); },
    loginItems() {
      const lg = this.ext.logins || {}, skip = this.ext.login_skip || [];
      return [
        { k: '1688', name: '1688', why: '采集 1688 商品、打开 1688 核对同款' },
        { k: 'pdd', name: '拼多多', why: '采集拼多多商品（mobile.yangkeduo.com）' },
        { k: 'aiprice', name: 'AiPrice', why: '以图搜 1688 同款' },
      ].map(it => ({ ...it, skipped: skip.includes(it.k), ok: lg[it.k] === true || skip.includes(it.k) }));
    },
    needLogin() {
      // 采集助手连上并报了登录状态才提示；已登录（自动检测）和手动跳过的不算
      if (!this.ext.connected || !Object.keys(this.ext.logins || {}).length) return [];
      return this.loginItems.filter(it => !it.ok);
    },
    isMarket() { return !!this.current && ['ozon', 'wb'].includes(this.current.source); },
    units() { return (this.current && this.current.units) || []; },
    mCount() {
      const c = { matched: 0, none: 0, error: 0, todo: 0, running: 0 };
      for (const u of this.units) c[['pending', 'searching', 'verifying'].includes(u.status) ? 'running' : u.status]++;
      return c;
    },
    matchRunning() { return this.mCount.running > 0 || !!(this.current && this.current.match_wait); },
    mFilters() {
      const c = this.mCount, f = [{ k: 'all', label: '全部规格', n: this.units.length }, { k: 'matched', label: '同款', n: c.matched },
        { k: 'none', label: '无同款', n: c.none }];
      if (c.running) f.push({ k: 'running', label: '匹配中', n: c.running });
      if (c.todo || c.error) f.push({ k: 'todo', label: '未匹配 / 失败', n: c.todo + c.error });
      return f;
    },
    shownUnits() {
      const f = this.mFilter, run = ['pending', 'searching', 'verifying'];
      return this.units.filter(u => f === 'all' || u.status === f || (f === 'running' && run.includes(u.status)) || (f === 'todo' && ['todo', 'error'].includes(u.status)));
    },
    removedKeys() { return Object.keys((this.current && this.current.match_removed) || {}); },
    offerList() { return Object.values((this.current && this.current.match_offers) || {}).filter(o => (o.colors || []).length); },
    queueSkus() {
      if (!this.isMarket) return this.skus;
      const ok = new Set(this.units.filter(u => u.status === 'matched').map(u => u.key));
      return this.skus.filter(k => ok.has(k.name));
    },
    selectedMarket() { return this.selected.filter(id => ['ozon', 'wb'].includes((this.products.find(p => p.id === id) || {}).source)); },
    listingLogos() {
      const m = {};
      for (const l of this.listings.filter(x => (x.mode || 'logo') === this.workMode)) {
        const g = m[l.key] || (m[l.key] = { key: l.key, name: l.key === 'src' ? '采集上架（原图）' : (l.logo_name || '无 logo'), count: 0, img: '' });
        g.count++;
        const lg = this.logos.find(x => x.id === l.logo_id);
        if (lg) g.img = this.logoUrl(lg);
      }
      return Object.values(m);
    },
    visibleCharcs() {
      if (!this.ed) return [];
      const skip = c => /^(цвет|бренд)$/i.test(c.name.trim()) || /^страна производства/i.test(c.name) || /ТНВЭД/i.test(c.name.replace(/\s/g, '')) || c.charcID === 88952;
      return this.ed.meta.filter(c => !skip(c) && (this.ed.allCharcs || c.required || c.popular || (this.ed.L.charcs[c.charcID] || []).length));
    },
    charcFilled() { return this.visibleCharcs.filter(c => (this.ed.L.charcs[c.charcID] || []).length).length; },
    pricePreview() {
      const L = this.ed && this.ed.L;
      if (!L) return {};
      const pr = L.pricing;
      const calc = cny => pr.mode === 'fixed' && pr.fixed > 0 ? Math.round(pr.fixed) : Math.max(1, Math.round((Number(cny) + Number(pr.add || 0)) * Number(pr.mul || 1) + Number(pr.add2 || 0)));
      const full = Math.max(0, ...L.sizes.filter(s => s.enabled).map(s => calc(s.cny_override || s.cny || 0)));
      return { full, sale: Math.round(full * (100 - Math.min(99, Math.max(0, pr.discount || 0))) / 100) };
    },
    queueCount() { return this.products.filter(p => (p.mode || 'logo') === this.workMode).reduce((n, p) => n + (p.queued || 0), 0); },
    visibleJobs() { return this.jobs.filter(j => !this.hiddenJobs.includes(j.id)).slice(0, 3); },
    extMode() { return (this.settings.scrape_mode || 'extension') === 'extension'; },
    statusTitle() { return this.extMode ? (this.ext.message || '') : this.browserState.message; },
    browserDot() {
      if (this.extMode) return this.ext.status ? 'warn' : this.ext.connected ? 'ok' : (this.extInstalled ? 'warn' : 'err');
      return { ready: 'ok', need_login: 'warn', need_verify: 'warn', starting: 'warn', error: 'err' }[this.browserState.state] || '';
    },
    browserLabel() {
      if (this.extMode) {
        if (this.ext.status === 'login') return '请在 Chrome 中登录 1688';
        if (this.ext.status === 'verify') return '请在 Chrome 中完成验证';
        return this.ext.connected ? `采集助手 ${this.ext.version || ''} 已连接` : (this.extInstalled ? '采集助手连接中…' : '未安装采集助手');
      }
      return { ready: '浏览器已连接', need_login: '等待登录 1688', need_verify: '等待滑块验证', starting: '浏览器启动中', error: '浏览器出错' }[this.browserState.state] || '浏览器未启动';
    },
  },
  watch: {
    'ext.logins'(lg, old) {
      for (const [k, v] of Object.entries(lg || {})) {
        if (v && this.loginBusy[k]) { this.loginBusy[k] = false; this.toast(`${{ '1688': '1688', pdd: '拼多多', aiprice: 'AiPrice' }[k] || k} 已登录`); }
      }
    },
    'needLogin.length'(n, o) { if (o > 0 && n === 0) { this.showLogin = false; this.toast('需要的平台都已登录 ✓'); } },
    tab(t) { if (t === 'queue') { this.loadQueue(); this.loadShops(); } if (t === 'settings') this.loadAiCalls(); },
  },
  async mounted() {
    await Promise.all([this.loadProducts(), this.loadLogos(), this.poll(), this.loadShops().catch(() => {})]);
    if (this.filtered.length) this.openProduct(this.filtered[0].id);
    setInterval(this.poll, 1500);
    window.postMessage({ logoStudio: 'check-logins' }, '*');  // 打开工作台就检测一次各平台登录状态
  },
  methods: {
    toast(msg, err = false) {
      this.toastMsg = msg; this.toastErr = err;
      clearTimeout(this._t); this._t = setTimeout(() => (this.toastMsg = ''), err ? 5000 : 2400);
    },
    imgSrc(u) {
      if (!u) return '';
      if (u.startsWith('local:')) { const [pid, ...rest] = u.slice(6).split('/'); return `/data/products/${pid}/${rest.join('/')}`; }
      return u.includes('alicdn.com') && !/_\d+x\d+/.test(u) ? u + '_400x400.jpg' : u; // 列表里用缩略图，省流量
    },
    zoom(src) { this.lightbox = { src: src.replace(/_400x400\.jpg$/, '') }; },
    fileUrl(pid, f, v) { return f ? `/data/products/${pid}/${f}?v=${v || 0}` : ''; },
    logoUrl(l) { return `/data/logos/${l.id}/${l.logo}?v=${l.v || 0}`; },
    fmtPrice(a, b) { return a === b || b == null ? `${a}` : `${a}-${b}`; },
    shotName(t) { return this.shotTypes[t] || t; },
    styleName(k) { return this.styleOptions[k] || k; },
    canLogo(t) { return this.canLogoTypes.includes(t); },
    setsOf(name) { return ((this.current && this.current.sets) || []).filter(s => s.sku === name); },
    jobStatus(j) { return { running: `${j.done}/${j.total || '?'}`, done: '完成', failed: '失败', pending: '排队中' }[j.status]; },

    async loadProducts() { this.products = await api('/api/products'); },
    async loadLogos() { this.logos = await api('/api/logos'); },
    async loadQueue() {
      this.listings = await api('/api/listings');
      this.qSel = this.qSel.filter(k => this.shownListings.some(l => this.lkey(l) === k));
      if (this.ed) {  // 编辑框开着时只同步上架状态，不覆盖正在编辑的内容
        const l = this.listings.find(x => this.lkey(x) === this.lkey(this.ed.summary));
        if (l && (l.status !== this.ed.L.status || JSON.stringify(l.progress) !== JSON.stringify(this.ed.L.progress))) {
          const r = await api(`/api/listings/${l.pid}/${l.key}`).catch(() => null);
          if (r && this.ed) Object.assign(this.ed.L, { status: r.listing.status, progress: r.listing.progress, error: r.listing.error, log: r.listing.log, nm: r.listing.nm });
        }
      }
    },
    async poll() {
      try {
        const s = await api('/api/state');
        this.browserState = s.browser;
        this.ext = s.ext || {};
        this.extInstalled = this.extInstalled || !!document.documentElement.getAttribute('data-logo-studio-ext');
        this.shotTypes = s.shot_types; this.canLogoTypes = s.can_logo; this.defaultShots = s.default_shots; this.styles = s.styles || {};
        if (!this.settingsLoaded) { this.settings = s.settings; this.settingsLoaded = true; this.genStyle = s.settings.style || 'classic'; }
        const sig = s.jobs.map(j => `${j.id}:${j.done}:${j.status}`).join('|');
        const running = s.jobs.some(j => j.status === 'running');
        this.jobs = s.jobs;
        // 完成的任务几秒后自动收起（导出任务多留一会儿，方便点「打开文件夹」）
        const now = Date.now();
        this._doneAt = this._doneAt || {};
        for (const j of s.jobs) {
          if (j.status === 'running' || j.status === 'pending') continue;
          this._doneAt[j.id] = this._doneAt[j.id] || now;
          const keep = j.kind === 'export' ? 30000 : j.status === 'failed' ? 15000 : 6000;
          if (now - this._doneAt[j.id] > keep && !this.hiddenJobs.includes(j.id)) this.hiddenJobs.push(j.id);
        }
        if (sig !== this._sig || running) {
          this._sig = sig;
          await this.loadProducts();
          if (this.current) await this.refreshCurrent();
          if (this.tab === 'queue') await this.loadQueue();
        } else if (this.tab === 'queue' && this.listings.some(l => ['pending_stock', 'uploading', 'waiting'].includes(l.status))) await this.loadQueue();
      } catch (e) { /* 服务未就绪时忽略 */ }
    },
    async openProduct(id) {
      this.current = await api(`/api/products/${id}`);
      this.skuSel = []; this.skuView = null;
      if (this.current.mode === 'direct') {
        this.dsel = { skus: this.queueSkus.filter(k => !this.directSetOf(k.name)).map(k => k.name),
          main: this.isMarket ? [] : [...(this.current.main_images || [])], detail: false };
        this.matchUrl = ''; this.mFilter = 'all'; this.pick = null;
      }
    },
    async setMode(m) {
      if (m === this.workMode) return;
      await api('/api/mode', { body: { mode: m } });
      this.settings.work_mode = m;
      this.selected = []; this.pStage = 'all'; this.pLogo = ''; this.pSource = ''; this.qSource = '';
      await this.loadProducts();
      if (this.filtered.length) await this.openProduct(this.filtered[0].id); else this.current = null;
      if (m === 'direct' && this.tab === 'logos') this.tab = 'products';
    },
    directSetOf(name) { return ((this.current && this.current.sets) || []).find(s => s.kind === 'source' && s.sku === name && s.queued); },
    toggleDsku(n) { const i = this.dsel.skus.indexOf(n); i >= 0 ? this.dsel.skus.splice(i, 1) : this.dsel.skus.push(n); },
    toggleDmain(u) { const i = this.dsel.main.indexOf(u); i >= 0 ? this.dsel.main.splice(i, 1) : this.dsel.main.push(u); },
    async directToQueue() {
      const again = this.dsel.skus.filter(n => this.directSetOf(n));
      if (again.length && !confirm(`${again.join('、')} 已经在待上架，重新加入会用现在勾选的图替换。继续？`)) return;
      try {
        this.jobs.unshift(await api(`/api/products/${this.current.id}/to-queue`, { body: { skus: this.dsel.skus, main: this.dsel.main, include_detail: this.dsel.detail } }));
        this.toast('正在下载原图并加入待上架，进度在右下角');
      } catch (e) { this.toast(e.message, true); }
    },
    async batchDirectQueue() {
      const ids = this.selected.filter(id => (this.products.find(p => p.id === id) || {}).mode === 'direct');
      if (!confirm(`把 ${ids.length} 个商品的全部颜色（原图 + 全部主图）加入待上架？`)) return;
      for (const id of ids) {
        try { this.jobs.unshift(await api(`/api/products/${id}/to-queue`, { body: { skus: [], main: null, include_detail: false } })); }
        catch (e) { this.toast(e.message, true); }
      }
    },
    async matchBind() {
      try { this.jobs.unshift(await api(`/api/products/${this.current.id}/match`, { body: { url: this.matchUrl } })); this.toast('采集助手正在打开这个 1688 商品核对规格…'); this.matchUrl = ''; }
      catch (e) { this.toast(e.message, true); }
    },
    async matchUnbind() {
      if (!confirm('清除这个商品的全部匹配结果？之前删掉的无同款规格会恢复，进货价清空。')) return;
      this.current = await api(`/api/products/${this.current.id}/match`, { method: 'DELETE' });
      this.loadProducts();
    },
    async autoMatch(ids, rematch = false) {
      try {
        this.jobs.unshift(await api('/api/products/auto-match', { body: { ids, rematch } }));
        this.toast('开始找 1688 同款：采集助手会在一个窗口里依次以图搜、打开 1688 核对，进度在右下角');
        setTimeout(this.refreshCurrent, 400);
      } catch (e) { this.toast(e.message, true); }
    },
    rematch() {
      if (!confirm('全部规格重新匹配？之前删掉的无同款规格会先恢复再重新核对（手动改判过的不动）。')) return;
      this.autoMatch([this.current.id], true);
    },
    openPick(u) {
      const r = u.result || {};
      this.pick = { unit: u, offer: r.offer_id || '', sku: r.sku_name || '' };
    },
    async pickSet(offer) {
      const u = this.pick.unit;
      if (!offer && !confirm(`把「${u.name}」设为无同款？这个规格会从商品里删除，不会进入待上架。`)) return;
      try {
        this.current = await api(`/api/products/${this.current.id}/match/unit`, { body: { unit: u.key, offer_id: offer, sku_name: offer ? this.pick.sku : null } });
        this.pick = null; this.toast(offer ? '已设为同款' : '已设为无同款'); this.loadProducts();
        if (offer && !this.dsel.skus.includes(u.key)) this.dsel.skus.push(u.key);
      } catch (e) { this.toast(e.message, true); }
    },
    unitStatusName(u) {
      return { matched: '同款', none: '无同款', error: '失败可重试', todo: '未匹配', pending: '排队中', searching: '以图搜中', verifying: '核对 1688' }[u.status] || u.status;
    },
    unitWhy(u) {
      const w = Object.values(u.why || {}).filter(Boolean);
      if (w.length) return w[0];
      return (u.cands && !u.cands.length) ? '以图搜没有找到 1688 候选' : '候选的图片和规格都对不上';
    },
    uniq(a) { return [...new Set((a || []).filter(Boolean))]; },
    matchStep() {
      // 现在在做哪一步、做了多久：慢的时候也能看出来没卡住
      const w = (this.current && this.current.match_wait) || {}, c = this.mCount;
      const done = c.matched + c.none + c.error, sec = w.t ? Math.max(0, Math.round(Date.now() / 1000 - w.t)) : 0;
      const step = w.kind === 'imgsearch' ? `正在以图搜「${w.unit}」` : w.kind === 'offer' ? `正在打开 1688 商品 ${w.offer} 核对「${w.unit}」` : '正在本地核对图片';
      return `${step}… ${sec ? sec + ' 秒 · ' : ''}已完成 ${done}/${this.units.length} 个规格`;
    },
    matchStateName(m) {
      if (!m) return '';
      if (m.state === 'none') return '未匹配 1688';
      if (m.state === 'running') return '匹配中…';
      return m.matched ? '' : '无同款';
    },
    async refreshCurrent() {
      const p = await api(`/api/products/${this.current.id}`).catch(() => null);
      if (p) { this.current = p; if (this.lightbox && this.lightbox.set) this.syncLightbox(); } else this.current = null;
    },
    toggleSel(id) { const i = this.selected.indexOf(id); i >= 0 ? this.selected.splice(i, 1) : this.selected.push(id); },
    toggleAll() { this.selected = this.allSelected ? [] : this.filtered.map(p => p.id); },
    toggleSku(name) { const i = this.skuSel.indexOf(name); i >= 0 ? this.skuSel.splice(i, 1) : this.skuSel.push(name); },
    listingMatch(l, f) {
      const st = l.status || 'draft';
      return f === 'all' || (f === 'todo' && st === 'draft') || (f === 'running' && (st === 'uploading' || st === 'pending_stock' || st === 'waiting')) || st === f;
    },
    qFilters() { return { status: this.qFilter, logo: this.qLogo, subject: this.qSubject, shop: this.qShopF, source: this.qSource }; },
    qMatch(l, f = this.qFilters()) {
      if ((l.mode || 'logo') !== this.workMode) return false;  // 只看当前模式（采集印标 / 采集上架）的商品卡
      if (!this.listingMatch(l, f.status)) return false;
      if (f.logo && l.key !== f.logo) return false;
      if (f.source && (l.source || '1688') !== f.source) return false;
      if (f.subject !== null && (l.subject_name || '') !== f.subject) return false;
      if (f.shop) {
        const [kind, sid] = f.shop.split(':');
        const on = (l.shops || []).some(x => x.shop_id === sid && ['uploaded', 'pending_stock'].includes(x.status));
        if ((kind === 'on') !== on) return false;
      }
      const q = this.qQuery.trim().toLowerCase();
      if (q && !((l.title || '') + ' ' + (l.product_title || '') + ' ' + (l.offer_id || '')).toLowerCase().includes(q)) return false;
      return true;
    },
    facetCount(dim, val) {
      // 左侧每一项的数字 = 选中它（其他筛选不变）之后能看到几张
      const f = { ...this.qFilters(), [dim]: val };
      return this.listings.filter(l => this.qMatch(l, f)).length;
    },
    sourceName(s) { return { '1688': '1688', wb: 'WB', pdd: '拼多多', ozon: 'Ozon', local: '本地' }[s] || s; },
    iconOk(s) { return ['1688', 'pdd', 'ozon', 'wb', 'aiprice'].includes(s) && !this.iconBad[s]; },
    startLogin(k) {
      this.loginBusy[k] = true;
      window.postMessage({ logoStudio: 'login', platform: k }, '*');
      this.toast('已弹出登录窗口：登录成功后会自动关闭');
      setTimeout(() => { this.loginBusy[k] = false; }, 15 * 60 * 1000);
    },
    async skipLogin(k) {
      const r = await api('/api/login-skip', { body: { platform: k, skip: true } });
      this.ext = { ...this.ext, login_skip: r.login_skip };
    },
    srcIcon(s) {
      // 来源平台图标（品牌色简化图形）
      const box = (bg, inner) => `<svg viewBox="0 0 24 24" width="18" height="18"><rect width="24" height="24" rx="6" fill="${bg}"/>${inner}</svg>`;
      const txt = (t, size = 11) => `<text x="12" y="16" text-anchor="middle" font-size="${size}" font-weight="800" fill="#fff" font-family="Arial, sans-serif">${t}</text>`;
      return {
        '1688': box('#FF6A00', '<path d="M5.5 9.5c1.6 5 11.4 5 13 0" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/><circle cx="17.6" cy="7.6" r="1.6" fill="#fff"/>'),
        pdd: box('#E02E24', txt('拼', 13)),
        ozon: box('#005BFF', '<circle cx="12" cy="12" r="5.6" fill="none" stroke="#fff" stroke-width="2.6"/>'),
        wb: box('url(#wbg)', '<defs><linearGradient id="wbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#CB11AB"/><stop offset="1" stop-color="#5B1BA8"/></linearGradient></defs>' + txt('WB', 9.5)),
        local: box('#8b5cf6', txt('本', 12)),
        aiprice: box('#7c3aed', txt('Ai', 10)),
      }[s] || box('#a78bfa', txt('?'));
    },
    sourcesOf(list) {
      const order = ['1688', 'wb', 'pdd', 'ozon'], have = new Set(list.map(x => x.source || '1688'));
      return [...order.filter(s => have.has(s)), ...[...have].filter(s => !order.includes(s))];
    },
    sourceCount(s) {
      return this.products.filter(p => (p.mode || 'logo') === this.workMode && (!s || p.source === s)
        && (this.pStage === 'all' || p.stage === this.pStage)).length;
    },
    logoMatch(p) {
      const f = this.pLogo, ids = p.logo_ids || [];
      if (!f) return true;
      return f.startsWith('!') ? !ids.includes(f.slice(1)) : ids.includes(f);
    },
    stageCount(k) { return this.products.filter(p => (p.mode || 'logo') === this.workMode && (k === 'all' || p.stage === k) && this.logoMatch(p) && (!this.pSource || p.source === this.pSource)).length; },
    setLive(st) {
      // 这套图在哪些店铺已上架（按上架记录里这个款式的 nmID）
      const L = ((this.current && this.current.listings) || {})[st.kind === 'source' ? 'src' : (st.logo_id || 'none')];
      const v = L && L.variants.find(x => x.sid === st.id);
      if (!v) return [];
      return Object.entries(L.pub || {}).filter(([, P]) => P.status && P.status !== 'draft' && P.codes && P.nm && P.nm[P.codes[v.sku]])
        .map(([sid, P]) => (this.shopName(sid) || '已删除的店铺') + (P.status === 'uploaded' ? '' : '（' + this.statusName(P.status) + '）'));
    },
    hhmm(t) { const d = new Date(t * 1000); return d.toTimeString().slice(0, 5); },
    lkey(l) { return `${l.pid}:${l.key}`; },
    toggleQ(id) { const i = this.qSel.indexOf(id); i >= 0 ? this.qSel.splice(i, 1) : this.qSel.push(id); },

    // ---------- 生成 ----------
    async generate() {
      if (!this.apiReady) { this.toast('请先在「设置」里填写中转站地址和令牌', true); this.tab = 'settings'; return; }
      this.generating = true;
      try {
        this.jobs.unshift(await api(`/api/products/${this.current.id}/generate`, { body: { skus: this.skuSel, logo_id: this.genLogo, style: this.genStyle } }));
        this.toast('已开始生成，进度在右下角');
        this.skuSel = [];
        setTimeout(this.refreshCurrent, 600);
      } catch (e) { this.toast(e.message, true); }
      this.generating = false;
    },
    async swapLogo(st, e) {
      const id = e.target.value;
      e.target.value = '';
      if (!id) return;
      const name = (this.logos.find(l => l.id === id) || {}).name;
      const n = st.images.filter(i => i.place || (i.logo && !i.base)).length;
      if (!confirm(`用「${name}」生成一套新图？衣服底图全部沿用，只有 ${n} 张需要重新印胸口 logo，其余在本地处理。`)) return;
      try {
        this.jobs.unshift(await api(`/api/products/${this.current.id}/sets/${st.id}/swap`, { body: { logo_id: id } }));
        setTimeout(this.refreshCurrent, 600);
      } catch (err) { this.toast(err.message, true); }
    },
    async regen(st, i, extra) {
      try {
        this.jobs.unshift(await api(`/api/products/${this.current.id}/sets/${st.id}/images/${i}/regen`, { body: { extra: extra || null } }));
        setTimeout(this.refreshCurrent, 400);
      } catch (e) { this.toast(e.message, true); }
    },
    async sizeChart(st) {
      try {
        this.jobs.unshift(await api(`/api/products/${this.current.id}/sets/${st.id}/size-chart`, { body: {} }));
        setTimeout(this.refreshCurrent, 600);
      } catch (e) { this.toast(e.message, true); }
    },
    resetShots() {
      this.settings.shots = this.defaultShots.map(x => ({ ...x }));
      this.settings.shot_count = 6;
      this.settings.size_chart_auto = true;
      this.toast('已恢复推荐模板，记得点「保存设置」');
    },
    async setQueued(st, queued) {
      this.current = await api(`/api/products/${this.current.id}/sets/${st.id}/queue`, { body: { queued } });
      this.toast(queued ? '已移入待上架' : '已移出待上架');
      this.loadProducts();
    },
    async deleteSet(st) {
      const live = this.setLive(st);
      if (!confirm(`删除「${st.sku}${st.logo_name ? ' / ' + st.logo_name : ''}」这套图？` + (live.length ? `\n\n这套图已经上架到：${live.join('、')}。删除只删本机图片，不影响 WB 上的商品，但以后就不能用它再上到其他店铺了。` : ''))) return;
      this.current = await api(`/api/products/${this.current.id}/sets/${st.id}`, { method: 'DELETE' });
      this.loadProducts();
    },
    openLightbox(st, i) {
      const im = st.images[i];
      this.lightbox = { set: st, idx: i, src: this.fileUrl(this.current.id, im.file, im.v), extra: im.extra || '' };
    },
    syncLightbox() {
      const st = (this.current.sets || []).find(s => s.id === this.lightbox.set.id);
      if (st) { this.lightbox.set = st; const im = st.images[this.lightbox.idx]; this.lightbox.src = this.fileUrl(this.current.id, im.file, im.v); }
    },

    // ---------- 商品理解 ----------
    async saveInfo(patch) {
      try { this.current = await api(`/api/products/${this.current.id}/info`, { body: patch }); }
      catch (e) { this.toast(e.message, true); }
    },
    async readSizeTable() {
      this.readingSize = true;
      try { this.current = await api(`/api/products/${this.current.id}/size-table`, { body: {} }); this.toast('已识别尺码表，请在下面核对一下数字'); }
      catch (e) { this.toast(e.message, true); }
      this.readingSize = false;
    },
    async analyze() {
      this.analyzing = true;
      try { this.current = await api(`/api/products/${this.current.id}/analyze`, { body: {} }); this.toast('商品理解完成'); }
      catch (e) { this.toast(e.message, true); }
      this.analyzing = false;
    },

    // ---------- 待上架 / WB ----------
    statusName(st) { return { draft: '草稿', uploading: '上架中', pending_stock: '库存同步中', waiting: '等待限流结束', uploaded: '已上架', failed: '失败' }[st || 'draft'] || st; },
    shopName(id) { return (this.shops.find(s => s.id === id) || {}).name; },
    async loadShops() {
      const r = await api('/api/shops');
      this.shops = r.shops; this.shopCurrent = r.current || (r.shops[0] || {}).id || '';
      if (this.shops.length && !this._dirsTried) this.loadDirs();  // 每个店铺只取一次，失败也不反复请求
    },
    async loadDirs() {
      this._dirsTried = true;
      try {
        const [colors, countries] = await Promise.all([api('/api/wb/directory/colors'), api('/api/wb/directory/countries')]);
        this.dir = { colors, countries };
      } catch (e) { /* 令牌没有 Content 权限时目录为空，手动填写 */ }
    },
    openShops() { this.showShops = true; this.shopTest = {}; this.loadShops(); },
    async addShop() {
      this.shopBusy = true;
      try {
        const r = await api('/api/shops', { body: this.shopForm });
        this.shopForm = { name: '', token: '' };
        this.warehouses[r.shop.id] = r.warehouses;
        await this.loadShops();
        this.toast(r.warehouses.length ? `已绑定，找到 ${r.warehouses.length} 个仓库` : '已绑定（没读到仓库，库存写入前请确认令牌有 Marketplace 权限）');
      } catch (e) { this.toast(e.message, true); }
      this.shopBusy = false;
    },
    async saveShop(s, patch) {
      try { await api(`/api/shops/${s.id}`, { body: patch }); this.toast('已保存'); } catch (e) { this.toast(e.message, true); }
    },
    async replaceToken(s, e) {
      const token = e.target.value.trim();
      if (!token) return;
      await this.saveShop(s, { token });
      e.target.value = '';
      this.loadShops();
    },
    async loadWarehouses(s) {
      try { this.warehouses[s.id] = await api(`/api/shops/${s.id}/warehouses`); this.toast(`读到 ${this.warehouses[s.id].length} 个仓库`); }
      catch (e) { this.toast(e.message, true); }
    },
    async pickWarehouse(s, id) {
      const w = (this.warehouses[s.id] || []).find(x => String(x.id) === String(id));
      await this.saveShop(s, { warehouse_id: w ? w.id : null, warehouse_name: w ? w.name : '' });
      this.loadShops();
    },
    async testShop(s) {
      this.shopTest[s.id] = { '测试中': '…' };
      try { this.shopTest[s.id] = await api(`/api/shops/${s.id}/test`, { body: {} }); } catch (e) { this.shopTest[s.id] = { '错误': e.message }; }
    },
    async setCurrentShop(id) {
      await api(`/api/shops/${id}/current`, { body: {} });
      this.shopCurrent = id; this.dir = { colors: [], countries: [] }; this._dirsTried = false; this.loadDirs();
      this.qSel = []; this.loadQueue(); this.loadProducts();  // 上架状态按店铺显示：切换后重新加载
    },
    async deleteShop(s) {
      if (!confirm(`删除店铺「${s.name}」？只删除本机保存的令牌，不影响 WB 上的商品。`)) return;
      await api(`/api/shops/${s.id}`, { method: 'DELETE' });
      this.loadShops();
    },

    async openEditor(l) {
      try {
        const r = await api(`/api/listings/${l.pid}/${l.key}`);
        this.ed = { ...this.edFrom(r), meta: [], tnved: [], subjQ: '', subjResults: [], sugReason: '', pf: null, aiNotes: [], aiRejected: [], allCharcs: false, busy: '' };
        if (this.ed.L.subject_id) this.loadSubjectData();
      } catch (e) { this.toast(e.message, true); }
    },
    edFrom(r) {
      const L = JSON.parse(JSON.stringify(r.listing));
      L.charcs = L.charcs || {};
      return { L, summary: r.summary, options: r.set_options, prices: r.prices, codes: r.codes, product: r.product, snap: this.editSig(L) };
    },
    editSig(L) {
      return JSON.stringify(['subject_id', 'title', 'description', 'brand', 'country', 'tnved', 'charcs', 'variants', 'sizes', 'pricing', 'package', 'stock'].map(k => L[k]));
    },
    closeEditor() {
      if (this.ed && this.ed.snap !== this.editSig(this.ed.L) && !confirm('关闭编辑？没点「保存」的修改会丢失。')) return;
      this.ed = null;
    },
    optCover(v) { const o = (this.ed.options[v.sku] || []).find(x => x.sid === v.sid); return o ? o.cover : ''; },
    async loadSubjectData() {
      const sid = this.ed.L.subject_id;
      this.ed.busy = 'meta';
      try {
        const [meta, tn] = await Promise.all([api(`/api/wb/charcs/${sid}`), api(`/api/wb/directory/tnved?subject=${sid}`).catch(() => [])]);
        if (this.ed && this.ed.L.subject_id === sid) { this.ed.meta = meta; this.ed.tnved = tn; }
      } catch (e) { this.toast(e.message, true); }
      if (this.ed) this.ed.busy = '';
    },
    async searchSubjects() {
      if (!this.ed.subjQ.trim()) return;
      this.ed.busy = 'subj';
      try { this.ed.subjResults = await api(`/api/wb/subjects?q=${encodeURIComponent(this.ed.subjQ.trim())}`); if (!this.ed.subjResults.length) this.toast('没有找到，换个词试试（俄文更准）', true); }
      catch (e) { this.toast(e.message, true); }
      this.ed.busy = '';
    },
    pickSubject(r) {
      if (this.ed.L.subject_id === r.id) return;
      if (this.ed.L.subject_id && this.ed.L.subject_id !== r.id && Object.keys(this.ed.L.charcs).length && !confirm('换类目会清空已经填写的属性，继续？')) return;
      this.ed.L.charcs = {}; this.ed.L.tnved = '';
      this.ed.L.subject_id = r.id; this.ed.L.subject_name = r.name;
      this.loadSubjectData();
    },
    charcText(c) { return (this.ed.L.charcs[c.charcID] || []).join(', '); },
    setCharc(c, text) {
      let vals = c.charcType === 4 ? [text.trim()].filter(Boolean) : text.split(/[,，;；]/).map(x => x.trim()).filter(Boolean);
      vals = vals.slice(0, c.maxCount || 1);
      if (vals.length) this.ed.L.charcs[c.charcID] = vals; else delete this.ed.L.charcs[c.charcID];
    },
    useEstimate() {
      const e = this.ed.L.package_estimate;
      Object.assign(this.ed.L.package, { length: e.lengthCm, width: e.widthCm, height: e.heightCm, weight: e.weightKg });
    },
    async saveListing(quiet) {
      const L = this.ed.L, s = this.ed.summary;
      const body = {};
      for (const k of ['subject_id', 'subject_name', 'title', 'description', 'brand', 'country', 'tnved', 'charcs', 'variants', 'sizes', 'pricing', 'package', 'stock']) body[k] = L[k];
      body.sizes = L.sizes.map(x => ({ ...x, cny_override: x.cny_override === '' ? null : x.cny_override }));
      try {
        const r = await api(`/api/listings/${s.pid}/${s.key}`, { body });
        Object.assign(this.ed, this.edFrom(r));
        if (!quiet) this.toast('已保存');
        this.loadQueue();
        return true;
      } catch (e) { this.toast(e.message, true); return false; }
    },
    async preflight() {
      if (!(await this.saveListing(true))) return;
      this.ed.busy = 'pf';
      try { this.ed.pf = await api(`/api/listings/${this.ed.summary.pid}/${this.ed.summary.key}/preflight`, { body: {} }); }
      catch (e) { this.toast(e.message, true); }
      if (this.ed) this.ed.busy = '';
      return this.ed && this.ed.pf;
    },
    async aiFill() {
      if (!(await this.saveListing(true))) return;
      this.ed.busy = 'ai';
      try {
        const r = await api(`/api/listings/${this.ed.summary.pid}/${this.ed.summary.key}/ai-fill`, { body: {} });
        Object.assign(this.ed, this.edFrom(r), { aiNotes: r.ai_notes, aiRejected: r.ai_rejected });
        this.toast('AI 已填写，请逐项核对后再上架');
      } catch (e) { this.toast(e.message, true); }
      if (this.ed) this.ed.busy = '';
    },
    confirmUpload(n, title) {
      return confirm(`确认上架到 WB 店铺「${this.shopName(this.shopCurrent)}」？\n\n${title}\n\n这会在 WB 上真实创建 ${n} 个款式的商品卡，并上传图片、价格和库存。\n同一张卡重复点上架是安全的：程序会按供应商编码回查，只续传没完成的步骤，不会重复建卡。`);
    },
    async saveAndUpload() {
      const pf = await this.preflight();
      if (!pf) return;
      if (!pf.ok) { this.toast('预检没通过，请先处理红色的问题', true); return; }
      if (!this.confirmUpload(this.ed.L.variants.filter(v => v.enabled).length, this.ed.L.title)) return;
      await this.startUpload(this.ed.summary);
    },
    async uploadListing(l) {
      try {
        const pf = await api(`/api/listings/${l.pid}/${l.key}/preflight`, { body: {} });
        if (!pf.ok) { this.toast('预检没通过：' + pf.errors.slice(0, 3).join('；'), true); this.openEditor(l).then(() => { if (this.ed) this.ed.pf = pf; }); return; }
        if (!this.confirmUpload(l.variants.filter(v => v.enabled).length, l.title || l.product_title)) return;
        await this.startUpload(l);
      } catch (e) { this.toast(e.message, true); }
    },
    async startUpload(l) {
      try {
        this.jobs.unshift(await api(`/api/listings/${l.pid}/${l.key}/upload`, { body: {} }));
        this.toast('已开始上架，进度在卡片上和右下角');
        setTimeout(this.loadQueue, 800);
      } catch (e) { this.toast(e.message, true); }
    },
    async batchUpload() {
      const items = this.listings.filter(l => this.qSel.includes(this.lkey(l)));
      const bad = [];
      for (const l of items) {
        const pf = await api(`/api/listings/${l.pid}/${l.key}/preflight`, { body: {} }).catch(e => ({ ok: false, errors: [e.message] }));
        if (!pf.ok) bad.push(`${(l.title || l.product_title).slice(0, 24)}：${pf.errors[0]}`);
      }
      if (bad.length) { alert(`有 ${bad.length} 张卡预检没通过，先处理后再批量上架：\n\n` + bad.join('\n')); return; }
      const n = items.reduce((k, l) => k + l.variants.filter(v => v.enabled).length, 0);
      if (!confirm(`确认把 ${items.length} 张商品卡（共 ${n} 个款式）上架到 WB 店铺「${this.shopName(this.shopCurrent)}」？会按顺序逐张上架。`)) return;
      try { this.jobs.unshift(await api('/api/listings/batch-upload', { body: { items: items.map(l => ({ pid: l.pid, key: l.key })) } })); this.toast('已开始批量上架'); }
      catch (e) { this.toast(e.message, true); }
    },
    selectedListings() { return this.listings.filter(l => this.qSel.includes(this.lkey(l))).map(l => ({ pid: l.pid, key: l.key })); },
    async batchAi() {
      if (!this.settings.listing_model && !this.settings.text_model) { this.toast('请先在「设置」里填写「上架资料模型」（如 gpt-6-sol）', true); return; }
      if (!confirm(`对 ${this.qSel.length} 张商品卡做 AI 填写？\n没选类目的会自动匹配类目；标题（每个颜色独立标题）、说明、属性、包装尺寸重量、海关编码会被覆盖。\n每张卡约 2 次 AI 调用。`)) return;
      try { this.jobs.unshift(await api('/api/listings/batch-ai', { body: { items: this.selectedListings() } })); this.toast('已开始，进度在右下角'); }
      catch (e) { this.toast(e.message, true); }
    },
    openBatchEdit() { this.bulk = { mode: '', add: 0, mul: 12, add2: 0, fixed: '', discount: '', length: '', width: '', height: '', weight: '', stock: '', push: true }; },
    async applyBulk() {
      const b = this.bulk, patch = { pricing: {}, package: {} };
      if (b.mode === 'formula') Object.assign(patch.pricing, { mode: 'formula', add: b.add || 0, mul: b.mul || 1, add2: b.add2 || 0 });
      if (b.mode === 'fixed') { if (!(b.fixed > 0)) { this.toast('请填写固定价', true); return; } Object.assign(patch.pricing, { mode: 'fixed', fixed: b.fixed }); }
      if (b.discount !== '') patch.pricing.discount = Number(b.discount);
      for (const k of ['length', 'width', 'height', 'weight']) if (b[k] !== '') patch.package[k] = Number(b[k]);
      if (b.stock !== '') patch.stock = Number(b.stock);
      try {
        if (b.discount !== '' && (Number(b.discount) < 0 || Number(b.discount) > 99)) { this.toast('折扣率要在 0–99 之间', true); return; }
        const r = await api('/api/listings/batch-edit', { body: { items: this.selectedListings(), patch, push_prices: b.push } });
        if (r.job) this.jobs.unshift(r.job);
        this.toast(`已修改 ${r.count} 张` + (r.job ? '，正在把价格和折扣同步到 WB' : '')); this.bulk = null; this.loadQueue();
      } catch (e) { this.toast(e.message, true); }
    },
    diagText(d) {
      if (typeof d === 'string') return d;
      if (d && d.errors) return [...d.errors.map(e => '✗ ' + e), ...d.warnings.map(w => '! ' + w)].join('　') || '全部通过';
      return Array.isArray(d) ? d.join('、') || '（空）' : JSON.stringify(d);
    },
    openDiag(s) { this.diag = { shop: s, target: this.listings[0] ? this.lkey(this.listings[0]) : '', report: null, busy: false }; },
    async runDiag() {
      this.diag.busy = true;
      const [pid, key] = this.diag.target ? this.diag.target.split(':') : [null, null];
      try { this.diag.report = await api(`/api/shops/${this.diag.shop.id}/diagnose`, { body: { pid, key } }); }
      catch (e) { this.toast(e.message, true); }
      if (this.diag) this.diag.busy = false;
    },
    async suggestSubject() {
      this.ed.busy = 'sug';
      try {
        const r = await api(`/api/listings/${this.ed.summary.pid}/${this.ed.summary.key}/suggest-subject`, { body: {} });
        this.ed.subjResults = r.options;
        if (r.suggest) {
          const src = { memory: '记住的选择', ai: 'AI 匹配', keyword: '关键词匹配' }[r.suggest.source];
          this.ed.sugReason = `${src}：${r.suggest.name}。${r.suggest.reason || ''}　下面是其他候选，点一下可以换。`;
          this.pickSubject({ id: r.suggest.id, name: r.suggest.name });
        } else this.ed.sugReason = '没能自动匹配，请从候选里选或搜索。';
      } catch (e) { this.toast(e.message, true); }
      if (this.ed) this.ed.busy = '';
    },
    async pushPrices() {
      if (!(await this.saveListing(true))) return;
      try { this.jobs.unshift(await api(`/api/listings/${this.ed.summary.pid}/${this.ed.summary.key}/push-prices`, { body: {} })); this.toast('正在同步价格和折扣到 WB'); }
      catch (e) { this.toast(e.message, true); }
    },
    async removeListing(l) {
      if (!confirm('把这张卡移出待上架？套图会保留在商品页，可以再移回来。')) return;
      try { await api(`/api/listings/${l.pid}/${l.key}/remove`, { body: {} }); this.loadQueue(); this.loadProducts(); }
      catch (e) { this.toast(e.message, true); }
    },
    async exportQueue() {
      const items = this.listings.filter(l => this.qSel.includes(this.lkey(l)))
        .flatMap(l => l.variants.filter(v => v.enabled).map(v => ({ pid: l.pid, sid: v.sid })));
      this.jobs.unshift(await api('/api/export', { body: { items } }));
    },

    // ---------- 商品 ----------
    async batchGenerate() {
      if (!this.apiReady) { this.toast('请先在「设置」里填写中转站地址和令牌', true); this.tab = 'settings'; return; }
      const n = this.selected.length;
      if (!confirm(`对 ${n} 个商品批量生成套图（${this.batch.sku_scope === 'all' ? '全部颜色' : '每个商品第一个颜色'}，每套约 ${this.activeShots.length + 1} 次生图调用）？`)) return;
      try { this.jobs.unshift(await api('/api/batch/generate', { body: { ids: this.selected, ...this.batch } })); this.toast('已开始批量生成，进度在右下角'); }
      catch (e) { this.toast(e.message, true); }
    },
    async batchQueue() {
      const r = await api('/api/batch/queue', { body: { ids: this.selected } });
      this.toast(r.count ? `已移入待上架 ${r.count} 套` : '选中的商品没有已完成、未移入的套图');
      this.loadProducts();
      if (this.current) this.refreshCurrent();
    },
    async deleteSel() {
      if (!confirm(`确定删除选中的 ${this.selected.length} 个商品（包括已生成的套图）？`)) return;
      await api('/api/products/delete', { body: { ids: this.selected } });
      if (this.current && this.selected.includes(this.current.id)) this.current = null;
      this.selected = [];
      await this.loadProducts();
    },
    async importLocal(e) {
      const files = [...e.target.files];
      if (!files.length) return;
      const fd = new FormData();
      files.forEach(f => fd.append('files', f));
      try {
        const p = await api('/api/products/import', { form: fd });
        await this.loadProducts();
        await this.openProduct(p.id);
        this.toast('已导入');
      } catch (err) { this.toast(err.message, true); }
      e.target.value = '';
    },

    // ---------- 采集 ----------
    async startScrape() {
      try {
        this.jobs.unshift(await api('/api/scrape', { body: this.scrapeForm }));
        this.showScrape = false;
        this.scrapeForm.links = ''; this.scrapeForm.shops = '';
        window.postMessage({ logoStudio: 'kick' }, '*');
      } catch (e) { this.toast(e.message, true); }
    },
    async scrapeTabs() {
      try {
        this.jobs.unshift(await api('/api/scrape/tabs', { body: {} })); this.showScrape = false;
        window.postMessage({ logoStudio: 'kick' }, '*');
      } catch (e) { this.toast(e.message, true); }
    },
    async openBrowser() { await api('/api/browser/open', { body: {} }); this.toast('正在打开浏览器，第一次使用请在窗口中登录 1688'); },
    async cancelJob(j) { await api(`/api/jobs/${j.id}/cancel`, { body: {} }); },
    hideJob(j) { this.hiddenJobs.push(j.id); },
    async openFolder(path) { await api('/api/open-folder', { body: { path } }).catch(e => this.toast(e.message, true)); },

    // ---------- Logo ----------
    dropLogo(e) { this.dropOver = false; this.uploadLogos(e.dataTransfer.files); },
    async uploadLogos(files) {
      for (const f of files) {
        const fd = new FormData();
        fd.append('file', f); fd.append('name', f.name.replace(/\.[^.]+$/, ''));
        try { await api('/api/logos', { form: fd }); } catch (e) { this.toast(e.message, true); }
      }
      await this.loadLogos();
      if (this.$refs.logoInput) this.$refs.logoInput.value = '';
    },
    async saveLogo(l, patch) { Object.assign(l, await api(`/api/logos/${l.id}`, { body: patch })); this.toast('已保存'); },
    async deleteLogo(l) {
      if (!confirm(`删除 logo「${l.name}」？已生成的图不受影响。`)) return;
      await api(`/api/logos/${l.id}`, { method: 'DELETE' });
      await this.loadLogos();
    },

    // ---------- 设置 ----------
    async saveSettings() {
      const s = await api('/api/settings', { body: this.settings });
      this.settings = s.settings;
      this.toast('设置已保存');
    },
    async testApi() {
      await this.saveSettings();
      this.testMsg = '测试中…';
      const r = await api('/api/settings/test', { body: {} });
      this.testMsg = r.message;
      this.models = r.models || [];
    },
    async testListing() {
      await this.saveSettings();
      this.listingTestMsg = '测试中…';
      const r = await api('/api/settings/test-listing', { body: {} }).catch(e => ({ message: e.message }));
      this.listingTestMsg = r.message;
    },
    async loadAiCalls() { this.aiCalls = await api('/api/ai-calls').catch(() => null); },
    async addStyleRef(e) {
      const f = e.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      this.settings.style_refs = await api('/api/style-refs', { form: fd });
      e.target.value = '';
    },
    async delStyleRef(r) { this.settings.style_refs = await api(`/api/style-refs/${r}`, { method: 'DELETE' }); },
  },
}).mount('#app');
