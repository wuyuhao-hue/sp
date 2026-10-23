// 1688 / 拼多多 / Ozon / WB 商品页右下角的「采集到工作台」按钮。
// WB、Ozon 是单页应用：站内点进商品页不会重新加载，所以按网址随时显示/隐藏按钮。
(() => {
  if (document.getElementById('logo-studio-collect')) return;
  const btn = document.createElement('button');
  btn.id = 'logo-studio-collect';
  btn.textContent = '采集到工作台';
  Object.assign(btn.style, {
    position: 'fixed', right: '24px', bottom: '96px', zIndex: 2147483647,
    padding: '10px 16px', border: '0', borderRadius: '10px', cursor: 'pointer',
    background: '#4f46e5', color: '#fff', fontSize: '14px', fontWeight: '600',
    boxShadow: '0 6px 20px rgba(79,70,229,.4)', fontFamily: 'system-ui, sans-serif',
  });
  const reset = () => { btn.disabled = false; btn.style.background = '#4f46e5'; btn.textContent = '采集到工作台'; };
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '采集中…';
    chrome.runtime.sendMessage({ type: 'collect-page' }, resp => {
      const ok = resp && resp.ok;
      btn.style.background = ok ? '#10b981' : '#ef4444';
      btn.textContent = (resp && resp.message) || '发送失败，请确认 AI 套图工作台已启动';
      setTimeout(reset, 6000);
    });
  });
  document.body.appendChild(btn);
  const PRODUCT = /detail\.1688\.com\/offer\/\d|yangkeduo\.com\/goods|pinduoduo\.com\/.*goods_id=|ozon\.\w+\/product\/|wildberries\.\w+\/catalog\/\d+\//;
  const sync = () => { btn.style.display = PRODUCT.test(location.href) ? '' : 'none'; };
  sync();
  setInterval(sync, 1000);
})();
