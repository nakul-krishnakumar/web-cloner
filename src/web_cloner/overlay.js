// Injected into every top-level page. Talks to Python only through the binding captured below,
// authenticated by a per-session token kept in this closure; page scripts never see it.
(() => {
  if (window.top !== window) return;
  const call = window.__webClonerControl; // grab before page scripts can replace it
  if (typeof call !== 'function') return;
  const TOKEN = __WC_TOKEN__;
  const ID = '__web_cloner_overlay';
  const LABELS = {
    waiting_login: 'Waiting for login', ready: 'Ready to scrape', scraping: 'Scraping',
    paused: 'Paused', finished: 'Finished', error: 'Error',
  };
  let host, root;

  function build() {
    host = document.createElement('div');
    host.id = ID;
    root = host.attachShadow({mode: 'open'});
    root.innerHTML = `
<style>
  :host { all: initial; }
  section { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 300px;
    font: 13px/1.4 system-ui, sans-serif; color: #fff; background: #111827; border: 3px solid #6b7280;
    border-radius: 10px; padding: 10px 12px; box-shadow: 0 6px 24px rgba(0,0,0,.4); }
  :host([data-state=ready]) section { border-color: #22c55e; }
  :host([data-state=scraping]) section { border-color: #ef4444; }
  :host([data-state=paused]) section, :host([data-state=error]) section { border-color: #f59e0b; }
  .title { font-weight: 700; margin-bottom: 4px; }
  .status { font-weight: 600; }
  .detail { color: #d1d5db; margin: 4px 0 8px; word-break: break-all; }
  .row { display: flex; gap: 6px; margin-top: 6px; }
  button, input { font: inherit; border-radius: 6px; padding: 5px 8px; }
  button { flex: 1; cursor: pointer; border: 1px solid #9ca3af; background: #374151; color: #fff; }
  button.primary { background: #16a34a; border-color: #16a34a; font-weight: 700; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button:focus-visible, input:focus-visible { outline: 3px solid #60a5fa; outline-offset: 1px; }
  input { flex: 1.4; min-width: 0; border: 1px solid #9ca3af; background: #1f2937; color: #fff; }
  [hidden] { display: none !important; }
</style>
<section role="region" aria-label="Web Cloner capture controls">
  <div class="title">Web Cloner</div>
  <div class="status" role="status" aria-live="polite">Connecting…</div>
  <div class="detail"></div>
  <div class="row">
    <button type="button" data-wc-action="confirm">I'm signed in</button>
    <button type="button" data-wc-action="start" class="primary" disabled>Start Scraping</button>
  </div>
  <div class="row">
    <input type="text" maxlength="80" placeholder="checkpoint name" aria-label="Checkpoint name">
    <button type="button" data-wc-action="checkpoint" disabled>Checkpoint</button>
  </div>
  <div class="row">
    <button type="button" data-wc-action="stop" disabled>Stop Scraping</button>
    <button type="button" data-wc-action="finish">Finish &amp; Save</button>
  </div>
</section>`;
    // Keep overlay events away from the site's own handlers (forms, shortcuts, click-outside logic).
    for (const t of ['click', 'dblclick', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'keydown', 'keyup',
                     'keypress', 'input', 'change', 'submit', 'focusin', 'focusout'])
      host.addEventListener(t, e => e.stopPropagation());
    root.addEventListener('click', async e => {
      const b = e.target.closest && e.target.closest('button[data-wc-action]');
      if (!b || b.disabled || !e.isTrusted) return; // page scripts can't drive us with synthetic clicks
      e.preventDefault();
      const input = root.querySelector('input');
      const action = b.dataset.wcAction;
      b.disabled = true;
      try {
        render(await call(TOKEN, action, action === 'checkpoint' ? input.value : null));
        if (action === 'checkpoint') input.value = '';
      } catch (err) { /* page navigated or browser closing */ }
    });
  }

  const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

  function render(s) {
    if (!s || !s.state) return;
    host.dataset.state = s.state;
    const q = sel => root.querySelector(sel);
    q('.status').textContent = LABELS[s.state] || s.state;
    const c = s.counts;
    q('.detail').textContent = [
      s.message,
      c && `${fmt(s.elapsed)} · ${c.pages} pages · ${c.requests} requests · ${c.assets} assets · ${c.errors} errors`,
      s.output && `Output: ${s.output}`,
    ].filter(Boolean).join('\n');
    q('.detail').style.whiteSpace = 'pre-line';
    const en = {
      confirm: s.state === 'waiting_login' || (s.state === 'paused' && s.reason === 'auth'),
      start: s.state === 'ready',
      checkpoint: s.state === 'scraping',
      stop: s.state === 'scraping' || s.state === 'paused',
      finish: s.state !== 'finished',
    };
    for (const [a, on] of Object.entries(en)) q(`[data-wc-action=${a}]`).disabled = !on;
    q('[data-wc-action=confirm]').hidden = !en.confirm;
  }

  async function tick() {
    if (!document.documentElement) return;
    if (!host) build();
    if (!host.isConnected) document.documentElement.appendChild(host);
    try { render(await call(TOKEN, 'state', null)); } catch (err) { /* browser closing */ }
  }
  setInterval(tick, 1000);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', tick); else tick();
})();
