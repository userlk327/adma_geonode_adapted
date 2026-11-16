/* Ask ADMA chat panel (included by templates/filemanager/_askadma_widget.html) */
(function () {
  const $ = (id) => document.getElementById(id);
  const fab = $('askadma-fab'), panel = $('askadma-panel'), msgs = $('askadma-messages');
  (function () { const f = document.querySelector('footer.footer'); if (!f) return; const mq = window.matchMedia('(max-width: 767.98px)'); const fit = () => { if (mq.matches) document.documentElement.style.removeProperty('--askadma-gap'); else document.documentElement.style.setProperty('--askadma-gap', (f.offsetHeight + 20) + 'px'); }; fit(); window.addEventListener('resize', fit); })();
  const form = $('askadma-inputbar'), input = $('askadma-text'), send = $('askadma-send');
  const viewer = $('askadma-viewer');
  const STORE = 'askadma_conversation', OPEN = 'askadma_open', WIDTH = 'askadma_width', PENDING = 'askadma_pending';
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const SVGNS = 'http://www.w3.org/2000/svg';

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    sget(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    sset(k, v) { try { if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) {} }
  };
  let conversation = [];
  try { conversation = JSON.parse(store.get(STORE, '[]')) || []; } catch (e) { conversation = []; }
  function save() { store.set(STORE, JSON.stringify(conversation.slice(-30))); }
  function getCookie(name) {
    const v = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return v ? v.pop() : '';
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function icon(name) { const i = el('i', 'fas ' + (name || 'fa-arrow-right')); i.setAttribute('aria-hidden', 'true'); return i; }

  // ---------- panel: open, close, resize ----------
  function setWidth(w) {
    const max = Math.min(760, Math.round(window.innerWidth * 0.6));
    w = Math.max(320, Math.min(max, Math.round(w)));
    document.documentElement.style.setProperty('--askadma-w', w + 'px');
    return w;
  }
  setWidth(parseInt(store.get(WIDTH, '420'), 10) || 420);
  function openPanel(focus) {
    panel.hidden = false; fab.hidden = true;
    document.documentElement.classList.add('askadma-docked');
    store.set(OPEN, '1');
    msgs.scrollTop = msgs.scrollHeight;
    if (focus) input.focus();
    setTimeout(refreshSwanBar, 0);
  }
  function closePanel() {
    panel.hidden = true; fab.hidden = false;
    document.documentElement.classList.remove('askadma-docked');
    store.set(OPEN, '0');
    fab.focus();
  }
  fab.addEventListener('click', () => openPanel(true));
  $('askadma-close').addEventListener('click', closePanel);
  $('askadma-clear').addEventListener('click', () => { conversation = []; save(); renderAll(); input.focus(); });
  $('askadma-runs').addEventListener('click', () => runAction({ type: 'show_runs' }, true));
  setInterval(() => { if (!panel.hidden && !document.hidden) refreshSwanBar(); }, 60000);
  (function resizer() {
    const handle = $('askadma-resize');
    let dragging = false;
    handle.addEventListener('pointerdown', (e) => { dragging = true; handle.classList.add('dragging'); handle.setPointerCapture(e.pointerId); e.preventDefault(); });
    handle.addEventListener('pointermove', (e) => { if (dragging) setWidth(window.innerWidth - e.clientX); });
    handle.addEventListener('pointerup', (e) => {
      if (!dragging) return;
      dragging = false; handle.classList.remove('dragging');
      store.set(WIDTH, String(setWidth(window.innerWidth - e.clientX)));
    });
  })();

  // ---------- text ----------
  function renderText(target, text) {
    // Plain text with **bold**, `code`, [links](/relative or https://...) and "- " bullets; no raw HTML.
    const lines = String(text || '').split('\n');
    lines.forEach((line, n) => {
      if (n) target.appendChild(document.createTextNode('\n'));
      line = line.replace(/^#{1,6}\s+(.*)$/, '**$1**').replace(/^\s*[-*]\s+/, '• ');
      const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(((?:https?:\/\/|\/)[^)\s]+)\))/g;
      let last = 0, m;
      while ((m = re.exec(line))) {
        if (m.index > last) target.appendChild(document.createTextNode(line.slice(last, m.index)));
        if (m[2]) target.appendChild(el('strong', '', m[2]));
        else if (m[3]) target.appendChild(el('code', '', m[3]));
        else { const a = el('a', '', m[4]); a.href = m[5]; if (/^https?:/.test(m[5])) { a.target = '_blank'; a.rel = 'noopener'; } target.appendChild(a); }
        last = re.lastIndex;
      }
      if (last < line.length) target.appendChild(document.createTextNode(line.slice(last)));
    });
  }

  // ---------- charts, galleries, photos ----------
  function addChart(parent, action) {
    if (typeof Chart === 'undefined') { parent.appendChild(el('div', 'askadma-msg note', 'Charts need the Chart.js script, which did not load.')); return; }
    const wrap = el('div', 'askadma-chart');
    const canvas = document.createElement('canvas');
    wrap.appendChild(canvas); parent.appendChild(wrap);
    const kind = ['pie', 'line', 'scatter'].includes(action.chart_type) ? action.chart_type : 'bar';
    const colors = ['#d00000', '#1f77b4', '#2ca02c', '#ff7f0e', '#9467bd', '#8c564b'];
    const series = (action.datasets && action.datasets.length) ? action.datasets : [{ label: action.y_label || '', data: action.values || [] }];
    const datasets = series.map((s, i) => {
      if (kind === 'pie') return { label: s.label, data: s.data };
      const c = colors[i % colors.length];
      return { label: s.label, data: s.data, borderColor: c, borderWidth: kind === 'bar' ? 1 : 2,
               backgroundColor: kind === 'bar' ? c + '99' : c, fill: false, spanGaps: true,
               pointRadius: (kind === 'line' && s.data.length > 60) ? 0 : 3 };
    });
    new Chart(canvas, {
      type: kind,
      data: kind === 'scatter' ? { datasets } : { labels: action.labels || [], datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : undefined,
        plugins: { title: { display: !!action.title, text: action.title }, legend: { display: kind === 'pie' || datasets.length > 1 } },
        scales: kind === 'pie' ? {} : { x: { title: { display: !!action.x_label, text: action.x_label } },
                                        y: { title: { display: !!action.y_label, text: action.y_label } } }
      }
    });
  }

  function photoSvg(src, width, height, boxes, compact) {
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img');
    const img = document.createElementNS(SVGNS, 'image');
    img.setAttribute('href', src); img.setAttribute('width', width); img.setAttribute('height', height);
    img.setAttribute('preserveAspectRatio', 'none');
    svg.appendChild(img);
    const stroke = Math.max(2, width / 300), fs = Math.max(12, width / 55);
    (boxes || []).forEach((b) => {
      const [x1, y1, x2, y2] = b.box;
      const r = document.createElementNS(SVGNS, 'rect');
      r.setAttribute('x', x1); r.setAttribute('y', y1); r.setAttribute('width', x2 - x1); r.setAttribute('height', y2 - y1);
      r.setAttribute('fill', 'none'); r.setAttribute('stroke', '#ffb703'); r.setAttribute('stroke-width', stroke);
      svg.appendChild(r);
      if (compact) return;
      const text = `${b.label} ${Math.round((b.confidence || 0) * 100)}%`;
      const tw = text.length * fs * 0.58 + fs * 0.6, th = fs * 1.35;
      const ty = y1 - th >= 0 ? y1 - th : y1;
      const bg = document.createElementNS(SVGNS, 'rect');
      bg.setAttribute('x', x1); bg.setAttribute('y', ty); bg.setAttribute('width', tw); bg.setAttribute('height', th); bg.setAttribute('fill', '#ffb703');
      const t = document.createElementNS(SVGNS, 'text');
      t.setAttribute('x', x1 + fs * 0.3); t.setAttribute('y', ty + fs * 1.02); t.setAttribute('font-size', fs);
      t.setAttribute('font-family', 'system-ui, sans-serif'); t.setAttribute('font-weight', '600'); t.setAttribute('fill', '#1f2622');
      t.textContent = text;
      svg.appendChild(bg); svg.appendChild(t);
    });
    const counts = {};
    (boxes || []).forEach((b) => { counts[b.label] = (counts[b.label] || 0) + 1; });
    const summary = Object.keys(counts).map((k) => `${counts[k]} ${k}`).join(', ');
    svg.setAttribute('aria-label', summary ? `Photo with ${summary}` : 'Photo');
    return svg;
  }

  function addPhoto(parent, action) {
    const b = el('button', 'askadma-photo'); b.type = 'button'; b.title = 'Open full size';
    const w = action.width || 1600, h = action.height || 1200;
    b.appendChild(photoSvg(action.image, w, h, action.boxes, true));
    b.addEventListener('click', () => openViewer([action], 0));
    parent.appendChild(b);
  }

  function addGallery(parent, action) {
    const items = action.items || [];
    const box = el('div', 'askadma-gallery');
    box.appendChild(el('div', 'askadma-gallery-title', action.title || `${items.length} photos`));
    const grid = el('div', 'askadma-grid');
    const SHOW = 12;
    items.forEach((it, i) => {
      const t = el('button', 'askadma-tile'); t.type = 'button'; t.title = it.name;
      if (i >= SHOW) t.hidden = true;
      const img = document.createElement('img'); img.loading = 'lazy'; img.alt = it.name; img.src = it.thumb;
      t.appendChild(img);
      t.appendChild(el('div', 'askadma-cap', it.caption || it.name));
      t.addEventListener('click', () => openViewer(items.map(galleryToPhoto), i));
      grid.appendChild(t);
    });
    box.appendChild(grid);
    if (items.length > SHOW) {
      const more = el('button', 'askadma-btn askadma-more', `Show all ${items.length}`); more.type = 'button';
      more.addEventListener('click', () => { grid.querySelectorAll('.askadma-tile').forEach((t) => { t.hidden = false; }); more.remove(); });
      box.appendChild(more);
    }
    parent.appendChild(box);
  }
  function galleryToPhoto(it) {
    const p = { type: 'show_detections', file_id: it.id, name: it.name, url: it.url, image: it.image, boxes: it.boxes || [], caption: it.caption };
    if (it.size) { p.width = it.size[0]; p.height = it.size[1]; }
    return p;
  }

  // ---------- full-screen photo viewer ----------
  let vList = [], vIndex = 0, vReturnFocus = null;
  const vImg = viewer.querySelector('.askadma-vimg'), vPrev = viewer.querySelector('.askadma-prev'), vNext = viewer.querySelector('.askadma-next');
  function showViewerPhoto() {
    const p = vList[vIndex];
    viewer.querySelector('.askadma-vname').textContent = p.name || '';
    const counts = {};
    (p.boxes || []).forEach((b) => { counts[b.label] = (counts[b.label] || 0) + 1; });
    const info = Object.keys(counts).map((k) => `${counts[k]} ${k}`).join(', ');
    viewer.querySelector('.askadma-vinfo').textContent = [info, vList.length > 1 ? `${vIndex + 1} of ${vList.length}` : ''].filter(Boolean).join('  |  ');
    const open = viewer.querySelector('.askadma-vopen'); open.href = p.url || '#'; open.hidden = !p.url;
    vPrev.hidden = vIndex <= 0; vNext.hidden = vIndex >= vList.length - 1;
    vImg.textContent = '';
    const draw = (w, h) => { vImg.textContent = ''; vImg.appendChild(photoSvg(p.image, w, h, p.boxes, false)); };
    if (p.width && p.height) draw(p.width, p.height);
    else { const probe = new Image(); probe.onload = () => draw(probe.naturalWidth, probe.naturalHeight); probe.onerror = () => { vImg.textContent = 'Could not load this photo.'; }; probe.src = p.image; }
  }
  function openViewer(list, index) {
    vList = list; vIndex = index || 0; vReturnFocus = document.activeElement;
    viewer.hidden = false; showViewerPhoto();
    viewer.querySelector('.askadma-vclose').focus();
  }
  function closeViewer() { viewer.hidden = true; vImg.textContent = ''; if (vReturnFocus && vReturnFocus.focus) vReturnFocus.focus(); }
  viewer.querySelector('.askadma-vclose').addEventListener('click', closeViewer);
  vPrev.addEventListener('click', () => { if (vIndex > 0) { vIndex--; showViewerPhoto(); } });
  vNext.addEventListener('click', () => { if (vIndex < vList.length - 1) { vIndex++; showViewerPhoto(); } });
  viewer.addEventListener('click', (e) => { if (e.target === viewer || e.target.classList.contains('askadma-vstage')) closeViewer(); });
  document.addEventListener('keydown', (e) => {
    if (viewer.hidden) return;
    if (e.key === 'Escape') closeViewer();
    else if (e.key === 'ArrowLeft') vPrev.click();
    else if (e.key === 'ArrowRight') vNext.click();
  });

  // ---------- page control ----------
  function samePage(url) {
    const u = new URL(url, location.href);
    return u.pathname === location.pathname && (u.search || '') === (location.search || '');
  }
  function highlight(id) {
    const all = Array.from(document.querySelectorAll(`[data-file-id="${CSS.escape(id)}"], [data-folder-id="${CSS.escape(id)}"]`));
    const target = all.find((n) => n.offsetParent !== null) || all[0];
    if (!target) { addNote('I could not find that item on this page.'); return false; }
    target.classList.add('askadma-hl');
    target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
    setTimeout(() => target.classList.remove('askadma-hl'), 8000);
    return true;
  }
  // ---------- ADMA Studio: script runs and saved reports ----------
  async function studioRequest(url, method, body) {
    let data = {};
    try {
      const r = await fetch(url, { method, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: body ? JSON.stringify(body) : undefined });
      try { data = await r.json(); } catch (e) { data = {}; }
      if (!r.ok && !data.status) data = Object.assign({}, data, { error: data.error || `the server answered ${r.status}` });
    } catch (e) { data = { error: (e && e.message) || String(e) }; }
    return data;
  }
  function smallButton(label, iconName, cls, onClick) {
    const b = el('button', 'askadma-btn' + (cls ? ' ' + cls : '')); b.type = 'button';
    b.appendChild(icon(iconName)); b.appendChild(document.createTextNode(label));
    b.addEventListener('click', onClick);
    return b;
  }
  function studioBadge(size) {
    const b = el('span', 'adma-studio-badge adma-studio-badge-' + (size || 'md')); b.setAttribute('aria-hidden', 'true');
    b.appendChild(icon('fa-seedling'));
    return b;
  }
  // Studio folder cards on folder pages: outline the card around a Studio folder icon
  function markStudioCards() {
    document.querySelectorAll('.adma-studio-folder').forEach((w) => {
      let n = w.parentElement;
      for (let i = 0; n && i < 5; i++, n = n.parentElement) {
        const cs = getComputedStyle(n);
        if ((parseFloat(cs.borderTopWidth) > 0 || cs.boxShadow !== 'none') && n.getBoundingClientRect().width < 700) {
          n.classList.add('adma-studio-card'); break;
        }
      }
    });
  }
  function humanSize(n) {
    n = Number(n) || 0;
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + ' ' + u[i];
  }
  // ---------- HCC Swan: connect with password + Duo ----------
  let swanState = null;
  async function swanStatus() { swanState = await studioRequest('/api/askadma/swan/status/', 'GET'); return swanState; }
  async function refreshSwanBar() {
    const bar = $('askadma-swanbar');
    if (!bar) return;
    const st = await swanStatus();
    try { window.dispatchEvent(new CustomEvent('askadma:swan', { detail: st })); } catch (e) {}
    bar.textContent = '';
    if (!st || !st.available || (!st.connected && (!st.state || st.state === 'none'))) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.append(el('span', 'askadma-dot' + (st.connected ? ' on' : '')),
               el('span', '', st.connected ? `Swan: connected as ${st.hcc_user} until ${st.until}` : 'Swan: not connected'));
    const b = el('button', 'askadma-linkbtn', st.connected ? 'Disconnect' : 'Connect'); b.type = 'button';
    b.addEventListener('click', async () => {
      if (st.connected) { b.disabled = true; await studioRequest('/api/askadma/swan/disconnect/', 'POST', {}); refreshSwanBar(); }
      else openSwanConnect();
    });
    bar.appendChild(b);
  }
  function openSwanConnect(onDone) {
    let box = $('askadma-swan');
    if (!box) { box = el('div'); box.id = 'askadma-swan'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
      box.setAttribute('aria-label', 'Connect to HCC Swan'); document.body.appendChild(box); }
    const prev = document.activeElement;
    let polling = null;
    function close(ok) { clearTimeout(polling); box.hidden = true; box.textContent = ''; if (prev && prev.focus) prev.focus(); refreshSwanBar(); if (onDone) onDone(ok); }
    function view(nodes) { box.textContent = ''; const d = el('div', 'askadma-sbox'); nodes.forEach((n) => d.appendChild(n)); box.appendChild(d); box.hidden = false; return d; }
    function title(text, iconName) { const h = el('h3'); h.append(icon(iconName), document.createTextNode(text)); return h; }
    function form(errorText) {
      const user = el('input'); user.type = 'text'; user.autocomplete = 'username'; user.value = store.get('askadma_hcc_user', '');
      const pass = el('input'); pass.type = 'password'; pass.autocomplete = 'current-password';
      const hours = el('select'); [[8, '8 hours (a workday)'], [4, '4 hours'], [2, '2 hours'], [12, '12 hours']].forEach(([v, t]) => {
        const o = el('option', '', t); o.value = v; hours.appendChild(o); });
      const useCode = el('input'); useCode.type = 'checkbox';
      const code = el('input'); code.type = 'text'; code.inputMode = 'numeric'; code.autocomplete = 'one-time-code'; code.placeholder = '123456';
      const codeLabel = el('label'); codeLabel.append(document.createTextNode('Duo passcode'), code); codeLabel.hidden = true;
      useCode.addEventListener('change', () => { codeLabel.hidden = !useCode.checked; });
      const lab = (t, input) => { const l = el('label'); l.append(document.createTextNode(t), input); return l; };
      const checkLab = el('label', 'askadma-check'); checkLab.append(useCode, document.createTextNode('Use a Duo passcode instead of a push'));
      const err = el('p', 'askadma-serr', errorText || '');
      const go = smallButton('Connect', 'fa-plug', 'primary', submit);
      const btns = el('div', 'askadma-sbtns'); btns.append(smallButton('Cancel', 'fa-times', '', () => close(false)), go);
      const f = el('form'); f.style.display = 'contents';
      f.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
      view([title('Connect to HCC Swan', 'fa-bolt'),
            el('p', 'askadma-snote', 'Sign in with your HCC account. ADMA uses your password only to log in to Swan and never stores it. Your jobs run under your own account.'),
            lab('HCC username', user), lab('Password', pass), lab('Stay connected for', hours), checkLab, codeLabel, err, btns]);
      (user.value ? pass : user).focus();
      async function submit() {
        if (!user.value.trim() || !pass.value) { err.textContent = 'Enter your HCC username and password.'; return; }
        go.disabled = true; store.set('askadma_hcc_user', user.value.trim());
        const data = await studioRequest('/api/askadma/swan/connect/', 'POST',
          { username: user.value.trim(), password: pass.value, passcode: useCode.checked ? code.value : '', hours: Number(hours.value) });
        pass.value = '';
        if (data.error) { form(data.error); return; }
        waiting(data);
      }
    }
    function waiting(s) {
      const msg = el('div', 'askadma-pt', s.message || 'Contacting Swan…'); msg.setAttribute('role', 'status');
      const duo = el('div', 'askadma-duo'); duo.append(icon('fa-mobile-alt'), msg,
        el('p', 'askadma-snote', 'Keep this open. It continues on its own once Duo approves the login.'));
      view([title('Connect to HCC Swan', 'fa-bolt'), duo, (() => { const b = el('div', 'askadma-sbtns'); b.append(smallButton('Cancel', 'fa-times', '', () => close(false))); return b; })()]);
      (async function poll() {
        const st = await swanStatus();
        if (box.hidden) return;
        if (st.connected) { connected(st); return; }
        if (st.state === 'failed' || st.error) { form(st.message || st.error || 'Could not connect.'); return; }
        msg.textContent = st.message || 'Contacting Swan…';
        polling = setTimeout(poll, 1500);
      })();
    }
    function connected(st) {
      const ok = el('div', 'askadma-duo'); const i = icon('fa-check-circle'); ok.append(i, el('div', 'askadma-pt', 'Connected to Swan'),
        el('p', 'askadma-snote', `Signed in as ${st.hcc_user} until ${st.until}. ADMA uses this connection only for the runs you start.`));
      const done = smallButton('Done', 'fa-check', 'primary', () => close(true));
      view([title('Connect to HCC Swan', 'fa-bolt'), ok, (() => { const b = el('div', 'askadma-sbtns'); b.append(done); return b; })()]);
      done.focus();
    }
    box.onkeydown = (e) => { if (e.key === 'Escape') close(false); };
    form('');
  }
  // ---------- Globus: sign-in window and copy card ----------
  function openGlobusConnect(onDone) {
    let box = $('askadma-swan');
    if (!box) { box = el('div'); box.id = 'askadma-swan'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
      document.body.appendChild(box); }
    box.setAttribute('aria-label', 'Connect Globus');
    const prev = document.activeElement;
    function close(ok) {
      box.hidden = true; box.textContent = ''; if (prev && prev.focus) prev.focus();
      try { window.dispatchEvent(new CustomEvent('askadma:globus', { detail: { connected: !!ok } })); } catch (e) {}
      if (onDone) onDone(ok);
    }
    function view(nodes) { box.textContent = ''; const d = el('div', 'askadma-sbox'); nodes.forEach((n) => d.appendChild(n)); box.appendChild(d); box.hidden = false; return d; }
    function title() { const h = el('h3'); h.append(icon('fa-exchange-alt'), document.createTextNode('Connect Globus')); return h; }
    function row(buttons) { const b = el('div', 'askadma-sbtns'); buttons.forEach((x) => b.appendChild(x)); return b; }
    box.onkeydown = (e) => { if (e.key === 'Escape') close(false); };
    view([title(), el('p', 'askadma-snote', 'Getting the Globus sign-in ready…')]);
    (async function () {
      const start = await studioRequest('/api/askadma/globus/login/', 'POST', {});
      if (start.error) { const c = smallButton('Close', 'fa-times', '', () => close(false)); view([title(), el('p', 'askadma-serr', start.error), row([c])]); c.focus(); return; }
      form(start);
    })();
    function form(start) {
      const code = el('input'); code.type = 'text'; code.autocomplete = 'off'; code.spellcheck = false; code.placeholder = 'Paste the code here';
      const open = smallButton('Open Globus sign-in', 'fa-external-link-alt', 'primary', () => { window.open(start.auth_url, '_blank', 'noopener'); code.focus(); });
      const err = el('p', 'askadma-serr', '');
      const go = smallButton('Connect', 'fa-plug', 'primary', submit);
      const steps = el('ol', 'askadma-gsteps');
      ['Click Open Globus sign-in. It opens in a new tab.',
       'Sign in with your university account, and with HCC too if Globus asks.',
       'Globus then shows a code. Copy it, come back here and paste it below.'].forEach((t) => steps.appendChild(el('li', '', t)));
      const lab = el('label'); lab.append(document.createTextNode('Code from Globus'), code);
      const nodes = [title()];
      if (start.reason) nodes.push(el('p', 'askadma-snote', start.reason));
      nodes.push(el('p', 'askadma-snote', 'ADMA never sees your password. Copies run under your own Globus account, and you can disconnect any time on the Connectors page.'),
                 steps, open, lab, err, row([smallButton('Cancel', 'fa-times', '', () => close(false)), go]));
      view(nodes);
      open.focus();
      code.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      async function submit() {
        if (!code.value.trim()) { err.textContent = 'Paste the code Globus shows after you sign in.'; return; }
        go.disabled = true;
        const res = await studioRequest('/api/askadma/globus/code/', 'POST', { code: code.value.trim() });
        if (res.error) { go.disabled = false; err.textContent = res.error; return; }
        const ok = el('div', 'askadma-duo');
        ok.append(icon('fa-check-circle'), el('div', 'askadma-pt', 'Connected to Globus'),
                  el('p', 'askadma-snote', res.identity ? `Signed in as ${res.identity}.` : 'You are signed in.'));
        const done = smallButton('Done', 'fa-check', 'primary', () => close(true));
        view([title(), ok, row([done])]); done.focus();
      }
    }
  }
  const GLOBUS_ACTIVE = ['active', 'inactive'];
  const GLOBUS_LABELS = { active: ['Copying', 'busy'], inactive: ['Paused', 'busy'], succeeded: ['Done', 'ok'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', ''] };
  function addGlobusCard(parent, a) {
    const card = el('div', 'askadma-run2');
    parent.appendChild(card);
    function header(pillText, pillCls) {
      const head = el('div', 'askadma-rhead');
      head.append(icon('fa-exchange-alt'), el('h4', '', a.label || 'Copy with Globus'));
      if (pillText) head.appendChild(el('span', 'askadma-pill' + (pillCls ? ' ' + pillCls : ''), pillText));
      return head;
    }
    function route() {
      const dl = el('dl', 'askadma-gkv');
      [['From', `${a.source_name} · ${a.source_path}`], ['To', `${a.destination_name} · ${a.destination_path}`]].forEach(([k, v]) => {
        dl.append(el('dt', '', k), el('dd', '', v)); });
      return dl;
    }
    function proposal(errorText, needsLogin) {
      card.textContent = '';
      card.append(header('Waiting for you', ''), route());
      const changed = el('input'); changed.type = 'checkbox'; changed.checked = a.sync !== 'all';
      const lab = el('label', 'askadma-pd'); lab.style.cssText = 'display:flex;gap:6px;align-items:center';
      lab.append(changed, document.createTextNode('Only copy new or changed files'));
      card.append(lab, el('div', 'askadma-pd', 'Nothing is deleted on either side.' + (a.checksum_note ? ' ' + a.checksum_note : '')));
      const msg = el('div', 'askadma-pd'); msg.setAttribute('role', 'status');
      if (errorText) { msg.textContent = errorText; msg.style.color = 'var(--askadma-red-dark)'; }
      card.appendChild(msg);
      const btns = el('div', 'askadma-buttons');
      if (needsLogin) btns.appendChild(smallButton('Connect Globus', 'fa-plug', 'primary', () => openGlobusConnect((ok) => { if (ok) proposal(''); })));
      const startBtn = smallButton('Start copy', 'fa-play', needsLogin ? '' : 'primary', async () => {
        startBtn.disabled = true; msg.style.color = ''; msg.textContent = 'Starting the copy…';
        a.sync = changed.checked ? 'mtime' : 'all';
        const res = await studioRequest('/api/askadma/globus/copy/', 'POST', { source: a.source, source_path: a.source_path,
          destination: a.destination, destination_path: a.destination_path, label: a.label, sync: a.sync, is_folder: a.is_folder });
        if (res.error) { proposal(res.error, !!res.needs_login); return; }
        a.task_id = res.task_id; save(); follow(res);
      });
      const cancel = smallButton('Cancel', 'fa-times', '', () => { a.dismissed = true; save(); dismissed(); });
      btns.append(startBtn, cancel);
      card.appendChild(btns);
    }
    function dismissed() {
      card.textContent = '';
      card.append(header('Not started', ''), route(), el('div', 'askadma-pd', 'You cancelled this copy. Nothing was copied.'));
    }
    let timer = null;
    function follow(st) {
      clearTimeout(timer);
      if (!document.body.contains(card)) return;
      card.textContent = '';
      if (st && st.error) {
        card.append(header('Unknown', ''), route(), el('div', 'askadma-pd', st.error));
        const btns = el('div', 'askadma-buttons');
        if (st.needs_login) btns.appendChild(smallButton('Connect Globus', 'fa-plug', 'primary', () => openGlobusConnect((ok) => { if (ok) poll(); })));
        btns.appendChild(smallButton('Check again', 'fa-sync-alt', '', poll));
        card.appendChild(btns);
        return;
      }
      const [label, cls] = st.warning ? ['Retrying', 'busy'] : (GLOBUS_LABELS[st.status] || [st.status || 'Starting', 'busy']);
      card.append(header(label, cls), route());
      const bar = el('div', 'askadma-gbar'); const fill = el('span'); fill.style.width = (st.percent || 0) + '%'; bar.appendChild(fill);
      bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-valuenow', st.percent || 0); bar.setAttribute('aria-valuemin', 0); bar.setAttribute('aria-valuemax', 100);
      const sum = el('div', 'askadma-pd', st.summary || ''); sum.setAttribute('role', 'status');
      card.append(bar, sum);
      const btns = el('div', 'askadma-buttons');
      if (GLOBUS_ACTIVE.includes(st.status)) {
        const stop = smallButton('Cancel copy', 'fa-stop', '', async () => {
          stop.disabled = true;
          const res = await studioRequest('/api/askadma/globus/task/' + encodeURIComponent(a.task_id) + '/', 'POST', { action: 'cancel' });
          follow(res.error ? Object.assign({}, st, { summary: res.error }) : res);
        });
        btns.appendChild(stop);
      }
      if (st.globus_url) btns.appendChild(smallButton('Open in Globus', 'fa-external-link-alt', '', () => window.open(st.globus_url, '_blank', 'noopener')));
      card.appendChild(btns);
      if (GLOBUS_ACTIVE.includes(st.status) || !st.status) timer = setTimeout(poll, 5000);
    }
    async function poll() {
      const st = await studioRequest('/api/askadma/globus/task/' + encodeURIComponent(a.task_id) + '/', 'GET');
      follow(st);
    }
    if (a.task_id) { card.append(header('Checking…', 'busy'), route()); poll(); }
    else if (a.dismissed) dismissed();
    else proposal('');
  }
  // ---------- Globus schedule card: proposed by Ask ADMA, saved only by the user's click ----------
  const GDAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
  const gpad = (n) => String(n).padStart(2, '0');
  const gLocalInput = (d) => d.getFullYear() + '-' + gpad(d.getMonth() + 1) + '-' + gpad(d.getDate()) + 'T' + gpad(d.getHours()) + ':' + gpad(d.getMinutes());
  function globusFirstRun(a) {
    const [hh, mm] = String(a.time || '02:00').split(':').map(Number);
    const now = new Date();
    let d;
    if (a.date) {
      const [y, m, dd] = a.date.split('-').map(Number);
      d = new Date(y, m - 1, dd, hh, mm);
    } else {
      d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm);
      const want = GDAYS.indexOf(a.day);
      if (want >= 0) { d.setDate(d.getDate() + (want - d.getDay() + 7) % 7); if (d <= now) d.setDate(d.getDate() + 7); }
      else if (d <= now) d.setDate(d.getDate() + 1);
    }
    return d;
  }
  function globusPlanText(every, d) {
    if (!d || isNaN(d)) return 'Pick the date and time of the first run.';
    const t = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    const date = d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
    const day = d.toLocaleDateString('en-US', { weekday: 'long' });
    if (every === 'once') return 'Once, on ' + date + ' at ' + t;
    if (every === 'day') return 'Every day at ' + t + ', starting ' + date;
    if (every === 'week') return 'Every ' + day + ' at ' + t + ', starting ' + date;
    return 'Every 4 weeks on ' + day + ' at ' + t + ', starting ' + date;
  }
  function addGlobusPlanCard(parent, a) {
    const card = el('div', 'askadma-run2');
    parent.appendChild(card);
    const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || '';
    function header(pillText, pillCls) {
      const head = el('div', 'askadma-rhead');
      head.append(icon('fa-calendar-check'), el('h4', '', a.label || 'Scheduled backup'));
      if (pillText) head.appendChild(el('span', 'askadma-pill' + (pillCls ? ' ' + pillCls : ''), pillText));
      return head;
    }
    function route() {
      const dl = el('dl', 'askadma-gkv');
      [['From', `${a.source_name} · ${a.source_path}`], ['To', `${a.destination_name} · ${a.destination_path}`]].forEach(([k, v]) => {
        dl.append(el('dt', '', k), el('dd', '', v)); });
      return dl;
    }
    function proposal(errorText, needsLogin) {
      card.textContent = '';
      card.append(header('Waiting for you', ''), route());
      const form = el('div', 'askadma-gform');
      const every = el('select');
      [['once', 'Once'], ['day', 'Every day'], ['week', 'Every week'], ['4weeks', 'Every 4 weeks (monthly)']].forEach(([v, t]) => {
        const o = el('option', '', t); o.value = v; every.appendChild(o); });
      every.value = a.every || 'week';
      const first = el('input'); first.type = 'datetime-local';
      first.value = a.first || gLocalInput(globusFirstRun(a));
      const l1 = el('label'); l1.append(document.createTextNode('How often'), every);
      const l2 = el('label'); l2.append(document.createTextNode('First run' + (tz ? ' (' + tz + ')' : '')), first);
      form.append(l1, l2);
      const when = el('div', 'askadma-pd askadma-gwhen'); when.setAttribute('aria-live', 'polite');
      const sync = () => { when.textContent = globusPlanText(every.value, new Date(first.value)); };
      every.addEventListener('change', sync); first.addEventListener('input', sync); sync();
      const changed = el('input'); changed.type = 'checkbox'; changed.checked = a.sync !== 'all';
      const lab = el('label', 'askadma-pd'); lab.style.cssText = 'display:flex;gap:6px;align-items:center';
      lab.append(changed, document.createTextNode('Only copy new or changed files'));
      card.append(form, when, lab, el('div', 'askadma-pd', 'Globus runs it on its own, even when ADMA is off, and emails you after each run. Nothing is deleted on either side.'
        + (a.checksum_note ? ' ' + a.checksum_note : '')));
      const msg = el('div', 'askadma-pd'); msg.setAttribute('role', 'status');
      if (errorText) { msg.textContent = errorText; msg.style.color = 'var(--askadma-red-dark)'; }
      card.appendChild(msg);
      const btns = el('div', 'askadma-buttons');
      const saveBtn = smallButton('Save schedule', 'fa-calendar-check', needsLogin ? '' : 'primary', submit);
      if (needsLogin) btns.appendChild(smallButton('Connect Globus', 'fa-plug', 'primary', () => openGlobusConnect((ok) => { if (ok) submit(); })));
      btns.append(saveBtn, smallButton('Cancel', 'fa-times', '', () => { a.dismissed = true; save(); dismissed(); }));
      card.appendChild(btns);
      async function submit() {
        const d = new Date(first.value);
        if (!first.value || isNaN(d)) { msg.textContent = 'Pick the date and time of the first run.'; return; }
        saveBtn.disabled = true; msg.style.color = ''; msg.textContent = 'Saving the schedule in Globus…';
        a.every = every.value; a.first = first.value; a.sync = changed.checked ? 'mtime' : 'all';
        const res = await studioRequest('/api/askadma/globus/plans/', 'POST', { source: a.source, source_path: a.source_path,
          destination: a.destination, destination_path: a.destination_path, label: a.label, sync: a.sync, is_folder: a.is_folder,
          every: a.every, start: d.toISOString(), tz: tz, when_text: globusPlanText(a.every, d) });
        if (res.error) { proposal(res.error, !!res.needs_login); return; }
        a.plan_id = res.plan_id; a.when_text = res.when_text; save(); saved(res);
      }
    }
    function dismissed() {
      card.textContent = '';
      card.append(header('Not scheduled', ''), route(), el('div', 'askadma-pd', 'You cancelled this. Nothing was scheduled.'));
    }
    function saved(p) {
      card.textContent = '';
      if (!p) { card.append(header('Removed', ''), route(), el('div', 'askadma-pd', 'This schedule was deleted.')); return; }
      card.append(header(p.state_label || 'Scheduled', p.state === 'scheduled' || p.state === 'running' ? 'ok' : 'busy'), route(),
                  el('div', 'askadma-pd askadma-gwhen', p.when_text || a.when_text || ''));
      if (p.next_run) card.appendChild(el('div', 'askadma-pd', 'Next run: ' + new Date(p.next_run * 1000).toLocaleString('en-US',
        { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })));
      if (p.last && p.last.summary) card.appendChild(el('div', 'askadma-pd', 'Last run: ' + p.last.summary));
      const btns = el('div', 'askadma-buttons');
      btns.appendChild(smallButton('Manage on the Connectors page', 'fa-plug', '', () => { window.location.href = '/connectors/'; }));
      card.appendChild(btns);
    }
    async function load() {
      const res = await studioRequest('/api/askadma/globus/plans/', 'GET');
      if (!document.body.contains(card)) return;
      if (res.error) { saved({ state_label: 'Scheduled', when_text: a.when_text }); return; }
      saved((res.plans || []).find((x) => x.plan_id === a.plan_id) || null);
    }
    if (a.plan_id) { card.append(header('Checking…', 'busy'), route()); load(); }
    else if (a.dismissed) dismissed();
    else proposal('');
  }
  // ---------- ADMA Studio / Swan run card ----------
  const RUN_ACTIVE = ['queued', 'uploading', 'pending', 'running', 'fetching', 'importing'];
  const RUN_LABELS = { awaiting_confirmation: ['Waiting for Run', ''], queued: ['Starting', 'busy'], uploading: ['Copying', 'busy'],
    pending: ['Queued', 'busy'], running: ['Running', 'busy'], fetching: ['Saving', 'busy'], importing: ['Saving', 'busy'],
    done: ['Done', 'ok'], failed: ['Failed', 'bad'], cancelled: ['Cancelled', ''], expired: ['Expired', ''] };
  function addRunCard(parent, a) {
    const card = el('div', 'askadma-run2');
    parent.appendChild(card);
    const url = '/api/askadma/studio/run/' + encodeURIComponent(a.run_id) + '/';
    const sw = a.swan || {};
    const items = a.items || [{ path: a.script, meta: 'Your script', kind: 'file', removable: false, size: 0, files: 1 }]
      .concat((a.inputs || []).map((p) => ({ path: p, meta: '', kind: p.endsWith('/') ? 'folder' : 'file', removable: false, size: 0, files: 1 })));
    const choice = { where: a.where === 'swan' && sw.available ? 'swan' : 'adma', gpu: sw.gpu || sw.default_gpu || 'any',
      hours: sw.hours || sw.default_hours || 2, setup: sw.setup || store.get('askadma_swan_setup', ''), removed: new Set(), confirmBig: false,
      picker: { open: false, folder: '', data: null, filter: '', sel: new Map() } };
    const head = el('div', 'askadma-rhead'); const pill = el('span', 'askadma-pill', 'Checking');
    const titleBox = el('div'); titleBox.style.cssText = 'flex:1;min-width:0';
    titleBox.append(el('div', 'askadma-eyebrow', 'ADMA STUDIO'), el('h4', '', 'Run ' + String(a.script || '').split('/').pop()));
    head.append(studioBadge('md'), titleBox, pill);
    const body = el('div'); body.style.cssText = 'display:flex;flex-direction:column;gap:10px';
    card.append(head, body);
    let timer = null, last = null, renderSeq = 0;
    const ACTIVE = RUN_ACTIVE;
    function setPill(text, cls) { pill.textContent = text; pill.className = 'askadma-pill' + (cls ? ' ' + cls : ''); }
    function chosen() { return items.filter((it) => !it.id || !choice.removed.has(it.id) || !it.removable); }
    function totals() { const c = chosen(); return { files: c.reduce((n, it) => n + (it.files || 1), 0), size: c.reduce((n, it) => n + (it.size || 0), 0),
      unknown: c.some((it) => it.kind === 'folder' && it.size == null) }; }
    async function pickerLoad(folder) {
      const pk = choice.picker;
      pk.folder = folder; pk.data = null; pk.filter = '';
      review(last);
      const data = await studioRequest('/api/askadma/studio/browse/?folder=' + encodeURIComponent(folder || ''), 'GET');
      if (pk.folder !== folder) return;
      pk.data = data; review(last);
    }
    function pickerBox() {
      const pk = choice.picker, box = el('div', 'askadma-picker');
      box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'Add data from ADMA');
      const head = el('div', 'askadma-pkhead');
      const where = pk.data && pk.data.folder ? pk.data.folder : { path: 'ADMA', parent_id: null };
      if (pk.folder) head.appendChild(smallButton('Up', 'fa-arrow-up', '', () => pickerLoad(where.parent_id || '')));
      head.appendChild(el('div', 'askadma-pkpath', where.path || 'ADMA'));
      box.appendChild(head);
      const find = el('input'); find.type = 'search'; find.placeholder = 'Filter this folder'; find.value = pk.filter;
      find.setAttribute('aria-label', 'Filter this folder');
      const listEl = el('ul', 'askadma-items askadma-pklist');
      const draw = () => {
        listEl.textContent = '';
        if (!pk.data) { listEl.appendChild(el('li', 'askadma-im', 'Loading…')); return; }
        if (pk.data.error) { listEl.appendChild(el('li', 'askadma-im', pk.data.error)); return; }
        const f = pk.filter.toLowerCase();
        const entries = (pk.data.folders || []).concat(pk.data.files || []).filter((e) => !f || e.name.toLowerCase().includes(f));
        if (!entries.length) listEl.appendChild(el('li', 'askadma-im', 'Nothing here.'));
        entries.forEach((e) => {
          const li = el('li'); const cb = el('input'); cb.type = 'checkbox'; cb.checked = pk.sel.has(e.id);
          cb.setAttribute('aria-label', 'Select ' + e.name);
          cb.addEventListener('change', () => { if (cb.checked) pk.sel.set(e.id, e); else pk.sel.delete(e.id); addBtn.lastChild.textContent = `Add ${pk.sel.size} selected`; addBtn.disabled = !pk.sel.size; });
          const txt = el('div'); txt.style.cssText = 'flex:1;min-width:0';
          if (e.kind === 'folder') { const open = el('button', 'askadma-pkopen', e.name + '/'); open.type = 'button'; open.addEventListener('click', () => pickerLoad(e.id)); txt.appendChild(open); }
          else txt.appendChild(el('div', 'askadma-ip', e.name));
          txt.appendChild(el('div', 'askadma-im', e.meta || ''));
          li.append(cb, icon(e.kind === 'folder' ? 'fa-folder' : 'fa-file'), txt); listEl.appendChild(li);
        });
        if (pk.data.more_files) listEl.appendChild(el('li', 'askadma-im', `…and ${pk.data.more_files} more files. Pick the folder itself, or ask in the chat.`));
      };
      find.addEventListener('input', () => { pk.filter = find.value; draw(); });
      const addBtn = smallButton(`Add ${pk.sel.size} selected`, 'fa-plus', 'primary', () => {
        pk.sel.forEach((e) => {
          choice.removed.delete(e.id);
          if (!items.some((it) => it.id === e.id)) items.push(Object.assign({}, e, { removable: true, added: true }));
        });
        pk.sel.clear(); pk.open = false; choice.confirmBig = false; review(last);
      });
      addBtn.disabled = !pk.sel.size;
      const foot = el('div', 'askadma-buttons');
      foot.append(addBtn, smallButton('Close', 'fa-times', '', () => { pk.open = false; pk.sel.clear(); review(last); }));
      draw(); box.append(find, listEl, foot);
      return box;
    }
    function kv(rows) { const dl = el('dl', 'askadma-kv'); rows.forEach(([k, v]) => { if (v) { dl.appendChild(el('dt', '', k)); dl.appendChild(el('dd', '', v)); } }); return dl; }
    function connLine(st) {
      const line = el('div', 'askadma-conn'); const dot = el('span', 'askadma-dot' + (st && st.connected ? ' on' : ''));
      line.appendChild(dot);
      if (st && st.connected) line.appendChild(el('span', '', `Connected to Swan as ${st.hcc_user} · until ${st.until}`));
      else {
        line.appendChild(el('span', '', st && st.message && st.state !== 'none' ? st.message : 'Not connected to Swan'));
        line.appendChild(smallButton('Connect to Swan', 'fa-plug', 'primary', () => openSwanConnect(() => review(last))));
      }
      return line;
    }
    async function review(s) {
      const mine = ++renderSeq;   // a newer render (or the run starting) wins over this one
      body.textContent = ''; setPill('Ready', '');
      const swan = choice.where === 'swan';
      if (sw.available) {
        body.appendChild(el('div', 'askadma-label', 'Where should this run?'));
        const wrap = el('div', 'askadma-where');
        const opt = (id, name, sub) => { const b = el('button'); b.type = 'button'; b.setAttribute('aria-pressed', String(choice.where === id));
          b.append(el('b', '', name), el('span', '', sub)); b.addEventListener('click', () => { choice.where = id; choice.confirmBig = false; review(last); }); return b; };
        const gpuLabel = ((sw.gpus || []).find((g) => g.id === choice.gpu) || {}).label || 'GPU';
        wrap.append(opt('adma', 'ADMA', '2 CPU · no GPU'), opt('swan', 'HCC Swan GPU', gpuLabel));
        body.appendChild(wrap);
      }
      body.appendChild(el('div', 'askadma-label', swan ? 'What will be sent to Swan' : 'What this run will read'));
      const ul = el('ul', 'askadma-items');
      chosen().forEach((it) => {
        const li = el('li'); li.appendChild(icon(it.kind === 'folder' ? 'fa-folder' : (/\.py$/i.test(it.path) ? 'fa-file-code' : 'fa-file')));
        const txt = el('div'); txt.style.cssText = 'flex:1;min-width:0'; txt.append(el('div', 'askadma-ip', it.path), el('div', 'askadma-im', it.meta || ''));
        li.appendChild(txt);
        if (it.removable && it.id) { const x = el('button'); x.type = 'button'; x.setAttribute('aria-label', 'Remove ' + it.name); x.appendChild(icon('fa-times'));
          x.addEventListener('click', () => { choice.removed.add(it.id); choice.confirmBig = false; review(last); }); li.appendChild(x); }
        ul.appendChild(li);
      });
      if (!choice.picker.open) {
        const addLi = el('li', 'askadma-addrow'); const addData = el('button'); addData.type = 'button';
        addData.append(icon('fa-plus'), document.createTextNode(' Add data'));
        addData.addEventListener('click', () => { choice.picker.open = true; pickerLoad(choice.picker.folder || ''); });
        addLi.appendChild(addData); ul.appendChild(addLi);
      }
      body.appendChild(ul);
      if (choice.picker.open) body.appendChild(pickerBox());
      const t = totals();
      body.appendChild(kv([
        ['Copies to', swan ? `Swan · $WORK/adma/${a.run_id.slice(0, 8)}…/ (deleted after the run)` : 'Nothing is copied; ADMA reads the data where it is.'],
        ['Results go to', a.results_to || a.output], ['Arguments', (a.arguments || []).length && choice.removed.size === 0 ? a.arguments.join(' ') : ''],
        ['Total', `${t.files.toLocaleString()} file${t.files === 1 ? '' : 's'} · ${humanSize(t.size)}${t.unknown ? ' + the added folders (counted when it runs)' : ''}`],
        ['Time limit', swan ? '' : `${a.timeout_minutes} minutes`]]));
      let st = null;
      if (swan) {
        const opts = el('div', 'askadma-opts');
        const gpu = el('select'); (sw.gpus || []).forEach((g) => { const o = el('option', '', g.label); o.value = g.id; gpu.appendChild(o); });
        gpu.value = choice.gpu; gpu.addEventListener('change', () => { choice.gpu = gpu.value; review(last); });
        const hrs = el('select'); (sw.hour_choices || [1, 2, 6, 12, 24, 72, 168]).forEach((h) => {
          const o = el('option', '', h >= 24 ? `${h / 24} day${h === 24 ? '' : 's'}` : `${h} hour${h === 1 ? '' : 's'}`); o.value = h; hrs.appendChild(o); });
        hrs.value = String(choice.hours); hrs.addEventListener('change', () => { choice.hours = Number(hrs.value); });
        const setup = el('textarea'); setup.rows = 2; setup.value = choice.setup; setup.placeholder = 'Optional, e.g.\nmodule load anaconda\nconda activate myenv';
        setup.addEventListener('input', () => { choice.setup = setup.value; });
        const lab = (text, input, wide) => { const l = el('label', wide ? 'askadma-wide' : ''); l.append(document.createTextNode(text), input); return l; };
        opts.append(lab('GPU', gpu), lab('Time limit', hrs), lab('Setup before the script (runs on Swan)', setup, true));
        body.appendChild(opts);
        st = await swanStatus();
        if (mine !== renderSeq || (last && last.status !== 'awaiting_confirmation')) return;
        body.appendChild(connLine(st));
        body.appendChild(el('div', 'askadma-run-note', 'ADMA copies exactly the items above to your Swan $WORK, runs a Slurm job on the gpu partition, then brings the results back. Your originals are not changed.'));
      } else {
        body.appendChild(el('div', 'askadma-run-note', 'It runs in ADMA’s sandbox with no internet. It can read these inputs and saves only to its results folder.'));
      }
      const big = swan && t.size > 10 * 1024 ** 3;
      const actions = el('div', 'askadma-buttons');
      if (big && choice.confirmBig) {
        const w = el('div', 'askadma-warn'); w.setAttribute('role', 'alert');
        w.append(el('b', '', `This is a large copy: ${humanSize(t.size)}`),
                 el('div', '', `Copying it to Swan takes about ${Math.max(1, Math.round(t.size / (50 * 1024 * 1024) / 60))} minutes and uses that much space in your $WORK. Send it?`));
        const b = el('div', 'askadma-buttons');
        b.append(smallButton(`Yes, send ${humanSize(t.size)}`, 'fa-paper-plane', 'primary', () => send('run')),
                 smallButton('Go back', 'fa-arrow-left', '', () => { choice.confirmBig = false; review(last); }));
        w.appendChild(b); body.appendChild(w);
      } else {
        const runBtn = smallButton(swan ? `Send ${humanSize(t.size)} to Swan and run` : 'Run', swan ? 'fa-paper-plane' : 'fa-play', 'primary',
          () => { if (big) { choice.confirmBig = true; review(last); } else send('run'); });
        if (swan && !(st && st.connected)) runBtn.disabled = true;
        actions.append(runBtn, smallButton('Cancel', 'fa-times', '', () => send('cancel')));
        body.appendChild(actions);
      }
      if (review.problem) { body.appendChild(el('div', 'askadma-run-state', review.problem)); review.problem = ''; }
    }
    function step(list, title, detail, state, extra) {
      const li = el('li', state); const dotEl = el('span', 'askadma-pi'); if (state === 'done') dotEl.appendChild(icon('fa-check'));
      if (state === 'stop') dotEl.appendChild(icon('fa-times'));
      const txt = el('div'); txt.style.cssText = 'flex:1;min-width:0'; txt.append(el('div', 'askadma-pt', title));
      if (detail) txt.appendChild(el('div', 'askadma-pd', detail));
      if (extra) txt.appendChild(extra);
      li.append(dotEl, txt); list.appendChild(li);
    }
    function progressSwan(s) {
      const d = s.swan || {}; const st = s.status;
      const order = ['uploading', 'pending', 'running', 'fetching'];
      let at = { queued: 0, uploading: 0, pending: 1, running: 2, fetching: 3, done: 4 }[st];
      let stopAt = -1;
      if (st === 'failed' || st === 'cancelled') {
        // the step where it went wrong gets the red mark; results that came back still count as brought back
        stopAt = d.job_id ? (d.slurm_state && !['PENDING', 'CONFIGURING'].includes(d.slurm_state) ? 2 : 1) : 0;
        at = s.output_url ? 4 : stopAt;
      }
      const ul = el('ol', 'askadma-prog'); ul.setAttribute('aria-label', 'Run progress');
      const state = (i) => (i === stopAt ? 'stop' : i < at ? 'done' : i === at ? 'now' : 'wait');
      const pct = d.bytes_total ? Math.min(100, Math.round(100 * (d.bytes_sent || 0) / d.bytes_total)) : 0;
      let bar = null;
      if (at === 0 && st === 'uploading') { bar = el('div', 'askadma-bar'); const i = el('i'); i.style.width = pct + '%'; bar.appendChild(i); }
      step(ul, 'Copy to Swan', at > 0 ? `${humanSize(d.bytes_total)} copied` : st === 'uploading' ? `${humanSize(d.bytes_sent)} of ${humanSize(d.bytes_total)} · ${pct}%` : `${humanSize(d.bytes_total)} to your $WORK`, state(0), bar);
      step(ul, 'Waiting for a GPU', d.job_id ? (at > 1 ? `Job ${d.job_id} started` : `Job ${d.job_id} queued ${d.reason || ''}`) : `Slurm job on the gpu partition · ${d.gpu || ''}`, state(1));
      let log = null;
      if (s.log_tail && (st === 'running' || stopAt === 2)) { log = el('pre'); log.textContent = s.log_tail; }
      const gpuName = d.gpu_used || d.gpu || 'GPU';
      step(ul, 'Running on the GPU', at > 2 ? (d.ran ? `Ran ${d.ran}${d.gpu_used ? ' on ' + d.gpu_used : ''}` : 'Finished')
        : at === 2 ? `${d.elapsed || 'Starting'}${d.reason ? ' · node ' + d.reason : ''}` : d.gpu, state(2), log);
      step(ul, 'Bringing results back', at > 3 ? `${s.files_saved || 0} file${s.files_saved === 1 ? '' : 's'} saved` : 'into ' + (a.results_to || ''), state(3));
      body.appendChild(el('div', 'askadma-run-note', `Swan · ${gpuName}${d.node ? ' · node ' + d.node : ''} · as ${d.hcc_user || 'you'}`));
      body.appendChild(ul);
      if (d.needs_connection && ACTIVE.includes(st)) {
        const w = el('div', 'askadma-warn'); w.append(el('b', '', 'The Swan connection closed'),
          el('div', '', 'The job keeps going on Swan. Connect again so ADMA can follow it and bring the results back.'));
        const b = el('div', 'askadma-buttons'); b.appendChild(smallButton('Connect to Swan', 'fa-plug', 'primary', () => openSwanConnect(() => poll())));
        w.appendChild(b); body.appendChild(w);
      }
    }
    function show(s, problem) {
      clearTimeout(timer); card.classList.remove('done', 'failed');
      if (!s.status) { body.textContent = ''; body.appendChild(el('div', 'askadma-run-state', 'Could not get the status: ' + (s.error || 'no answer') + '.')); return; }
      last = s;
      const st = s.status;
      if (st === 'awaiting_confirmation') { review.problem = problem || ''; review(s); return; }
      renderSeq++;
      body.textContent = '';
      setPill(...(RUN_LABELS[st] || [st, '']));
      if (s.where === 'swan') progressSwan(s);
      else {
        const text = { queued: 'Waiting for the runner; another script is running.', running: `Running for ${s.seconds || 0} s`,
          importing: 'Saving the results to ADMA Studio' }[st];
        if (text) body.appendChild(el('div', 'askadma-run-state', text));
        if (s.log_tail && st === 'running') { const pre = el('pre'); pre.textContent = s.log_tail; body.appendChild(pre); pre.scrollTop = pre.scrollHeight; }
      }
      if (st === 'done' || st === 'failed') { card.classList.add(st); body.appendChild(resultBlock(s)); }
      if (st === 'cancelled') body.appendChild(el('div', 'askadma-run-state', s.error_message || 'Cancelled. Nothing was run.'));
      if (st === 'expired') body.appendChild(el('div', 'askadma-run-state', 'Not run: the Run button expires after 30 minutes. Ask again to run it.'));
      if (problem) body.appendChild(el('div', 'askadma-run-state', problem));
      const actions = el('div', 'askadma-buttons');
      if (ACTIVE.includes(st) && st !== 'fetching' && st !== 'importing' && !(s.swan && s.swan.cancel_requested))
        actions.appendChild(smallButton(s.where === 'swan' && ['pending', 'running'].includes(st) ? 'Stop the job' : 'Stop', 'fa-stop', '', () => send('cancel')));
      if (s.output_url) actions.appendChild(smallButton('Open results', 'fa-folder-open', 'primary', () => runAction({ type: 'navigate', url: s.output_url }, true)));
      if (actions.childNodes.length) body.appendChild(actions);
      if (ACTIVE.includes(st) && !(s.swan && s.swan.needs_connection)) timer = setTimeout(poll, s.where === 'swan' ? 3000 : 2500);
    }
    function resultBlock(s) {
      const wrap = el('div'); wrap.style.cssText = 'display:flex;flex-direction:column;gap:8px';
      if (s.status === 'failed') wrap.appendChild(el('div', 'askadma-run-state askadma-err', s.error_message || 'The run failed.'));
      if (s.hint) {
        const h = el('div', 'askadma-hint'); h.setAttribute('role', 'note');
        h.append(el('b', '', s.hint.title), el('div', '', s.hint.text));
        if (s.hint.command) {
          const row = el('div', 'askadma-cmd'); const code = el('code', '', s.hint.command);
          const copy = smallButton('Copy', 'fa-copy', '', async () => {
            try { await navigator.clipboard.writeText(s.hint.command); copy.lastChild.textContent = 'Copied'; } catch (e) { copy.lastChild.textContent = 'Select and copy'; }
          });
          row.append(code, copy); h.appendChild(row);
        }
        wrap.appendChild(h);
      }
      if (s.ran_text) wrap.appendChild(el('div', 'askadma-run-state', s.ran_text));
      const previews = s.previews || [];
      if (previews.length) {
        const grid = el('div', 'askadma-grid askadma-previews');
        previews.forEach((p, i) => {
          const t = el('button', 'askadma-tile'); t.type = 'button'; t.title = p.name;
          const img = document.createElement('img'); img.loading = 'lazy'; img.alt = p.name; img.src = p.thumb;
          img.addEventListener('error', () => t.remove());
          t.append(img, el('div', 'askadma-cap', p.name.split('/').pop()));
          t.addEventListener('click', () => openViewer(previews.map(galleryToPhoto), i));
          grid.appendChild(t);
        });
        wrap.appendChild(grid);
      }
      if (s.files_saved != null) {
        const o = s.outputs || {}, n = s.files_saved;
        const kinds = [o.figures ? `${o.figures} figure${o.figures === 1 ? '' : 's'}` : '', o.results ? `${o.results} result file${o.results === 1 ? '' : 's'}` : ''].filter(Boolean).join(', ');
        const line = el('div', 'askadma-run-state');
        line.appendChild(document.createTextNode(`${n} file${n === 1 ? '' : 's'} saved${kinds && n ? ' (' + kinds + ')' : ''}${s.output_url ? ' to ' : '.'}`));
        if (s.output_url) { const link = el('a', 'askadma-link', s.saved_label || s.output); link.href = s.output_url; line.appendChild(link); }
        wrap.appendChild(line);
      }
      if (s.log_tail) {
        const d = el('details', 'askadma-log'); if (s.status === 'failed' && !s.hint) d.open = true;
        d.appendChild(el('summary', '', 'Show the log')); const pre = el('pre'); pre.textContent = s.log_tail; d.appendChild(pre);
        wrap.appendChild(d);
      }
      return wrap;
    }
    async function send(action) {
      body.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      const payload = { action };
      if (action === 'run') {
        Object.assign(payload, { where: choice.where, gpu: choice.gpu, hours: choice.hours, setup: choice.setup, remove: Array.from(choice.removed),
          add: items.filter((it) => it.added && !choice.removed.has(it.id)).map((it) => ({ kind: it.kind, id: it.id })) });
        if (choice.where === 'swan') store.set('askadma_swan_setup', choice.setup || '');
      }
      const data = await studioRequest(url, 'POST', payload);
      if (data.needs_connection) { review.problem = ''; await review(last); openSwanConnect(() => review(last)); return; }
      if (data.status) show(data); else show(await studioRequest(url, 'GET'), data.error);
    }
    async function poll(first) {
      // later polls stop once the card is gone (new conversation); the first one runs before it is placed
      if (first || document.body.contains(card)) show(await studioRequest(url, 'GET'));
    }
    poll(true);
  }
  // ---------- your runs ----------
  function when(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000), today = new Date();
    const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return d.toDateString() === today.toDateString() ? t : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t;
  }
  function addRunsList(parent) {
    const box = el('div', 'askadma-runs');
    const head = el('div', 'askadma-rhead'); const refresh = el('button', 'askadma-iconbtn'); refresh.type = 'button';
    refresh.setAttribute('aria-label', 'Refresh'); refresh.title = 'Refresh'; refresh.appendChild(icon('fa-sync-alt'));
    const titleBox = el('div'); titleBox.style.cssText = 'flex:1;min-width:0';
    titleBox.append(el('div', 'askadma-eyebrow', 'ADMA STUDIO'), el('h4', '', 'Your runs'));
    head.append(studioBadge('md'), titleBox, refresh);
    const list = el('div'); list.style.cssText = 'display:flex;flex-direction:column;gap:8px';
    box.append(head, list); parent.appendChild(box);
    let timer = null;
    function row(r) {
      const div = el('div', 'askadma-runrow');
      const top = el('div', 'askadma-rhead'); const [label, cls] = RUN_LABELS[r.status] || [r.status, ''];
      top.append(icon(r.where === 'swan' ? 'fa-bolt' : 'fa-server'), el('b', '', r.script), el('span', 'askadma-pill' + (cls ? ' ' + cls : ''), label));
      div.appendChild(top);
      const sw = r.swan || {};
      const where = r.where === 'swan' ? `Swan · ${sw.gpu || 'GPU'}${sw.job_id ? ' · job ' + sw.job_id : ''}` : 'ADMA';
      div.appendChild(el('div', 'askadma-pd', `${where} · ${when(r.started || r.created)}`));
      let note = '';
      if (sw.needs_connection && RUN_ACTIVE.includes(r.status)) note = 'Swan connection closed: connect again so ADMA can follow it.';
      else if (r.status === 'uploading') note = `Copying to Swan · ${sw.copy_percent || 0}%`;
      else if (r.status === 'pending') note = `Waiting for a GPU ${sw.reason || ''}`;
      else if (r.status === 'running') note = r.where === 'swan' ? `Running on the GPU · ${sw.elapsed || ''}` : `Running for ${r.seconds || 0} s`;
      else if (r.status === 'done') note = `${r.files_saved || 0} file${r.files_saved === 1 ? '' : 's'} saved to ${r.saved_label || r.output}`;
      else if (r.status === 'failed') note = r.error_message || 'The run failed.';
      if (note) div.appendChild(el('div', 'askadma-pd', note));
      const btns = el('div', 'askadma-buttons');
      if (r.output_url) btns.appendChild(smallButton('Open results', 'fa-folder-open', '', () => runAction({ type: 'navigate', url: r.output_url }, true)));
      if (RUN_ACTIVE.includes(r.status) || r.status === 'awaiting_confirmation') {
        btns.appendChild(smallButton(r.status === 'awaiting_confirmation' ? 'Open the card' : 'Show progress', 'fa-eye', '', () => {
          const t = el('div', 'askadma-turn'); box.after(t);
          addRunCard(t, { run_id: r.run_id, script: r.script, where: r.where, inputs: r.inputs || [], output: r.output, timeout_minutes: 30 });
          t.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' });
        }));
      }
      if (sw.needs_connection && RUN_ACTIVE.includes(r.status)) btns.appendChild(smallButton('Connect to Swan', 'fa-plug', 'primary', () => openSwanConnect(() => load())));
      if (btns.childNodes.length) div.appendChild(btns);
      return div;
    }
    async function load() {
      clearTimeout(timer); refresh.disabled = true;
      const data = await studioRequest('/api/askadma/studio/runs/', 'GET');
      refresh.disabled = false; list.textContent = '';
      if (data.error) { list.appendChild(el('div', 'askadma-run-state', 'Could not load your runs: ' + data.error)); return; }
      const runs = data.runs || [];
      if (!runs.length) list.appendChild(el('div', 'askadma-run-note', 'No runs yet. Ask me to run one of your Python scripts, on ADMA or on a Swan GPU.'));
      runs.forEach((r) => list.appendChild(row(r)));
      if (runs.some((r) => RUN_ACTIVE.includes(r.status)) && document.body.contains(box)) timer = setTimeout(load, 10000);
    }
    refresh.addEventListener('click', load);
    load();
  }
  async function saveToStudio(a) {
    const note = el('div', 'askadma-msg note', 'Saving to ADMA Studio');
    msgs.appendChild(note); msgs.scrollTop = msgs.scrollHeight;
    const data = await studioRequest('/api/askadma/studio/save/', 'POST', { kind: a.kind, source: a.source, title: a.title || '' });
    note.remove();
    if (data.error) { addNote('Could not save: ' + data.error); return; }
    const entry = { role: 'assistant', local: true, content: data.message,
      suggestions: [{ label: 'Open reports folder', icon: 'fa-folder-open', action: { type: 'navigate', url: data.folder_url } }]
        .concat((data.saved || []).slice(0, 2).map((f) => ({ label: 'Open ' + f.name.replace(/^\d{4}-\d\d-\d\d_\d{4}_/, ''),
                                                           icon: 'fa-file', action: { type: 'navigate', url: f.url } }))) };
    conversation.push(entry); save(); renderEntry(entry); msgs.scrollTop = msgs.scrollHeight;
  }

  function runAction(a, fromClick) {
    if (!a || !a.type) return;
    if (a.type === 'navigate' && a.url) {
      if (a.highlight && samePage(a.url)) { highlight(a.highlight); return; }
      if (a.highlight) store.sset(PENDING, JSON.stringify({ url: a.url, highlight: a.highlight }));
      setTimeout(() => { window.location.href = a.url; }, fromClick ? 0 : 900);
    } else if (a.type === 'show_detections') {
      openViewer([a], 0);
    } else if (a.type === 'show_gallery' || a.type === 'render_chart') {
      const turn = el('div', 'askadma-turn');
      if (a.type === 'show_gallery') addGallery(turn, a); else addChart(turn, a);
      msgs.appendChild(turn); msgs.scrollTop = msgs.scrollHeight;
      conversation.push({ role: 'assistant', content: '', display: [a], local: true }); save();
    } else if (a.type === 'open_url' && a.url) {
      if (fromClick) window.open(a.url, '_blank', 'noopener');
    } else if (a.type === 'highlight' && a.id) {
      highlight(a.id);
    } else if (a.type === 'studio_save' && fromClick) {
      saveToStudio(a);
    } else if (a.type === 'show_runs') {
      const turn = el('div', 'askadma-turn'); addRunsList(turn);
      msgs.appendChild(turn); msgs.scrollTop = msgs.scrollHeight;
      if (fromClick) { conversation.push({ role: 'assistant', content: '', display: [a], local: true }); save(); }
    }
  }
  function applyPending() {
    const raw = store.sget(PENDING);
    if (!raw) return;
    store.sset(PENDING, null);
    try {
      const p = JSON.parse(raw);
      if (p.highlight && samePage(p.url)) setTimeout(() => highlight(p.highlight), 250);
    } catch (e) {}
  }

  // ---------- conversation rendering ----------
  function addNote(text) { const n = el('div', 'askadma-msg note', text); msgs.appendChild(n); msgs.scrollTop = msgs.scrollHeight; }
  function renderButtons(parent, buttons) {
    if (!buttons || !buttons.length) return;
    const row = el('div', 'askadma-buttons');
    buttons.forEach((b) => {
      const btn = el('button', 'askadma-btn'); btn.type = 'button';
      btn.appendChild(icon(b.icon)); btn.appendChild(document.createTextNode(b.label));
      btn.addEventListener('click', () => {
        if (b.action && b.action.type === 'studio_save') btn.disabled = true;
        if (b.action) runAction(b.action, true); else if (b.prompt) ask(b.prompt);
      });
      row.appendChild(btn);
    });
    parent.appendChild(row);
  }
  function renderEntry(m) {
    if (m.role === 'user') { msgs.appendChild(el('div', 'askadma-msg user', m.content)); return; }
    const turn = el('div', 'askadma-turn');
    if (m.steps && m.steps.length) {
      const d = el('details', 'askadma-done-steps');
      d.appendChild(el('summary', '', `${m.steps.length} step${m.steps.length > 1 ? 's' : ''}${m.seconds ? ` in ${m.seconds} s` : ''}`));
      const ul = el('ul', 'askadma-steps'); m.steps.forEach((s) => ul.appendChild(el('li', '', s))); d.appendChild(ul);
      turn.appendChild(d);
    }
    if (m.content) { const b = el('div', 'askadma-msg bot'); renderText(b, m.content); turn.appendChild(b); }
    (m.display || []).forEach((a) => {
      try {
        if (a.type === 'render_chart') addChart(turn, a);
        else if (a.type === 'show_gallery') addGallery(turn, a);
        else if (a.type === 'show_detections') addPhoto(turn, a);
        else if (a.type === 'confirm_run') addRunCard(turn, a);
        else if (a.type === 'confirm_globus') addGlobusCard(turn, a);
        else if (a.type === 'confirm_globus_plan') addGlobusPlanCard(turn, a);
        else if (a.type === 'show_runs') addRunsList(turn);
      } catch (e) {}
    });
    renderButtons(turn, m.suggestions);
    msgs.appendChild(turn);
  }
  function renderAll() {
    msgs.textContent = '';
    if (!conversation.length) {
      const hello = el('div', 'askadma-msg bot');
      renderText(hello, 'Hi! I can find files, show and count what is in photos, scan camera folders, and analyze or chart spreadsheets.');
      msgs.appendChild(hello);
      renderButtons(msgs, [
        { label: 'What is in this folder?', icon: 'fa-folder-open', prompt: 'What is in this folder?' },
        { label: 'Find spreadsheets', icon: 'fa-table', prompt: 'Find spreadsheets and CSV files' },
        { label: 'Show recent uploads', icon: 'fa-clock', prompt: 'What was added in the last 7 days?' }]);
    }
    conversation.forEach(renderEntry);
    msgs.scrollTop = msgs.scrollHeight;
  }

  // ---------- asking ----------
  let busy = false;
  async function ask(text) {
    const q = String(text || '').trim();
    if (!q || busy) return;
    busy = true; send.disabled = true;
    if (panel.hidden) openPanel(false);
    conversation.push({ role: 'user', content: q }); save();
    msgs.appendChild(el('div', 'askadma-msg user', q));

    const live = el('div', 'askadma-turn askadma-live');
    const ul = el('ul', 'askadma-steps');
    const now = el('li', 'now', 'Starting');
    const clock = el('span', 'askadma-clock', '0 s');
    now.appendChild(clock); ul.appendChild(now); live.appendChild(ul); msgs.appendChild(live);
    msgs.scrollTop = msgs.scrollHeight;
    const started = Date.now(), steps = [];
    const timer = setInterval(() => { clock.textContent = Math.round((Date.now() - started) / 1000) + ' s'; }, 1000);
    function step(textNow) {
      if (now.firstChild && now.firstChild.nodeType === 3 && now.firstChild.nodeValue !== 'Starting') {
        const done = el('li', '', now.firstChild.nodeValue); ul.insertBefore(done, now); steps.push(now.firstChild.nodeValue);
      }
      now.firstChild.nodeValue = textNow;
      msgs.scrollTop = msgs.scrollHeight;
    }

    let final = null, error = null;
    try {
      const resp = await fetch('/api/askadma/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: JSON.stringify({ messages: conversation.filter((m) => !m.local).slice(-12).map((m) => ({ role: m.role, content: m.content })),
                               current_url: location.pathname + location.search, stream: true })
      });
      const type = resp.headers.get('Content-Type') || '';
      if (!resp.ok && !type.includes('json')) throw new Error(`the server answered ${resp.status}`);
      if (type.includes('ndjson') && resp.body && resp.body.getReader) {
        const reader = resp.body.getReader(), dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (value) buf += dec.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
            if (!line) continue;
            const ev = JSON.parse(line);
            if (ev.type === 'step') step(ev.text);
            else if (ev.type === 'done') final = ev;
            else if (ev.type === 'error') error = ev.error;
          }
          if (done) break;
        }
      } else {
        const data = await resp.json();
        if (data.error) error = data.error;
        else final = { answer: data.answer, actions: data.actions || (data.action ? [data.action] : []), suggestions: data.suggestions || [] };
      }
    } catch (e) {
      error = (e && e.message) || String(e);
    }
    clearInterval(timer);
    if (now.firstChild && now.firstChild.nodeValue !== 'Starting') steps.push(now.firstChild.nodeValue);
    live.remove();

    if (final) {
      const actions = final.actions || [];
      const entry = { role: 'assistant', content: final.answer || '', steps, seconds: Math.round((Date.now() - started) / 1000),
                      suggestions: final.suggestions || [],
                      display: actions.filter((a) => ['render_chart', 'show_gallery', 'show_detections', 'confirm_run', 'confirm_globus', 'confirm_globus_plan', 'show_runs'].includes(a.type)) };
      conversation.push(entry); save(); renderEntry(entry);
      msgs.scrollTop = msgs.scrollHeight;
      const viewerAction = actions.filter((a) => a.type === 'show_detections').pop();
      if (viewerAction) openViewer([viewerAction], 0);
      const nav = actions.filter((a) => a.type === 'navigate').pop();
      if (nav) runAction(nav, false);
      actions.filter((a) => a.type === 'highlight').forEach((a) => runAction(a, false));
    } else {
      addNote('Something went wrong: ' + (error || 'no answer') + '. Try again.');
    }
    busy = false; send.disabled = false; input.focus();
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); const t = input.value; input.value = ''; autosize(); ask(t); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 140) + 'px'; }
  input.addEventListener('input', autosize);

  if (store.get(OPEN, '0') === '1') openPanel(false);
  try { markStudioCards(); } catch (e) {}
  renderAll();
  msgs.scrollTop = msgs.scrollHeight;
  applyPending();
  window.AskADMA = { open: openPanel, close: closePanel, ask: ask,
                     swanConnect: openSwanConnect, swanStatus: swanStatus, refreshSwan: refreshSwanBar,
                     globusConnect: openGlobusConnect };
})();
