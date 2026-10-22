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

createApp({
  data: () => ({
    tab: 'products',
    products: [], logos: [], current: null, imgId: null,
    query: '', selected: [], activeLogo: null,
    settings: {}, browserState: { state: 'idle', message: '' }, jobs: [], hiddenJobs: [],
    showScrape: false, scrapeForm: { links: '', shops: '', max_pages: 5 },
    busy: false, compare: false, wrapSize: { w: 600, h: 600 }, dragging: false, dragBox: null, natural: { w: 1, h: 1 },
    toastMsg: '', toastErr: false, dropOver: false, testMsg: '', variantTarget: null,
    colorOpts: [['', '跟随Logo'], ['auto', '自动'], ['light', '浅'], ['dark', '深'], ['original', '原色']],
    styleOpts: [['', '跟随Logo'], ['print', '印花'], ['embroidery', '刺绣']],
  }),
  computed: {
    filtered() {
      const q = this.query.trim().toLowerCase();
      return q ? this.products.filter(p => (p.title || '').toLowerCase().includes(q) || (p.offer_id || '').includes(q)) : this.products;
    },
    allSelected() { return this.filtered.length && this.filtered.every(p => this.selected.includes(p.id)); },
    img() { return this.current ? (this.current.images.find(i => i.id === this.imgId) || this.current.images[0]) : null; },
    place() { return (this.img && this.img.place) || {}; },
    printableCount() { return this.current ? this.current.images.filter(i => (i.place || {}).printable).length : 0; },
    shownSrc() {
      if (!this.img) return '';
      const f = this.compare || !this.img.out ? this.img.file : this.img.out;
      return this.fileUrl(this.current.id, f, this.img.v);
    },
    box() {
      if (this.dragBox) return this.dragBox;
      if (!this.img) return null;
      if (this.img.rect) return { ...this.img.rect, angle: this.place.angle || 0 };
      const p = this.place;
      if (p.x == null) return null;
      const logo = this.logos.find(l => l.id === this.current.logo_id);
      const aspect = logo ? logo.aspect : 4;
      return { x: p.x, y: p.y, w: p.w, h: p.w * this.natural.w / this.natural.h / aspect, angle: p.angle || 0 };
    },
    boxStyle() {
      const b = this.box;
      return {
        // 四周留 6px，避免小 logo 的框太小不好拖
        left: `calc(${(b.x - b.w / 2) * 100}% - 6px)`, top: `calc(${(b.y - b.h / 2) * 100}% - 6px)`,
        width: `calc(${b.w * 100}% + 12px)`, height: `calc(${b.h * 100}% + 12px)`, transform: `rotate(${b.angle || 0}deg)`,
      };
    },
    visibleJobs() { return this.jobs.filter(j => !this.hiddenJobs.includes(j.id)).slice(0, 3); },
    browserDot() {
      return { ready: 'ok', need_login: 'warn', need_verify: 'warn', starting: 'warn', error: 'err' }[this.browserState.state] || '';
    },
    browserLabel() {
      return { ready: '浏览器已连接', need_login: '等待登录 1688', need_verify: '等待滑块验证', starting: '浏览器启动中', error: '浏览器出错' }[this.browserState.state] || '浏览器未启动';
    },
  },
  async mounted() {
    await Promise.all([this.loadProducts(), this.loadLogos(), this.poll()]);
    if (this.products.length) this.openProduct(this.products[0].id);
    setInterval(this.poll, 1500);
    window.addEventListener('keydown', this.onKey);
    this._ro = new ResizeObserver(() => this.measure());
    this.$watch('current', () => this.$nextTick(() => { if (this.$refs.wrap) this._ro.observe(this.$refs.wrap); this.measure(); }));
  },
  methods: {
    toast(msg, err = false) {
      this.toastMsg = msg; this.toastErr = err;
      clearTimeout(this._t); this._t = setTimeout(() => (this.toastMsg = ''), err ? 4500 : 2200);
    },
    fileUrl(pid, f, v) { return `/data/products/${pid}/${f}?v=${v || 0}`; },
    logoUrl(l, slot = 'logo') { return `/data/logos/${l.id}/${l[slot]}?v=${l.v || 0}`; },
    logoName(id) { const l = this.logos.find(l => l.id === id); return l ? l.name : '已删除的 logo'; },
    fmtPrice(a, b) { return a === b || b == null ? `${a}` : `${a}-${b}`; },
    jobStatus(j) { return { running: `${j.done}/${j.total || '?'}`, done: '完成', failed: '失败', pending: '排队中' }[j.status]; },

    async loadProducts() { this.products = await api('/api/products'); },
    async loadLogos() { this.logos = await api('/api/logos'); },
    async poll() {
      try {
        const s = await api('/api/state');
        this.browserState = s.browser;
        if (!this.settingsLoaded) { this.settings = s.settings; this.settingsLoaded = true; }
        const prevDone = this.jobs.reduce((n, j) => n + j.done + (j.status === 'done' ? 1 : 0), 0);
        this.jobs = s.jobs;
        const nowDone = this.jobs.reduce((n, j) => n + j.done + (j.status === 'done' ? 1 : 0), 0);
        if (nowDone !== prevDone) {
          await this.loadProducts();
          if (this.current) this.refreshCurrent();
        }
      } catch (e) { /* 服务未就绪时忽略 */ }
    },
    async openProduct(id) {
      this.current = await api(`/api/products/${id}`);
      const first = this.current.images.find(i => (i.place || {}).printable) || this.current.images[0];
      this.imgId = first ? first.id : null;
    },
    async refreshCurrent() {
      const p = await api(`/api/products/${this.current.id}`).catch(() => null);
      if (p) this.current = p; else this.current = null;
    },
    setCurrent(p) {
      this.current = p;
      const s = this.products.find(x => x.id === p.id);
      if (s) {
        const t = p.images.find(i => i.out) || p.images[0];
        Object.assign(s, { logo_id: p.logo_id, thumb: t ? (t.out || t.file) : null, v: t ? t.v : 0 });
      }
    },
    toggleSel(id) {
      const i = this.selected.indexOf(id);
      i >= 0 ? this.selected.splice(i, 1) : this.selected.push(id);
    },
    toggleAll() { this.selected = this.allSelected ? [] : this.filtered.map(p => p.id); },
    measure() {
      const el = this.$refs.wrap;
      if (el) this.wrapSize = { w: Math.max(el.clientWidth - 40, 200), h: Math.max(el.clientHeight - 32, 200) };
    },
    imgLoaded(e) { this.natural = { w: e.target.naturalWidth, h: e.target.naturalHeight }; },
    onKey(e) {
      if (this.tab !== 'products' || !this.current || ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
      const imgs = this.current.images, k = imgs.findIndex(i => i.id === this.img.id);
      if (e.key === 'ArrowRight' && k < imgs.length - 1) this.imgId = imgs[k + 1].id;
      if (e.key === 'ArrowLeft' && k > 0) this.imgId = imgs[k - 1].id;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const list = this.filtered, j = list.findIndex(p => p.id === this.current.id) + (e.key === 'ArrowDown' ? 1 : -1);
        if (list[j]) { e.preventDefault(); this.openProduct(list[j].id); }
      }
    },

    // ---------- 印标 ----------
    async applyLogo(logoId) {
      this.activeLogo = logoId;
      if (!this.current) { if (logoId) this.toast('已选中 logo，可以在左侧勾选商品批量印'); return; }
      if (!logoId && !this.current.logo_id) return;
      this.busy = true;
      try {
        this.setCurrent(await api(`/api/products/${this.current.id}/apply`, { body: { logo_id: logoId } }));
        if (logoId && !this.printableCount) this.toast('这个商品没有识别到可印的图，可以选中图片后打开「这张图印 logo」', true);
      } catch (e) { this.toast(e.message, true); }
      this.busy = false;
    },
    async patchPlace(patch) {
      this.busy = true;
      try {
        this.setCurrent(await api(`/api/products/${this.current.id}/images/${this.img.id}/place`, { body: patch }));
      } catch (e) { this.toast(e.message, true); }
      this.busy = false; this.dragBox = null;
    },
    async relocateImage() {
      this.busy = true;
      try { this.setCurrent(await api(`/api/products/${this.current.id}/images/${this.img.id}/relocate`, { body: {} })); }
      catch (e) { this.toast(e.message, true); }
      this.busy = false;
    },
    async refine() {
      this.busy = true;
      this.toast('AI 精修中，通常需要 20~60 秒…');
      try { this.setCurrent(await api(`/api/products/${this.current.id}/images/${this.img.id}/refine`, { body: {} })); this.toast('精修完成'); }
      catch (e) { this.toast(e.message, true); }
      this.busy = false;
    },
    startCompare(e) { if (this.img.out && e.button === 0) this.compare = true; },
    startDrag(e, mode) {
      const rect = this.$refs.canvas.getBoundingClientRect();
      const start = { ...this.box }, sx = e.clientX, sy = e.clientY;
      this.dragging = true;
      const move = ev => {
        const dx = (ev.clientX - sx) / rect.width, dy = (ev.clientY - sy) / rect.height;
        if (mode === 'move') this.dragBox = { ...start, x: start.x + dx, y: start.y + dy };
        else {
          const w = Math.max(0.02, start.w + dx * 2);
          this.dragBox = { ...start, w, h: start.h * w / start.w };
        }
      };
      const up = () => {
        window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
        this.dragging = false;
        const b = this.dragBox;
        if (b) this.patchPlace({ x: b.x, y: b.y, w: b.w });
      };
      window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
    },

    // ---------- 批量 ----------
    async batchApply(logoId) {
      const job = await api('/api/batch/apply', { body: { ids: this.selected, logo_id: logoId } });
      this.jobs.unshift(job);
    },
    async exportSel() { this.jobs.unshift(await api('/api/export', { body: { ids: this.selected } })); },
    async exportOne() { this.jobs.unshift(await api('/api/export', { body: { ids: [this.current.id] } })); },
    async deleteSel() {
      if (!confirm(`确定删除选中的 ${this.selected.length} 个商品？`)) return;
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
      this.toast('正在导入并识别…');
      try {
        const p = await api('/api/products/import', { form: fd });
        await this.loadProducts();
        await this.openProduct(p.id);
        this.toast(`已导入 ${p.count} 张图，识别到 ${p.printable} 张可印`);
      } catch (err) { this.toast(err.message, true); }
      e.target.value = '';
    },

    // ---------- 采集 ----------
    async startScrape() {
      try {
        const job = await api('/api/scrape', { body: this.scrapeForm });
        this.jobs.unshift(job);
        this.showScrape = false;
        this.scrapeForm.links = ''; this.scrapeForm.shops = '';
      } catch (e) { this.toast(e.message, true); }
    },
    async openBrowser() { await api('/api/browser/open', { body: {} }); this.toast('正在打开浏览器，第一次使用请在窗口中登录 1688'); },
    async scrapeTabs() {
      try { this.jobs.unshift(await api('/api/scrape/tabs', { body: {} })); this.showScrape = false; }
      catch (e) { this.toast(e.message, true); }
    },
    async cancelJob(j) { await api(`/api/jobs/${j.id}/cancel`, { body: {} }); },
    hideJob(j) { this.hiddenJobs.push(j.id); },
    async openFolder(path) { await api('/api/open-folder', { body: { path } }).catch(e => this.toast(e.message, true)); },

    // ---------- Logo 库 ----------
    dropLogo(e) { this.dropOver = false; this.uploadLogos(e.dataTransfer.files); },
    async uploadLogos(files) {
      for (const f of files) {
        const fd = new FormData();
        fd.append('file', f); fd.append('name', f.name.replace(/\.[^.]+$/, ''));
        try { await api('/api/logos', { form: fd }); } catch (e) { this.toast(e.message, true); }
      }
      await this.loadLogos();
      this.$refs.logoInput && (this.$refs.logoInput.value = '');
    },
    async saveLogo(l, patch) { Object.assign(l, await api(`/api/logos/${l.id}`, { body: patch })); this.toast('已保存，重新点击该 logo 即可按新设置印标'); },
    pickVariant(l, slot) { this.variantTarget = { l, slot }; this.$refs.variantInput.click(); },
    async uploadVariant(e) {
      const f = e.target.files[0];
      if (!f || !this.variantTarget) return;
      const fd = new FormData(); fd.append('file', f);
      const { l, slot } = this.variantTarget;
      Object.assign(l, await api(`/api/logos/${l.id}/variant/${slot}`, { form: fd }));
      e.target.value = '';
    },
    async delVariant(l, slot) { Object.assign(l, await api(`/api/logos/${l.id}/variant/${slot}`, { method: 'DELETE' })); },
    async deleteLogo(l) {
      if (!confirm(`删除 logo「${l.name}」？`)) return;
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
    },
  },
}).mount('#app');
