(() => {
  const request = __FOCUS_CONFIG__;
  const key = `company-${request.token}`;
  const selector = `.st-key-cmp-card-${request.index}`;
  // Streamlit scrolls stMain, not window. Wait for the rerun's layout to settle
  // before measuring, including font/expander sizing and the sticky composer.
  const state = window.__researchCompanyFocus ||= { completed: new Set() };
  if (state.completed.has(key)) return;
  state.cancel?.();
  let stopped = false;
  let timer;
  let previous = null;
  let stable = 0;
  let clearHighlight = () => {};
  const started = performance.now();
  const cancel = () => { stopped = true; clearTimeout(timer); clearHighlight(); };
  state.cancel = cancel;
  const visible = element => element && element.getClientRects().length > 0;
  function check() {
    if (stopped) return;
    const card = document.querySelector(selector);
    const header = card?.querySelector('summary');
    // Streamlit 1.57 uses stAppScrollToBottomContainer in place of stMain;
    // the public stMain class remains on the actual scrollable section.
    const main = document.querySelector('.stMain, [data-testid="stMain"]');
    if (!visible(header) || !main) {
      if (performance.now() - started < 8000) timer = setTimeout(check, 60);
      return;
    }
    const rect = header.getBoundingClientRect();
    const geometry = [rect.top, rect.height, main.scrollHeight, main.clientHeight];
    stable = previous && geometry.every((n, i) => Math.abs(n - previous[i]) < 1) ? stable + 1 : 0;
    previous = geometry;
    if (stable < 3 && performance.now() - started < 8000) {
      timer = setTimeout(check, 60);
      return;
    }
    state.completed.add(key);
    // Keep this bounded during a long-lived Streamlit session.
    if (state.completed.size > 50) state.completed.delete(state.completed.values().next().value);
    const bounds = main.getBoundingClientRect();
    const topBar = document.querySelector('[data-testid="stHeader"]');
    const composer = document.querySelector('[data-testid="stBottom"]');
    const top = Math.max(0, bounds.top, visible(topBar) ? topBar.getBoundingClientRect().bottom : 0) + 20;
    const bottom = Math.min(window.innerHeight, bounds.bottom,
      visible(composer) ? composer.getBoundingClientRect().top : window.innerHeight) - 20;
    if (rect.top < top || rect.bottom > bottom) {
      const targetTop = top + Math.max(0, (bottom - top) / 3);
      main.scrollTo({ top: main.scrollTop + rect.top - targetTop,
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    }
    card.classList.add('wb-company-added');
    const badge = document.createElement('span');
    badge.className = 'wb-added-badge';
    badge.textContent = '已添加';
    badge.setAttribute('role', 'status');
    badge.setAttribute('aria-label', `已添加公司 ${String.fromCharCode(65 + request.index)}`);
    header.append(badge);
    clearHighlight = () => { card.classList.remove('wb-company-added'); badge.remove(); };
    timer = setTimeout(clearHighlight, 3000);
  }
  check();
})();
