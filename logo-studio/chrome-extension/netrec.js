// 在 1688 / AiPrice 页面最开始注入（页面自己的环境里）：记录页面发出的数据请求和返回内容。
// 以图搜的结果往往由页面脚本请求接口后再画出来，页面改版时直接从接口数据里读候选更稳；排查问题时也能看到每一步发生了什么。
(() => {
  if (window.__lsHooked) return;
  window.__lsHooked = true;
  window.__lsNetFull = [];
  const SKIP = /mmstat|matomo|google|gstatic|aplus|log\.aliyuncs|\.(css|js|png|jpe?g|gif|webp|svg|woff2?)(\?|$)/i;
  const keep = (url, body) => {
    try {
      if (SKIP.test(url) || !body || body.length > 1500000) return;
      const a = window.__lsNetFull;
      a.push({ url: String(url).slice(0, 500), body, t: Date.now() });
      while (a.length > 40) a.shift();
    } catch (e) { /* 忽略 */ }
  };
  const rec = x => {
    try {
      const a = JSON.parse(sessionStorage.getItem('__lsNet') || '[]');
      a.push(x);
      sessionStorage.setItem('__lsNet', JSON.stringify(a.slice(-60)));
    } catch (e) { /* 忽略 */ }
  };
  const of = window.fetch;
  window.fetch = async function (u, o) {
    const url = String((u && u.url) || u), m = (o && o.method) || (u && u.method) || 'GET';
    try {
      const r = await of.apply(this, arguments);
      if (!SKIP.test(url)) {
        r.clone().text().then(t => { keep(url, t); rec({ k: 'fetch', m, url: url.slice(0, 300), st: r.status, len: t.length, body: t.slice(0, 400) }); })
          .catch(() => rec({ k: 'fetch', m, url: url.slice(0, 300), st: r.status }));
      }
      return r;
    } catch (e) { rec({ k: 'fetch', m, url: url.slice(0, 300), err: String(e).slice(0, 200) }); throw e; }
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__ls = { m, url: String(u) }; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    const info = this.__ls || {};
    if (!SKIP.test(info.url || '')) {
      const sent = typeof FormData !== 'undefined' && b instanceof FormData ? 'FormData' : b ? String(b).slice(0, 200) : '';
      this.addEventListener('loadend', () => {
        let body = '';
        try { body = (this.responseType === '' || this.responseType === 'text') ? String(this.responseText || '') : (this.responseType === 'json' ? JSON.stringify(this.response) : ''); } catch (e) { /* 忽略 */ }
        keep(info.url, body);
        rec({ k: 'xhr', m: info.m, url: String(info.url).slice(0, 300), sent, st: this.status, len: body.length, body: body.slice(0, 400) });
      });
    }
    return os.apply(this, arguments);
  };
})();
