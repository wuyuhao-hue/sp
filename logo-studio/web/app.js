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
  }),
  computed: {
    filtered() {
      const q = this.query.trim().toLowerCase();
      return q ? this.products.filter(p => (p.title || '').toLowerCase().includes(q) || (p.offer_id || '').includes(q)) : this.products;
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
    queueCount() { return this.products.reduce((n, p) => n + (p.queued || 0), 0); },
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
        return this.ext.connected ? '采集助手已连接' : (this.extInstalled ? '采集助手连接中…' : '未安装采集助手');
      }
      return { ready: '浏览器已连接', need_login: '等待登录 1688', need_verify: '等待滑块验证', starting: '浏览器启动中', error: '浏览器出错' }[this.browserState.state] || '浏览器未启动';
    },
  },
  watch: {
    tab(t) { if (t === 'queue') this.loadQueue(); },
  },
  async mounted() {
    await Promise.all([this.loadProducts(), this.loadLogos(), this.poll()]);
    if (this.products.length) this.openProduct(this.products[0].id);
    setInterval(this.poll, 1500);
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
      this.queue = await api('/api/queue');
      this.qSel = this.qSel.filter(id => this.queue.some(q => q.set.id === id));
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
        }
      } catch (e) { /* 服务未就绪时忽略 */ }
    },
    async openProduct(id) {
      this.current = await api(`/api/products/${id}`);
      this.skuSel = []; this.skuView = null;
    },
    async refreshCurrent() {
      const p = await api(`/api/products/${this.current.id}`).catch(() => null);
      if (p) { this.current = p; if (this.lightbox && this.lightbox.set) this.syncLightbox(); } else this.current = null;
    },
    toggleSel(id) { const i = this.selected.indexOf(id); i >= 0 ? this.selected.splice(i, 1) : this.selected.push(id); },
    toggleAll() { this.selected = this.allSelected ? [] : this.filtered.map(p => p.id); },
    toggleSku(name) { const i = this.skuSel.indexOf(name); i >= 0 ? this.skuSel.splice(i, 1) : this.skuSel.push(name); },
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
      if (!confirm(`删除「${st.sku}${st.logo_name ? ' / ' + st.logo_name : ''}」这套图？`)) return;
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

    // ---------- 待上架 ----------
    async unqueue(q) {
      await api(`/api/products/${q.pid}/sets/${q.set.id}/queue`, { body: { queued: false } });
      this.loadQueue(); this.loadProducts();
    },
    async gotoSet(q) { this.tab = 'products'; await this.openProduct(q.pid); },
    async exportQueue() {
      const items = this.queue.filter(q => this.qSel.includes(q.set.id)).map(q => ({ pid: q.pid, sid: q.set.id }));
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
