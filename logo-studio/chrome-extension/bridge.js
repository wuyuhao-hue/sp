// 运行在本机印标工作台页面上：告诉页面扩展已安装，并在有新任务时唤醒后台去领取。
(() => {
  const version = chrome.runtime.getManifest().version;
  document.documentElement.setAttribute('data-logo-studio-ext', version);
  const kick = () => {
    try { chrome.runtime.sendMessage({ type: 'kick' }).catch(() => {}); } catch (e) { /* 扩展被重新加载过，刷新页面即可 */ }
  };
  kick();
  setInterval(kick, 10000);
  window.addEventListener('message', e => {
    if (e.source === window && e.data && e.data.logoStudio === 'kick') kick();
  });
})();
