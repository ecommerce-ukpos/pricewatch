/* ════════════════════════════════════════
   CONFIG + GLOBALS
════════════════════════════════════════ */
const SUPABASE_URL  = 'https://uaqakssusydpjzrcznhb.supabase.co';
const SUPABASE_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVhcWFrc3N1c3lkcGp6cmN6bmhiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3NDM3ODUsImV4cCI6MjA5NDMxOTc4NX0.OM0XBJsFb6hlLKXLoNpahuH4zzGcnNR3W-bzKfKPZ-w';
const API_BASE      = 'https://uaqakssusydpjzrcznhb.supabase.co/functions/v1/api';
const ALLOWED_DOMAINS = ['ukpos.com'];

let sb, currentUser, currentProfile;
let skuPage = 1, skuLimit = 50, skuTotal = 0;
let alertTab = 'crit';
let currentCompId = null, currentCompName = null, currentCompSlug = null;
let currentSkuId = null;
let compViewMode = 'grid';
let compSkusAll = [], compSkusFiltered = [];
let drawerSkuId = null, drawerFromPanel = null;
let distChart = null;

/* Configurable thresholds — loaded from localStorage */
let T = { red: 10, amb: 5, par: 2 };
(function loadThresholds() {
  try {
    const saved = localStorage.getItem('pw_thresholds');
    if (saved) T = {...T, ...JSON.parse(saved)};
  } catch(e) {}
  applyThresholdCSS();
})();

function applyThresholdCSS() {
  document.getElementById('thresh-red-label') && (document.getElementById('thresh-red-label').textContent = T.red);
  document.getElementById('thresh-red-label2') && (document.getElementById('thresh-red-label2').textContent = T.red);
  document.getElementById('thresh-amb-label') && (document.getElementById('thresh-amb-label').textContent = T.amb);
}

function isMobile() { return window.innerWidth <= 768; }

function setTopbarLogo(collapsed) {
  const el = document.getElementById('topbar-logo');
  if (!el) return;
  const src = (!collapsed || isMobile()) ? 'logo-full.png' : 'logo-icon.png';
  const img = new Image();
  img.alt = 'UKPOS';
  img.style.cssText = 'height:22px;width:auto;display:block';
  img.onload  = () => { el.innerHTML = ''; el.appendChild(img); };
  img.onerror = () => { el.innerHTML = ''; };
  img.src = src;
}

function toggleSidebar() {
  const shell     = $('view-app');
  const icon      = $('sidebar-toggle-icon');
  const btn       = $('sidebar-toggle-btn');
  const collapsed = shell.classList.toggle('nav-collapsed');
  icon.className  = collapsed ? 'ti ti-layout-sidebar-left-expand' : 'ti ti-layout-sidebar-left-collapse';
  btn.title       = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  setTopbarLogo(collapsed);
  try { localStorage.setItem('pw_sidebar_collapsed', collapsed ? '1' : '0'); } catch(e) {}
}

function toggleMobileSidebar() {
  const sidebar = $('sidebar');
  const overlay = $('sidebar-overlay');
  const icon    = $('mob-hamburger-icon');
  const isOpen  = sidebar.classList.toggle('mob-open');
  overlay.classList.toggle('open', isOpen);
  if (icon) icon.className = isOpen ? 'ti ti-x' : 'ti ti-menu-2';
}

document.addEventListener('click', e => {
  if (e.target.closest('.nav-item') && $('sidebar')?.classList.contains('mob-open')) {
    toggleMobileSidebar();
  }
});

(function initSidebar() {
  try {
    const wasCollapsed = !isMobile() && localStorage.getItem('pw_sidebar_collapsed') === '1';
    if (wasCollapsed) {
      const shell = document.getElementById('view-app');
      const icon  = document.getElementById('sidebar-toggle-icon');
      const btn   = document.getElementById('sidebar-toggle-btn');
      if (shell) shell.classList.add('nav-collapsed');
      if (icon)  icon.className = 'ti ti-layout-sidebar-left-expand';
      if (btn)   btn.title = 'Expand sidebar';
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => setTopbarLogo(wasCollapsed));
    } else {
      setTopbarLogo(wasCollapsed);
    }
  } catch(e) { setTopbarLogo(false); }
})();

/* ════════════════════════════════════════
   HELPERS
════════════════════════════════════════ */
const $  = id => document.getElementById(id);
const el = (tag, attrs={}, ...children) => {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k,v]) => {
    if (k === 'className') e.className = v;
    else if (k === 'innerHTML') e.innerHTML = v;
    else if (k.startsWith('on')) e[k] = v;
    else e.setAttribute(k, v);
  });
  children.forEach(c => e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
};

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function matchStatusMini(ms) {
  const parts = [];
  if (ms.human    > 0) parts.push(`<span style="display:inline-flex;align-items:center;gap:2px;background:var(--gb);color:var(--grn);border-radius:4px;padding:1px 5px;font-size:11px;font-weight:500" title="${ms.human} human verified"><i class="ti ti-user-check" style="font-size:11px"></i>${ms.human}</span>`);
  if (ms.auto     > 0) parts.push(`<span style="display:inline-flex;align-items:center;gap:2px;background:var(--bb);color:var(--blu);border-radius:4px;padding:1px 5px;font-size:11px" title="${ms.auto} AI matched"><i class="ti ti-robot" style="font-size:11px"></i>${ms.auto}</span>`);
  if (ms.review   > 0) parts.push(`<span style="display:inline-flex;align-items:center;gap:2px;background:var(--ab);color:var(--amb);border-radius:4px;padding:1px 5px;font-size:11px" title="${ms.review} need review"><i class="ti ti-eye" style="font-size:11px"></i>${ms.review}</span>`);
  if (ms.rejected > 0) parts.push(`<span style="display:inline-flex;align-items:center;gap:2px;background:var(--bg);color:var(--t3);border-radius:4px;padding:1px 5px;font-size:11px" title="${ms.rejected} rejected"><i class="ti ti-x" style="font-size:11px"></i>${ms.rejected}</span>`);
  return parts.length ? `<div style="display:flex;gap:3px;flex-wrap:wrap">${parts.join('')}</div>` : '<span style="color:var(--t3);font-size:12px">—</span>';
}

function thumbUrl(url, w=120, h=120) {
  if (!url) return '';
  if (url.includes('supabase.co/storage/v1/object/public/')) {
    return `${url}?width=${w}&height=${h}&resize=contain&quality=80`;
  }
  return url;
}

function ts(dt) {
  if (!dt) return '—';
  const d = new Date(dt);
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return 'Just now';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) {
    return d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  }
  if (diff < 172800000) return 'Yesterday ' + d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  return d.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
}

function fmtPrice(p) {
  if (p === null || p === undefined) return '—';
  return '£' + parseFloat(p).toFixed(2);
}

function normalisePrice(price, vat) {
  if (!price) return null;
  const p = parseFloat(price);
  return vat === 'inc' ? p / 1.2 : p;
}

function getTier(diff) {
  if (diff === null || diff === undefined) return 'par';
  if (diff <= -T.red) return 'r';
  if (diff <= -T.amb) return 'a';
  if (diff >= T.par)  return 'g';
  return 'p';
}

function diffClass(diff) {
  return {r:'d-r', a:'d-a', g:'d-g', p:'d-p'}[getTier(diff)];
}

function diffLabel(diff) {
  if (diff === null || diff === undefined) return '—';
  return (diff > 0 ? '+' : '') + parseFloat(diff).toFixed(1) + '%';
}

function vatPill(v) {
  if (v === 'ex')  return '<span class="vat vex">ex</span>';
  if (v === 'inc') return '<span class="vat vinc">inc</span>';
  return '<span class="vat vunk">?</span>';
}

function stockBadge(avail) {
  if (avail === 'out_of_stock') return '<span class="badge b-oos"><i class="ti ti-package-off" style="font-size:9px"></i> OOS</span>';
  if (avail === 'unavailable')  return '<span class="badge b-gray">Unavail.</span>';
  return '';
}

function confWidget(c) {
  if (!c) return '—';
  const col = c >= 80 ? 'var(--grn)' : c >= 60 ? 'var(--amb)' : 'var(--red)';
  return `<div class="cbar"><div class="ctrack"><div class="cfill" style="width:${c}%;background:${col}"></div></div><span style="font-size:10px">${c}%</span></div>`;
}

function buildLegend(elId) {
  const e = $(elId);
  if (!e) return;
  e.innerHTML = `
    <div class="leg-item"><div class="leg-sw" style="background:var(--rb);border:1px solid var(--rbd)"></div>&gt;${T.red}% more exp.</div>
    <div class="leg-item"><div class="leg-sw" style="background:var(--ab);border:1px solid var(--abd)"></div>${T.amb}–${T.red}%</div>
    <div class="leg-item"><div class="leg-sw" style="background:var(--bg);border:1px solid var(--border)"></div>±${T.par}% parity</div>
    <div class="leg-item"><div class="leg-sw" style="background:var(--gb);border:1px solid var(--gbd)"></div>We're cheaper</div>`;
}

function rowClass(diff) {
  return {r:'row-r', a:'row-a', g:'row-g', p:'row-gray'}[getTier(diff)];
}

function skuLink(row) {
  const url = row.our_url || row.product_url || (row.slug ? `https://www.ukpos.com/${row.slug}?vat=0` : '#');
  return `<a class="prod-link" href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">
    <span style="font-family:'SF Mono',monospace;font-size:11px;color:var(--blu)">${row.sku_id}</span>
    <span style="font-size:10px;color:var(--t2);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">${row.short_title||''}</span>
  </a>`;
}

function slugify(name) {
  return (name||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
}

let cachedToken = null;

async function getToken() {
  if (cachedToken) return cachedToken;
  const { data: { session } } = await sb.auth.getSession();
  cachedToken = session?.access_token || null;
  return cachedToken;
}

async function authFetch(path, opts = {}) {
  opts.headers = opts.headers || {};
  const token = await getToken();
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) throw new Error(`API ${path} → ${res.status}`);
  return res.json();
}

async function authPost(path, body, method = 'POST') {
  return authFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined
  });
}

function showMsg(elId, text, kind) {
  $(elId).innerHTML = `<div class="auth-msg ${kind}">${text}</div>`;
}

function copyUrl() {
  navigator.clipboard.writeText(window.location.href).catch(() => {});
  event.target.textContent = 'Copied!';
  setTimeout(() => event.target.textContent = 'Copy link', 1500);
}

/* ════════════════════════════════════════
   ROUTING — hash-based
════════════════════════════════════════ */
const PANEL_MAP = {
  alerts: 'alerts', review: 'review', skus: 'skus',
  bycat: 'bycat', bycomp: 'bycomp', schedule: 'schedule', settings: 'settings'
};
const NAV_ITEMS = ['alerts','review','skus','bycat','bycomp','schedule','settings'];

function go(name, opts = {}) {
  document.querySelectorAll('.panel').forEach(p => {
    p.classList.remove('active');
    p.style.display = '';
  });
  NAV_ITEMS.forEach(n => {
    const el = $('nav-' + n);
    if (el) el.classList.remove('active');
  });

  const panelId = 'p-' + name;
  const panel = $(panelId);
  if (!panel) { go('alerts'); return; }
  panel.classList.add('active');

  let hash = '#' + name;
  if (opts.skuId) { hash = '#sku/' + opts.skuId; }
  else if (opts.compSlug) { hash = '#competitor/' + opts.compSlug; }
  history.replaceState(null, '', hash);

  const navKey = {
    'alerts':'alerts','review':'review','skus':'skus','bycat':'bycat',
    'bycomp':'bycomp','comp-detail':'bycomp','sku-detail':'skus',
    'schedule':'schedule','settings':'settings'
  }[name];
  if (navKey && $('nav-' + navKey)) $('nav-' + navKey).classList.add('active');

  if (name === 'skus') {
    const q = $('skuQ');
    if (q) q.value = '';
    if (!skusLoaded) { skusLoaded = true; loadSKUs(); }
  }
  if (name === 'review')      loadReview();
  if (name === 'bycat')       loadByCategory();
  if (name === 'bycomp')      loadByCompetitor();
  if (name === 'schedule')    loadRuns();
  if (name === 'settings')    { goSett('configurables'); loadCompetitorSettings(); loadUsers(); }
  if (name === 'comp-detail') { loadCompDetail(opts); }
  if (name === 'sku-detail')  { loadSkuDetail(opts); }
}

function restoreRoute() {
  const hash = location.hash.replace('#','');
  if (!hash) { go('alerts'); return; }
  if (hash.startsWith('sku/')) {
    go('sku-detail', { skuId: hash.slice(4) });
  } else if (hash.startsWith('competitor/')) {
    const slug = hash.slice(11);
    if (sb) {
      sb.from('competitors').select('id,name,domain,vat_status').eq('active',true).then(({data}) => {
        const c = (data||[]).find(x => slugify(x.name) === slug);
        if (c) go('comp-detail', { compId: c.id, compName: c.name, compSlug: slug, compDomain: c.domain, compVat: c.vat_status });
        else go('bycomp');
      });
    }
  } else if (PANEL_MAP[hash]) {
    go(hash);
  } else {
    go('alerts');
  }
}

function goSett(tab) {
  document.querySelectorAll('.stab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.sn-item').forEach(n => n.classList.remove('active'));
  const st = $('st-' + tab); if (st) st.classList.add('active');
  const sn = $('sn-' + tab); if (sn) sn.classList.add('active');
}

async function signOut() {
  if (sb) await sb.auth.signOut();
  window.location.href = 'login.html';
}

/* ════════════════════════════════════════
   SESSION GUARD
════════════════════════════════════════ */
async function bootstrap() {
  sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
  const { data: { session } } = await sb.auth.getSession();
  if (!session) {
    sessionStorage.setItem('pw_redirect', window.location.href);
    window.location.href = 'login.html';
    return;
  }
  await onSignedIn(session);

  sb.auth.onAuthStateChange(async (event, session) => {
    cachedToken = session?.access_token || null;
    if (event === 'SIGNED_OUT' || !session) {
      window.location.href = 'login.html';
      return;
    }
    if (['SIGNED_IN','TOKEN_REFRESHED','USER_UPDATED','INITIAL_SESSION'].includes(event)) {
      await onSignedIn(session);
    }
  });
}

async function onSignedIn(session) {
  currentUser  = session.user;
  cachedToken  = session.access_token || null;
  const { data: profiles } = await sb.from('profiles').select('*').eq('id', session.user.id).limit(1);
  if (!profiles?.length || profiles[0].status === 'pending' || profiles[0].status === 'rejected') {
    window.location.href = 'login.html';
    return;
  }
  currentProfile = profiles[0];
  $('header-email').textContent = currentProfile.email || '—';
  $('ma-email').value = currentProfile.email || '';
  $('ma-name').value  = currentProfile.full_name || '';

  $('view-app').style.display = 'grid';
  if (currentProfile.role === 'super_admin') {
    const usersTab = $('sn-users');
    if (usersTab) usersTab.style.display = '';
  }
  loadDashboard();
  restoreRoute();
  if (!allSkus.length) {
    sb.from('skus')
      .select('sku_id,short_title,price_ex_vat,product_url,availability,cat_l4,cat_l5,image_url,slug')
      .eq('active', true)
      .order('sku_id')
      .then(({ data }) => { if (data) allSkus = data; });
  }
  setInterval(() => { if ($('p-alerts').classList.contains('active')) loadDashboard(); }, 5 * 60 * 1000);
}

function openMyAccount() { go('settings'); goSett('account'); }

async function saveMyAccount() {
  const name = $('ma-name').value.trim();
  const pw   = $('ma-password').value;
  $('ma-msg').style.display = 'none';
  try {
    const { error: dbErr } = await sb.from('profiles').update({ full_name: name }).eq('id', currentProfile.id);
    if (dbErr) throw new Error(dbErr.message);
    if (pw) {
      if (pw.length < 8) throw new Error('Password must be at least 8 characters.');
      const { error: pwErr } = await sb.auth.updateUser({ password: pw });
      if (pwErr) throw new Error(pwErr.message);
    }
    currentProfile.full_name = name;
    $('ma-msg').textContent = 'Changes saved.';
    $('ma-msg').className = 'auth-msg ok';
    $('ma-msg').style.display = 'block';
    $('ma-password').value = '';
  } catch (err) {
    $('ma-msg').textContent = err.message;
    $('ma-msg').className = 'auth-msg err';
    $('ma-msg').style.display = 'block';
  }
}

/* ════════════════════════════════════════
   DASHBOARD / ALERTS
════════════════════════════════════════ */
async function loadDashboard() {
  try {
    const d = await authFetch('/dashboard');
    const m = d.metrics || {};

    $('m-crit').textContent  = m.critical  ?? '—';
    $('m-warn').textContent  = m.warning   ?? '—';
    $('m-cheap').textContent = m.cheapest  ?? '—';
    $('m-oos').textContent   = m.oos       ?? '—';

    const reviewCount = m.review || 0;
    if (reviewCount > 0) {
      $('nav-review-badge').textContent = reviewCount.toLocaleString();
      $('nav-review-badge').style.display = '';
    }
    const critCount = m.critical || 0;
    if (critCount > 0) {
      $('nav-alerts-badge').textContent = critCount;
      $('nav-alerts-badge').style.display = '';
    }

    $('sidebar-foot').innerHTML = `<strong>${(d.sku_count||0).toLocaleString()}</strong> SKUs · <strong>${d.competitor_count||23}</strong> competitors<br><strong>${(d.snapshot_count||0).toLocaleString()}</strong> snapshots`;

    if (d.last_run) {
      const r = d.last_run;
      $('sync-status').innerHTML = `Last sync <strong style="color:rgba(255,255,255,.65)">${ts(r.completed_at||r.started_at)}</strong> · ${r.skus_succeeded||0} SKUs`;
    }

    if (reviewCount > 100) {
      $('alerts-data-note').style.display = '';
    }
    $('alerts-sub').textContent = `${critCount} critical · ${m.warning||0} warning · ${m.oos||0} competitor OOS`;

    renderPriorityStrip(d.worst || []);
    renderAlertList(d.alerts || []);
    if (d.alerts?.length) $('alert-ts').textContent = `— ${d.alerts.length} active`;
    renderWorstTable(d.worst || []);
    buildDistChart(m);

  } catch (e) {
    console.error('Dashboard load error:', e);
    $('dash-alerts').innerHTML = `<div style="color:var(--red);font-size:11px;padding:8px">Failed to load: ${e.message}</div>`;
  }
}

function renderPriorityStrip(rows) {
  const strip = $('strip-items');
  if (!rows.length) { strip.innerHTML = '<span style="color:rgba(255,255,255,.3);font-size:14px">No data yet</span>'; return; }
  strip.innerHTML = rows.slice(0, 6).map(r => {
    const diff = r.diff_pct_normalised ?? r.diff_pct;
    const tierCls = diff <= -T.red ? '' : 'warn';
    return `<div class="strip-item" onclick="go('sku-detail',{skuId:'${r.sku_id}',fromPanel:'alerts'})" title="Go to ${r.sku_id}">
      <div class="si-name" style="font-size:12px;max-width:160px;white-space:normal;line-height:1.3">${(r.short_title||r.sku_id).slice(0,40)}</div>
      <div class="si-val" style="font-size:14px;font-weight:600;margin:3px 0">${fmtPrice(r.our_price)}</div>
      <div class="si-diff ${tierCls}" style="font-size:13px">${diffLabel(diff)} vs ${r.competitor_name||'competitor'}</div>
    </div>`;
  }).join('');
}

function setAlertTab(tab) {
  alertTab = tab;
  ['all','crit','warn','oos'].forEach(t => {
    const b = $('atab-'+t);
    if (b) b.style.background = t === tab ? 'var(--bg)' : '';
  });
  if (window._lastAlerts) renderAlertList(window._lastAlerts);
}

function exportAlerts() {
  const alerts = window._lastAlerts || [];
  const filtered = alertTab === 'all' ? alerts
    : alerts.filter(a => alertTab === 'crit' ? a.alert_type === 'critical'
        : alertTab === 'warn' ? a.alert_type === 'warning'
        : ['oos_us','oos_competitor','unavailable'].includes(a.alert_type));
  if (!filtered.length) { alert('No alerts to export for the current filter.'); return; }
  const rows = [['SKU ID','Product','Alert Type','Our Price','Their Price','Diff %','Competitor','Created']];
  filtered.forEach(a => rows.push([
    a.sku_id, (a.skus?.short_title||''), a.alert_type,
    a.our_price||'', a.their_price||'', a.diff_pct||'',
    (a.competitors?.name||''), a.created_at||''
  ]));
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], {type:'text/csv'});
  const url  = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download=`alerts-${alertTab}-${new Date().toISOString().slice(0,10)}.csv`; a.click();
  URL.revokeObjectURL(url);
}

function renderAlertList(alerts) {
  window._lastAlerts = alerts;
  const filtered = alertTab === 'all' ? alerts
    : alertTab === 'crit' ? alerts.filter(a => a.alert_type === 'critical')
    : alertTab === 'warn' ? alerts.filter(a => a.alert_type === 'warning')
    : alerts.filter(a => ['oos_us','oos_competitor','unavailable'].includes(a.alert_type));

  if (!filtered.length) {
    $('dash-alerts').innerHTML = '<div style="color:var(--t2);padding:8px;text-align:center">No alerts for this filter</div>';
    return;
  }
  const typeMap = {
    critical: {cls:'ar-crit',icon:'ti-trending-up'},
    warning:  {cls:'ar-warn',icon:'ti-trending-up'},
    oos_us:   {cls:'ar-oos', icon:'ti-package-off'},
    oos_competitor: {cls:'ar-oos',icon:'ti-package-off'},
    unavailable: {cls:'ar-oos',icon:'ti-x'},
    price_rise_them: {cls:'ar-good',icon:'ti-trending-down'},
  };
  $('dash-alerts').innerHTML = filtered.slice(0, 8).map(a => {
    const {cls, icon} = typeMap[a.alert_type] || {cls:'ar-info',icon:'ti-bell'};
    const sku  = a.skus || {};
    const comp = a.competitors || {};
    const borderColor = a.alert_type === 'critical' ? 'var(--red)' : a.alert_type === 'warning' ? 'var(--amb)' : 'var(--t3)';
    const bg = a.alert_type === 'critical' ? 'var(--rb)' : a.alert_type === 'warning' ? 'var(--ab)' : 'var(--bg)';
    return `<div style="border-left:3px solid ${borderColor};border-radius:0 var(--r) var(--r) 0;padding:10px 12px;background:${bg};border:1px solid var(--border);border-left-width:3px;margin-bottom:6px;cursor:pointer"
      onclick="go('sku-detail',{skuId:'${a.sku_id}',fromPanel:'alerts'})">
      <div style="display:flex;align-items:flex-start;gap:7px;margin-bottom:6px">
        <i class="ti ${icon}" style="font-size:14px;flex-shrink:0;margin-top:1px"></i>
        <span style="flex:1;font-weight:500;line-height:1.4;font-size:14px">${a.message}</span>
        <span style="font-size:12px;opacity:.55;white-space:nowrap">${ts(a.created_at)}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <div style="background:rgba(0,0,0,.04);border-radius:5px;padding:6px 8px">
          <div style="font-size:12px;opacity:.6;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">Your product</div>
          <a href="#sku/${a.sku_id}" onclick="event.preventDefault();event.stopPropagation();go('sku-detail',{skuId:'${a.sku_id}',fromPanel:'alerts'})"
            style="color:var(--blu);font-weight:600;font-size:14px;text-decoration:none">${a.sku_id}</a>
          <div style="font-size:12px;color:var(--t2);margin-top:1px">${sku.short_title||''}</div>
          ${a.our_price ? `<div style="font-weight:600;margin-top:3px;font-size:14px">${fmtPrice(a.our_price)} <span class="vat vex">ex</span></div>` : ''}
        </div>
        <div style="background:rgba(0,0,0,.04);border-radius:5px;padding:6px 8px">
          <div style="font-size:12px;opacity:.6;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">${comp.name||'Competitor'}</div>
          ${a.their_price ? `<div style="font-weight:600;font-size:14px">${fmtPrice(a.their_price)}</div>` : '<div style="opacity:.5;font-size:13px">Price unknown</div>'}
          ${a.diff_pct ? `<div style="margin-top:3px"><span class="${diffClass(a.diff_pct)}">${diffLabel(a.diff_pct)}</span></div>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');
}

function renderWorstTable(rows) {
  if (!rows.length) {
    $('dash-tbody').innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--t2);padding:20px">No data yet — run a sync first</td></tr>';
    return;
  }
  $('dash-tbody').innerHTML = rows.map(r => {
    const diff = r.diff_pct_normalised ?? r.diff_pct;
    const raw  = r.competitor_price ? parseFloat(r.competitor_price) : null;
    const vat  = r.competitor_vat || r.competitor_vat_default || 'unknown';
    const ex   = raw ? normalisePrice(raw, vat) : null;
    return `<tr class="${rowClass(diff)} tr-link" onclick="go('sku-detail',{skuId:'${r.sku_id}',fromPanel:'alerts'})">
      <td>${skuLink(r)}</td>
      <td style="color:var(--t2);max-width:160px;white-space:normal;line-height:1.35">${r.short_title||''}</td>
      <td style="font-weight:500">${fmtPrice(r.our_price)}</td>
      <td>${r.competitor_name||'—'}</td>
      <td style="font-weight:500">${ex ? fmtPrice(ex) : '—'}</td>
      <td>${vatPill('ex')} ${vatPill(vat)}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span></td>
      <td>${stockBadge(r.availability)}</td>
      <td style="color:var(--t3)">${ts(r.scraped_at)}</td>
      <td>→</td>
    </tr>`;
  }).join('');
}

function buildDistChart(m) {
  const ctx = $('distChart');
  if (!ctx) return;
  if (distChart) distChart.destroy();
  distChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ["Cheaper", "±Parity", `${T.amb}–${T.red}%`, `>${T.red}%`, "OOS"],
      datasets: [{
        data: [m.cheapest||0, m.parity||0, m.warning||0, m.critical||0, m.oos||0],
        backgroundColor: ['#639922','#888780','#BA7517','#A32D2D','#B4B2A9'],
        borderRadius: 3, borderSkipped: false
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `${c.parsed.y} SKUs` } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: '#6b6a64', font: { size: 9 }, maxRotation: 0 } },
        y: { grid: { color: 'rgba(0,0,0,.05)' }, ticks: { color: '#6b6a64', font: { size: 9 } } }
      }
    }
  });
}

/* ════════════════════════════════════════
   SKU TABLE
════════════════════════════════════════ */
let skusAllData = [];

async function loadSKUs() {
  const q     = $('skuQ')?.value || '';
  const diff  = $('fDiff')?.value || '';
  const vat   = $('fVat')?.value  || '';
  const stock = $('fStock')?.value || '';
  let url = `/skus?page=${skuPage}&limit=${skuLimit}`;
  if (q)     url += '&q='    + encodeURIComponent(q);
  if (diff)  url += '&diff=' + diff;
  if (vat)   url += '&vat='  + vat;
  if (stock) url += '&stock='+ stock;

  try {
    const d = await authFetch(url);
    const rows = d.data || [];
    if (d.total !== undefined) skuTotal = d.total;
    else if (rows.length < skuLimit) skuTotal = (skuPage-1)*skuLimit + rows.length;

    if (!rows.length) {
      $('skus-sub').textContent = `${skuTotal.toLocaleString()} SKUs`;
      $('sku-tbody').innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--t2);padding:20px">No matches found</td></tr>';
      $('sku-pagination').innerHTML = '';
      return;
    }

    const skuMetaMap = {};
    (allSkus || []).forEach(s => { skuMetaMap[s.sku_id] = { image_url: s.image_url, unit_qty: s.unit_qty }; });
    const enriched = rows.map(r => ({
      ...r,
      image_url: skuMetaMap[r.sku_id]?.image_url || r.image_url || null,
      unit_qty:  skuMetaMap[r.sku_id]?.unit_qty  || r.unit_qty  || 1,
    }));
    skusAllData = enriched;

    if (!window._matchSummary) {
      sb.from('competitor_matches')
        .select('sku_id,match_source,match_status,reviewed_at,human_reviewed')
        .then(({ data: cm }) => {
          if (!cm) return;
          const summary = {};
          cm.forEach(r => {
            if (!summary[r.sku_id]) summary[r.sku_id] = { human:0, auto:0, review:0, rejected:0, last_reviewed:null };
            const s = summary[r.sku_id];
            if (r.match_status === 'rejected')                                    s.rejected++;
            else if (r.match_source === 'human' || r.human_reviewed)              s.human++;
            else if (r.match_source === 'scraper_auto' || r.match_status === 'matched') s.auto++;
            else                                                                   s.review++;
            if (r.reviewed_at && (!s.last_reviewed || r.reviewed_at > s.last_reviewed))
              s.last_reviewed = r.reviewed_at;
          });
          window._matchSummary = summary;
          if ($('p-skus')?.classList.contains('active') && skusData.length) {
            const enriched2 = skusData.map(r => ({ ...r, _ms: summary[r.sku_id] || null }));
            skusData = enriched2;
            renderSkusRows(enriched2);
          }
        });
    }

    const enrichedWithMs = enriched.map(r => ({ ...r, _ms: window._matchSummary?.[r.sku_id] || null }));
    skusAllData = enrichedWithMs;

    const cf = $('fComp');
    if (cf && cf.options.length === 1) {
      if (window._allCompetitors?.length) {
        window._allCompetitors.forEach(c => {
          const opt = document.createElement('option');
          opt.value = c.name; opt.textContent = c.name;
          cf.appendChild(opt);
        });
      } else {
        sb.from('competitors').select('name').eq('active',true).order('name').then(({ data }) => {
          if (!data) return;
          window._allCompetitors = data;
          const cf2 = $('fComp');
          if (!cf2) return;
          data.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.name; opt.textContent = c.name;
            cf2.appendChild(opt);
          });
        });
      }
    }

    applySkuCompetitorFilter();
  } catch (e) {
    $('sku-tbody').innerHTML = `<tr><td colspan="13" style="color:var(--red);padding:8px">${e.message}</td></tr>`;
  }
}

function applySkuCompetitorFilter() {
  const comp = $('fComp')?.value || '';
  const rows = comp ? skusAllData.filter(r => r.competitor_name === comp) : skusAllData;

  $('skus-sub').textContent = comp
    ? `${rows.length.toLocaleString()} of ${skusAllData.length.toLocaleString()} SKUs matched by ${comp}`
    : `${skuTotal.toLocaleString()} SKUs`;

  if (!rows.length) {
    $('sku-tbody').innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--t2);padding:20px">No SKUs matched by this competitor</td></tr>';
    $('sku-pagination').innerHTML = '';
    return;
  }

  skusData = rows;
  sortState.skus = { col: null, dir: 1 };
  updateSortHeaders('skus-table', 'skus', null);
  renderSkusRows(rows);

  if (comp) {
    $('sku-pagination').innerHTML = `<span style="color:var(--t2)">${rows.length.toLocaleString()} SKUs matched by ${comp}</span><span></span>`;
  } else {
    const from = (skuPage-1)*skuLimit+1, to = (skuPage-1)*skuLimit+rows.length;
    $('sku-pagination').innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span>${from.toLocaleString()}–${to.toLocaleString()} of ${skuTotal.toLocaleString()}</span>
        <select onchange="skuLimit=+this.value===0?999999:+this.value;skuPage=1;loadSKUs()" style="padding:3px 6px;border-radius:5px;border:1px solid var(--bm);background:var(--surface);font-size:11px">
          <option value="50" ${skuLimit===50?'selected':''}>50 / page</option>
          <option value="100" ${skuLimit===100?'selected':''}>100 / page</option>
          <option value="250" ${skuLimit===250?'selected':''}>250 / page</option>
        </select>
      </div>
      <div style="display:flex;gap:6px">
        ${skuPage>1?`<button class="btn sm" onclick="skuPage--;loadSKUs()">← Prev</button>`:''}
        ${rows.length===skuLimit?`<button class="btn sm" onclick="skuPage++;loadSKUs()">Next →</button>`:''}
      </div>`;
  }
}

function filterSKUs() {
  const comp = $('fComp')?.value || '';
  if (comp && skusAllData.length) { applySkuCompetitorFilter(); return; }
  skuPage = 1;
  loadSKUs();
}

/* ════════════════════════════════════════
   MATCH MANAGER
════════════════════════════════════════ */
let matchTab     = 'review';
let reviewPage   = 1;
let reviewLimit  = 50;
let reviewAllRows = [];
let reviewData    = [];

function setMatchTab(tab) {
  matchTab   = tab;
  reviewPage = 1;
  ['review','auto','human','amended','rejected'].forEach(t =>
    $('mtab-'+t)?.classList.toggle('active', t === tab)
  );
  filterReview();
}

async function loadReview() {
  $('review-tbody').innerHTML = `<tr><td colspan="11"><div class="loading"><span class="spinner"></span></div></td></tr>`;

  const cf = $('review-comp-filter');
  if (cf && cf.options.length === 1) {
    const { data: comps } = await sb.from('competitors').select('id,name').eq('active',true).order('name');
    (comps||[]).forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.name; opt.textContent = c.name;
      cf.appendChild(opt);
    });
  }

  try {
    const [{ data: matches, error: err1 }, { data: snaps, error: err2 }] = await Promise.all([
      sb.from('competitor_matches')
        .select(`
          id, sku_id, competitor_id, competitor_url, competitor_title,
          competitor_image_url, confidence, match_status, match_source,
          human_reviewed, reviewed_at, updated_at, notes, previous_url,
          skus!inner(sku_id, short_title, price_ex_vat, product_url, image_url, slug, unit_qty),
          competitors!inner(name, domain, vat_status)
        `)
        .order('updated_at', { ascending: false })
        .limit(3000),
      sb.from('latest_snapshots')
        .select('sku_id, competitor_id, competitor_price, competitor_vat, competitor_vat_default, diff_pct, diff_pct_normalised, availability, scraped_at'),
    ]);

    if (err1) throw new Error(err1.message);
    if (err2) throw new Error(err2.message);

    const snapMap = {};
    (snaps||[]).forEach(s => { snapMap[`${s.sku_id}__${s.competitor_id}`] = s; });

    reviewAllRows = (matches||[]).map(m => ({
      ...m,
      _snap: snapMap[`${m.sku_id}__${m.competitor_id}`] || {},
    }));

    updateMatchTabCounts();
    filterReview();
  } catch(e) {
    $('review-tbody').innerHTML = `<tr><td colspan="13" style="color:var(--red);padding:8px">${e.message}</td></tr>`;
  }
}

function updateMatchTabCounts() {
  const counts = { review:0, auto:0, human:0, amended:0, rejected:0 };
  reviewAllRows.forEach(r => {
    if      (r.match_status === 'rejected')                              counts.rejected++;
    else if (r.match_status === 'amended')                               counts.amended++;
    else if (r.match_status === 'matched' && r.human_reviewed === true)  counts.human++;
    else if (r.match_status === 'matched' && r.human_reviewed === false) counts.auto++;
    else if (r.match_status === 'review'  && r.human_reviewed === false) counts.review++;
  });
  Object.entries(counts).forEach(([k,v]) => {
    const el = $('mtab-n-'+k); if (el) el.textContent = v.toLocaleString();
  });
  const nb = $('nav-review-badge');
  if (nb) { nb.textContent = counts.review; nb.style.display = counts.review > 0 ? '' : 'none'; }
}

function filterReview() {
  const q    = ($('review-search')?.value||'').toLowerCase().trim();
  const comp = $('review-comp-filter')?.value || '';

  let rows = reviewAllRows.filter(r => {
    if (matchTab === 'rejected') return r.match_status === 'rejected';
    if (matchTab === 'human')    return r.match_status === 'matched' && r.human_reviewed === true;
    if (matchTab === 'auto')     return r.match_status === 'matched' && r.human_reviewed === false;
    if (matchTab === 'amended')  return r.match_status === 'amended';
    return r.match_status === 'review' && r.human_reviewed === false;
  });

  if (q) rows = rows.filter(r =>
    r.sku_id?.toLowerCase().includes(q) ||
    (r.skus?.short_title||'').toLowerCase().includes(q) ||
    (r.competitors?.name||'').toLowerCase().includes(q) ||
    (r.competitor_title||'').toLowerCase().includes(q)
  );
  if (comp) rows = rows.filter(r => r.competitors?.name === comp);

  const showing = rows.length;
  const tabLabel = {review:'needs review',auto:'AI matched',human:'confirmed',amended:'needs rescrape',rejected:'rejected'}[matchTab];
  $('review-sub').textContent = (q||comp) ? `${showing} matches (filtered)` : `${showing} ${tabLabel}`;

  if (!rows.length) {
    const msgs = {
      review:   ['ti-circle-check','var(--grn)','All caught up — no matches need review'],
      auto:     ['ti-robot','var(--blu)','No AI-matched pairs yet'],
      human:    ['ti-user-check','var(--grn)','No confirmed matches yet'],
      amended:  ['ti-clock','var(--amb)','No amended URLs awaiting rescrape'],
      rejected: ['ti-x','var(--t3)','No rejected matches'],
    };
    const [icon, color, text] = msgs[matchTab] || ['ti-circle-check','var(--t2)','Nothing here'];
    $('review-tbody').innerHTML = `<tr><td colspan="13"><div style="text-align:center;color:var(--t2);padding:40px">
      <i class="ti ${icon}" style="font-size:28px;display:block;margin-bottom:8px;color:${color}"></i>
      ${q||comp ? 'No matches for that filter' : text}
    </div></td></tr>`;
    $('review-pagination').innerHTML = '';
    return;
  }

  reviewData = rows;
  renderReviewPage();
}

function renderReviewPage() {
  const lim   = reviewLimit === 999999 ? reviewData.length : reviewLimit;
  const start = (reviewPage-1) * lim;
  const page  = reviewData.slice(start, start+lim);
  const from  = start+1, to = start+page.length;

  renderReviewRows(page);

  $('review-pagination').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>${from}–${to} of ${reviewData.length}</span>
      <select onchange="reviewLimit=+this.value===0?999999:+this.value;reviewPage=1;renderReviewPage()"
        style="padding:3px 6px;border-radius:5px;border:1px solid var(--bm);background:var(--surface);font-size:11px">
        <option value="50">50/page</option><option value="100">100/page</option><option value="250">250/page</option>
      </select>
    </div>
    <div style="display:flex;gap:6px">
      ${reviewPage>1 ? `<button class="btn sm" onclick="reviewPage--;renderReviewPage()">← Prev</button>` : ''}
      ${to<reviewData.length ? `<button class="btn sm" onclick="reviewPage++;renderReviewPage()">Next →</button>` : ''}
    </div>`;
}

function renderReviewRows(rows) {
  $('review-tbody').innerHTML = rows.map(r => {
    const sku   = r.skus        || {};
    const comp  = r.competitors || {};
    const snap  = r._snap || {};

    const ourPriceEx = sku.price_ex_vat ? parseFloat(sku.price_ex_vat) : null;
    const unitQty    = sku.unit_qty && sku.unit_qty > 1 ? sku.unit_qty : null;
    const ourPerUnit = (unitQty && ourPriceEx) ? ourPriceEx / unitQty : null;
    const ourUrl     = sku.product_url || (sku.slug ? `https://www.ukpos.com/${sku.slug}?vat=0` : '#');
    const ourThumb   = thumbUrl(sku.image_url||'', 50, 50);

    const theirRaw  = snap.competitor_price ? parseFloat(snap.competitor_price) : null;
    const theirVat  = snap.competitor_vat || snap.competitor_vat_default || comp.vat_status || 'unknown';
    const theirEx   = theirRaw ? normalisePrice(theirRaw, theirVat) : null;
    const theirPerU = (unitQty && theirEx) ? theirEx / unitQty : null;

    const diff = snap.diff_pct_normalised ?? snap.diff_pct;

    const statusPill = {
      review:   `<span style="background:var(--ab);color:var(--amb);border-radius:4px;padding:2px 7px;font-size:11px;font-weight:500">Needs review</span>`,
      matched:  r.human_reviewed
                ? `<span style="background:var(--gb);color:var(--grn);border-radius:4px;padding:2px 7px;font-size:11px;font-weight:500">✓ Confirmed</span>`
                : `<span style="background:var(--bb);color:var(--blu);border-radius:4px;padding:2px 7px;font-size:11px;font-weight:500">🤖 AI matched</span>`,
      rejected: `<span style="background:var(--bg);color:var(--t3);border-radius:4px;padding:2px 7px;font-size:11px;border:1px solid var(--border)">Rejected</span>`,
      amended:  `<span style="background:var(--ab);color:var(--amb);border-radius:4px;padding:2px 7px;font-size:11px;font-weight:500"><i class="ti ti-clock" style="font-size:11px"></i> Needs rescrape</span>`,
    }[r.match_status] || '—';

    /* ── Action cell ── */
    const actions = matchTab === 'amended' ? `
      <div style="font-size:11px;color:var(--amb);padding:4px 0;display:flex;align-items:center;gap:6px">
        <i class="ti ti-clock" style="font-size:13px"></i>
        Queued for rescrape
        ${r.previous_url ? `<span style="font-size:10px;color:var(--t3);margin-left:4px" title="Previous URL: ${r.previous_url}">↩ URL updated</span>` : ''}
      </div>` : matchTab === 'review' ? `
      <div style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="btn sm prim"   style="font-size:11px;padding:3px 7px" onclick="event.stopPropagation();quickApprove(${r.id},this)"><i class="ti ti-check"></i> Confirm</button>
        <button class="btn sm"        style="font-size:11px;padding:3px 7px" onclick="event.stopPropagation();showUpdateUrl(${r.id})"><i class="ti ti-link"></i> Update URL</button>
        <button class="btn sm danger" style="font-size:11px;padding:3px 7px" onclick="event.stopPropagation();quickRejectNoProduct(${r.id},this)" title="Competitor doesn't sell this product"><i class="ti ti-x"></i> Reject</button>
      </div>
      <div id="rrow-update-${r.id}" style="display:none;margin-top:6px">
        <div style="font-size:11px;color:var(--t2);margin-bottom:4px">Paste the correct competitor product URL:</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <input id="rrow-url-${r.id}" type="text" value="${(r.competitor_url||'').replace(/"/g,'&quot;')}" placeholder="https://competitor.com/product"
            autocomplete="new-password" spellcheck="false"
            style="flex:1;min-width:200px;padding:5px 8px;font-size:11px;border:1px solid var(--bm);border-radius:var(--r);font-family:inherit;outline:none">
          <button class="btn sm prim"  style="font-size:11px" onclick="event.stopPropagation();quickReject(${r.id},this,true)"><i class="ti ti-device-floppy"></i> Save</button>
          <button class="btn sm ghost" style="font-size:11px" onclick="event.stopPropagation();hideUpdateUrl(${r.id})">Cancel</button>
        </div>
      </div>` : matchTab !== 'rejected' ? `
      <button class="btn sm ghost" style="font-size:11px" onclick="event.stopPropagation();revertToReview(${r.id},this)">
        <i class="ti ti-rotate-clockwise"></i> Re-review
      </button>` : '';

    return `<tr class="${rowClass(diff)} tr-link" id="rrow-${r.id}" onclick="go('sku-detail',{skuId:'${r.sku_id}',fromPanel:'review'})">
      <td style="padding:4px 8px;width:58px">
        <div style="width:50px;height:50px;border:1px solid var(--border);border-radius:5px;overflow:hidden;background:var(--bb);display:flex;align-items:center;justify-content:center;flex-shrink:0">
          ${ourThumb ? `<img src="${ourThumb}" style="width:100%;height:100%;object-fit:contain" onerror="this.style.display='none'">` : `<i class="ti ti-photo" style="font-size:16px;color:var(--t3)"></i>`}
        </div>
      </td>
      <td style="min-width:140px">
        <a href="${ourUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()"
          style="display:inline-flex;align-items:center;gap:4px;font-family:'SF Mono',monospace;font-size:12px;color:var(--blu);text-decoration:none;font-weight:600">
          ${r.sku_id}<i class="ti ti-arrow-up-right" style="font-size:13px"></i>
        </a>
        <a href="${ourUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()"
          style="font-size:12px;color:var(--t2);display:block;line-height:1.3;text-decoration:none;margin-top:2px">
          ${sku.short_title||''}
        </a>
      </td>
      <td style="white-space:nowrap;font-weight:500">
        ${ourPriceEx ? fmtPrice(ourPriceEx) : '—'} <span class="vat vex" style="font-size:10px">ex</span>
        ${ourPerUnit ? `<div style="font-size:11px;font-weight:600;background:#fef08a;color:#854d0e;border-radius:3px;padding:1px 5px;display:inline-block;margin-top:2px">${fmtPrice(ourPerUnit)}/unit</div>` : ''}
      </td>
      <td style="min-width:140px;padding:0">
        ${r.competitor_url
          ? `<a href="${r.competitor_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()"
              style="display:block;padding:8px 10px;text-decoration:none;height:100%">
              <div style="font-weight:600;color:var(--blu);display:flex;align-items:center;gap:4px">
                ${comp.name||'—'}<i class="ti ti-arrow-up-right" style="font-size:13px;flex-shrink:0"></i>
              </div>
              <div style="font-size:11px;color:var(--t3);margin-top:2px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${r.competitor_url.replace('https://','').replace('http://','').slice(0,50)}
              </div>
             </a>`
          : `<div style="padding:8px 10px">
              <div style="font-weight:500;color:var(--t2)">${comp.name||'—'}</div>
              <div style="font-size:11px;color:var(--t3);margin-top:2px">No URL set</div>
             </div>`}
      </td>
      <td style="white-space:nowrap;font-weight:500">
        ${theirEx
          ? `${fmtPrice(theirEx)} ${vatPill(theirVat)}
             ${theirPerU ? `<div style="font-size:11px;font-weight:600;background:#fef08a;color:#854d0e;border-radius:3px;padding:1px 5px;display:inline-block;margin-top:2px">${fmtPrice(theirPerU)}/unit</div>` : ''}`
          : `<span style="color:var(--t3);font-size:12px">Not yet scraped</span>`}
      </td>
      <td>${diff != null ? `<span class="${diffClass(diff)}">${diffLabel(diff)}</span>` : '—'}</td>
      <td>${confWidget(r.confidence)}</td>
      <td onclick="event.stopPropagation()" style="min-width:220px">${actions}</td>
      <td style="white-space:nowrap">${statusPill}</td>
      <td style="font-size:12px;color:var(--t3);white-space:nowrap">${ts(r.reviewed_at||r.updated_at)}</td>
      <td>${stockBadge(snap.availability)}</td>
      <td style="font-size:12px;color:var(--t3);white-space:nowrap">${ts(snap.scraped_at)}</td>
      <td><button class="btn sm ghost" onclick="event.stopPropagation();openDrawer('${r.sku_id}','${(sku.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(ourPriceEx)}','review')"
        style="font-size:11px;padding:4px 8px">Quick view</button></td>
    </tr>`;
  }).join('');
}

function sortReviewTable(col) {
  const dir = (sortState.review?.col === col && sortState.review?.dir === 1) ? -1 : 1;
  sortState.review = { col, dir };
  updateSortHeaders('review-table', 'review', col);
  const getV = r => {
    const sku  = r.skus || {};
    const comp = r.competitors || {};
    const snap = r._snap || {};
    if (col === 'sku_id')          return r.sku_id||'';
    if (col === 'short_title')     return (sku.short_title||'').toLowerCase();
    if (col === 'competitor_name') return (comp.name||'').toLowerCase();
    if (col === 'our_price')       return parseFloat(sku.price_ex_vat||0);
    if (col === 'their_price')     { const raw = snap.competitor_price ? parseFloat(snap.competitor_price) : null; return raw ? normalisePrice(raw, snap.competitor_vat||'unknown') : 999999; }
    if (col === 'diff')            return parseFloat(snap.diff_pct_normalised ?? snap.diff_pct ?? 0);
    if (col === 'confidence')      return parseFloat(r.confidence||0);
    if (col === 'match_source')    return r.match_source||'';
    if (col === 'reviewed_at')     return r.reviewed_at ? new Date(r.reviewed_at).getTime() : 0;
    if (col === 'availability')    return (snap.availability||'').toLowerCase();
    if (col === 'scraped_at')      return snap.scraped_at ? new Date(snap.scraped_at).getTime() : 0;
    return '';
  };
  reviewData.sort((a,b) => cmpVal(getV(a), getV(b), dir));
  reviewPage = 1;
  renderReviewPage();
}

function showUpdateUrl(id)  { $(`rrow-update-${id}`).style.display = ''; $(`rrow-url-${id}`)?.focus(); }
function hideUpdateUrl(id)  { $(`rrow-update-${id}`).style.display = 'none'; }
function showRejectOptions(id) { showUpdateUrl(id); }
function hideRejectOptions(id) { hideUpdateUrl(id); }

async function quickRejectNoProduct(matchId, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i>';
  try {
    await authPost(`/review/${matchId}`, { decision: 'reject' });
    await sb.from('competitor_matches').update({
      match_source: 'human', human_reviewed: true,
      reviewed_at:  new Date().toISOString(),
      competitor_url: null,
    }).eq('id', matchId);
    const r = reviewAllRows.find(r => r.id === matchId);
    if (r) { r.match_status='rejected'; r.human_reviewed=true; r.match_source='human'; r.reviewed_at=new Date().toISOString(); r.competitor_url=null; }
    const row = $(`rrow-${matchId}`);
    if (row) row.remove();
    updateMatchTabCounts(); filterReview(); loadDashboard();
  } catch(e) { btn.disabled=false; btn.innerHTML=orig; alert(e.message); }
}

async function quickApprove(matchId, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i>';
  try {
    await authPost(`/review/${matchId}`, { decision: 'approve' });
    await sb.from('competitor_matches').update({ match_source:'human', human_reviewed:true, reviewed_at:new Date().toISOString() }).eq('id', matchId);
    const r = reviewAllRows.find(r => r.id === matchId);
    if (r) { r.match_status='matched'; r.human_reviewed=true; r.match_source='human'; r.reviewed_at=new Date().toISOString(); }
    const row = $(`rrow-${matchId}`);
    if (row) row.remove();
    updateMatchTabCounts(); filterReview(); loadDashboard();
  } catch(e) { btn.disabled=false; btn.innerHTML=orig; alert(e.message); }
}

async function quickReject(matchId, btn, saveUrl) {
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i>';
  try {
    const urlInput = $(`rrow-url-${matchId}`);
    const newUrl   = saveUrl && urlInput ? urlInput.value.trim() : null;

    if (newUrl && newUrl.startsWith('http')) {
      // URL provided — route through /correct: status becomes 'amended', not 'rejected'
      const json = await authPost(`/review/${matchId}/correct`, { url: newUrl });
      if (!json.ok) throw new Error(json.error || 'Save failed');
      const r = reviewAllRows.find(r => r.id === matchId);
      if (r) { r.match_status='amended'; r.human_reviewed=true; r.competitor_url=newUrl; }
    } else {
      // No URL — straight reject
      await authPost(`/review/${matchId}`, { decision: 'reject' });
      await sb.from('competitor_matches').update({
        match_source:'human', human_reviewed:true, reviewed_at:new Date().toISOString()
      }).eq('id', matchId);
      const r = reviewAllRows.find(r => r.id === matchId);
      if (r) { r.match_status='rejected'; r.human_reviewed=true; r.match_source='human'; r.reviewed_at=new Date().toISOString(); }
    }

    const row = $(`rrow-${matchId}`);
    if (row) row.remove();
    updateMatchTabCounts(); filterReview(); loadDashboard();
  } catch(e) { btn.disabled=false; btn.innerHTML=orig; alert(e.message); }
}

async function revertToReview(matchId, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i>';
  try {
    await sb.from('competitor_matches').update({ match_status:'review', match_source:'scraper_search', human_reviewed:false, reviewed_at:null }).eq('id', matchId);
    const r = reviewAllRows.find(r => r.id === matchId);
    if (r) { r.match_status='review'; r.human_reviewed=false; r.match_source='scraper_search'; r.reviewed_at=null; }
    updateMatchTabCounts(); filterReview();
  } catch(e) { btn.disabled=false; btn.innerHTML=orig; alert(e.message); }
}

async function reviewDecision(matchId, decision, btn) {
  btn.disabled = true;
  try {
    await authPost(`/review/${matchId}`, { decision });
    await sb.from('competitor_matches').update({
      match_source:   'human',
      human_reviewed: true,
      reviewed_at:    new Date().toISOString(),
    }).eq('id', matchId);
    const row = $(`rev-${matchId}`);
    if (row) { row.style.opacity = '.4'; row.style.pointerEvents = 'none'; btn.textContent = decision === 'approve' ? '✓ Confirmed' : 'Rejected'; }
    reviewAllRows = reviewAllRows.filter(r => r.id !== matchId);
    loadDashboard();
  } catch (e) { btn.disabled = false; alert(e.message); }
}

function skipReview(matchId) {
  const row = $(`rev-${matchId}`);
  if (row) row.style.display = 'none';
}

function showCorrectUrl(matchId) {
  $(`rev-actions-${matchId}`).style.display = 'none';
  $(`rev-url-wrap-${matchId}`).style.display = 'block';
  const input = $(`rev-url-${matchId}`);
  if (input) input.focus();
}

function cancelReject(matchId) {
  $(`rev-actions-${matchId}`).style.display = 'flex';
  $(`rev-url-wrap-${matchId}`).style.display = 'none';
}

async function saveCorrectUrl(matchId, btn) {
  const input  = $(`rev-url-${matchId}`);
  const newUrl = (input?.value || '').trim();
  if (!newUrl || !newUrl.startsWith('http')) {
    input.style.borderColor = 'var(--red)';
    input.placeholder = 'Must be a valid URL starting with https://…';
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="ti ti-loader"></i> Saving…';
  try {
    // POST to edge function — sets status='amended', saves previous_url,
    // sets awaiting_scrape=true. Row leaves review queue immediately.
    const json = await authPost(`/review/${matchId}/correct`, { url: newUrl });
    if (!json.ok) throw new Error(json.error || 'Save failed');
    // Optimistic removal from review queue DOM
    const row = $(`rev-${matchId}`);
    if (row) row.remove();
    // Update local cache so tab counts update correctly
    const cached = reviewAllRows.find(r => r.id === matchId);
    if (cached) { cached.match_status = 'amended'; cached.human_reviewed = true; cached.competitor_url = newUrl; }
    updateMatchTabCounts();
    filterReview();
    loadDashboard();
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = orig;
    alert('Save failed: ' + e.message);
  }
}

/* ════════════════════════════════════════
   BY CATEGORY
════════════════════════════════════════ */
let allSkus = [], catL4 = null, catL5 = null;

async function loadByCategory() {
  if (!allSkus.length) {
    const { data } = await sb.from('skus').select('sku_id,short_title,price_ex_vat,product_url,availability,cat_l4,cat_l5,image_url,slug').eq('active',true).order('sku_id');
    allSkus = data || [];
  }
  catL4 = null; catL5 = null;
  renderCategoryLevel();
}

let catPage = 1;
const catPageSize = 50;

function renderCategoryLevel() {
  const sub     = $('bycat-sub');
  const content = $('bycat-content');

  if (!catL4) {
    catPage = 1;
    sub.textContent = `${allSkus.length.toLocaleString()} SKUs across ${new Set(allSkus.map(s=>s.cat_l4).filter(Boolean)).size} categories`;
    const counts = {};
    allSkus.forEach(s => { if (s.cat_l4) counts[s.cat_l4] = (counts[s.cat_l4]||0) + 1; });
    const cats = Object.entries(counts).sort((a,b)=>a[0].localeCompare(b[0]));
    content.innerHTML = `<div class="cat-grid">${cats.map(([name, count]) => {
      const safe = name.replace(/'/g,"\\'");
      return `<div class="cat-tile" onclick="selectCat4('${safe}')">
        <div style="font-weight:500;font-size:14px;margin-bottom:3px">${name}</div>
        <div class="ct-count">${count} SKUs</div>
      </div>`;
    }).join('')}</div>`;
    return;
  }

  const skusInCat = allSkus.filter(s => s.cat_l4 === catL4 && (!catL5 || (s.cat_l5||'(other)') === catL5));
  const sub5s = [...new Set(allSkus.filter(s=>s.cat_l4===catL4&&s.cat_l5).map(s=>s.cat_l5))].sort();

  const breadcrumb = `
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:12px">
      <button class="btn sm ghost" onclick="catL4=null;catL5=null;catPage=1;renderCategoryLevel()"><i class="ti ti-arrow-left"></i> All categories</button>
      <span style="color:var(--t3)">/</span>
      ${catL5
        ? `<span onclick="catL5=null;catPage=1;renderCategoryLevel()" style="cursor:pointer;color:var(--blu)">${catL4}</span>
           <span style="color:var(--t3)">/</span>
           <span style="font-weight:500">${catL5}</span>`
        : `<span style="font-weight:500">${catL4}</span>`}
    </div>`;

  let subTiles = '';
  if (!catL5 && sub5s.length > 1) {
    const counts = {};
    allSkus.filter(s=>s.cat_l4===catL4).forEach(s=>{ const k=s.cat_l5||'(other)'; counts[k]=(counts[k]||0)+1; });
    subTiles = `<div class="cat-grid" style="margin-bottom:16px">${
      Object.entries(counts).sort((a,b)=>a[0].localeCompare(b[0])).map(([name,count]) => {
        const safe = name.replace(/'/g,"\\'");
        return `<div class="cat-tile" onclick="selectCat5('${safe}')">
          <div style="font-weight:500;font-size:14px;margin-bottom:3px">${name}</div>
          <div class="ct-count">${count} SKUs</div>
        </div>`;
      }).join('')
    }</div>`;
  }

  sub.textContent = `${skusInCat.length} SKUs${catL5 ? ' in '+catL5 : ' in '+catL4}`;
  const start  = (catPage - 1) * catPageSize;
  const page   = skusInCat.slice(start, start + catPageSize);
  const from   = start + 1;
  const to     = start + page.length;

  const table = `
    <div style="font-size:13px;color:var(--t2);margin-bottom:8px">${from}–${to} of ${skusInCat.length} SKUs</div>
    <div class="card-0p">
      <table><thead><tr>
        <th style="border-left:3px solid transparent">SKU ID</th>
        <th>Product</th>
        <th>Our price</th>
        <th>Stock</th>
        <th style="width:50px"></th>
      </tr></thead><tbody>${page.map(s => `
        <tr class="tr-link" onclick="go('sku-detail',{skuId:'${s.sku_id}',fromPanel:'bycat'})">
          <td><span style="font-family:'SF Mono',monospace;color:var(--blu)">${s.sku_id}</span></td>
          <td style="color:var(--text)">${s.short_title||''}</td>
          <td style="font-weight:500">${fmtPrice(s.price_ex_vat)} <span class="vat vex">ex</span></td>
          <td>${stockBadge(s.availability)}</td>
          <td>→</td>
        </tr>`).join('')}
      </tbody></table>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;font-size:13px;color:var(--t2)">
      <span>${from}–${to} of ${skusInCat.length}</span>
      <div style="display:flex;gap:6px">
        ${catPage>1 ? `<button class="btn sm" onclick="catPage--;renderCategoryLevel()">← Prev</button>` : ''}
        ${to<skusInCat.length ? `<button class="btn sm" onclick="catPage++;renderCategoryLevel()">Next →</button>` : ''}
      </div>
    </div>`;

  content.innerHTML = breadcrumb + subTiles + table;
}

function selectCat4(name) { catL4 = name; catL5 = null; catPage = 1; renderCategoryLevel(); }
function selectCat5(name) { catL5 = name; catPage = 1; renderCategoryLevel(); }

/* ════════════════════════════════════════
   BY COMPETITOR (list)
════════════════════════════════════════ */
async function loadByCompetitor() {
  try {
    const [{ data: comps }, { data: snaps }] = await Promise.all([
      sb.from('competitors').select('id,name,domain,vat_status,active').eq('active',true).order('name'),
      sb.from('latest_snapshots').select('competitor_id,diff_pct_normalised,diff_pct,competitor_price').not('competitor_price','is',null)
    ]);

    if (!comps?.length) {
      $('bycomp-tbody').innerHTML = '<tr><td colspan="8" style="padding:20px;text-align:center;color:var(--t2)">No competitors found</td></tr>';
      return;
    }

    const stats = {};
    (snaps || []).forEach(s => {
      const cid  = s.competitor_id;
      const diff = s.diff_pct_normalised ?? s.diff_pct ?? 0;
      if (!stats[cid]) stats[cid] = { matched:0, critical:0, warning:0, cheaper:0, parity:0 };
      stats[cid].matched++;
      if      (diff <= -T.red)               stats[cid].critical++;
      else if (diff <= -T.amb)               stats[cid].warning++;
      else if (diff >=  T.par)               stats[cid].cheaper++;
      else                                   stats[cid].parity++;
    });

    bycompData = comps.map(c => ({
      ...c,
      _matched:  stats[c.id]?.matched  ?? 0,
      _critical: stats[c.id]?.critical ?? 0,
      _warning:  stats[c.id]?.warning  ?? 0,
      _cheaper:  stats[c.id]?.cheaper  ?? 0,
      _parity:   stats[c.id]?.parity   ?? 0,
    }));

    sortState.bycomp = { col: null, dir: 1 };
    renderByCompRows(bycompData);
    updateSortHeaders('bycomp-table', 'bycomp', null);
  } catch (e) {
    $('bycomp-tbody').innerHTML = `<tr><td colspan="8" style="color:var(--red);padding:8px">${e.message}</td></tr>`;
  }
}

/* ════════════════════════════════════════
   COMPETITOR DETAIL PAGE
════════════════════════════════════════ */
async function loadCompDetail(opts) {
  currentCompId   = opts.compId;
  currentCompName = opts.compName;
  currentCompSlug = opts.compSlug || slugify(opts.compName||'');
  const domain    = opts.compDomain || '';
  const vat       = opts.compVat || 'unknown';

  $('comp-detail-url').textContent = `pricewatch.ukpos.com/competitor/${currentCompSlug}`;
  $('comp-detail-name').textContent = currentCompName;
  $('comp-detail-sub').textContent = `${domain} · ${vat}-VAT`;
  history.replaceState(null, '', '#competitor/' + currentCompSlug);

  buildLegend('comp-detail-legend');

  $('comp-detail-stats').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
  $('comp-sku-grid').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
  $('comp-sku-tbody').innerHTML = '<tr><td colspan="8"><div class="loading"><span class="spinner"></span></div></td></tr>';

  try {
    const { data: snaps } = await sb.from('latest_snapshots').select('*').eq('competitor_id', currentCompId).not('competitor_price','is',null).order('diff_pct_normalised', {ascending:true, nullsFirst:false});
    compSkusAll = snaps || [];
    compSkusFiltered = [...compSkusAll];
    sortState.compSku = { col: null, dir: 1 };
    updateSortHeaders('comp-sku-table', 'compSku', null);
    renderCompDetail();
  } catch (e) {
    $('comp-sku-grid').innerHTML = `<div style="color:var(--red);padding:8px">${e.message}</div>`;
  }

  const defaultGrid = $('pref-default-grid')?.checked !== false;
  setCompView(defaultGrid ? 'grid' : 'list');
}

function renderCompDetail() {
  const skus = compSkusFiltered;
  let crit=0, warn=0, par=0, good=0;
  skus.forEach(s => {
    const t = getTier(s.diff_pct_normalised??s.diff_pct);
    if(t==='r')crit++; else if(t==='a')warn++; else if(t==='g')good++; else par++;
  });

  $('comp-detail-stats').innerHTML = `
    <div class="comp-stat"><div class="cs-val">${skus.length}</div><div class="cs-label">Matched SKUs</div></div>
    <div class="comp-stat"><div class="cs-val" style="color:var(--red)">${crit}</div><div class="cs-label">Critical</div></div>
    <div class="comp-stat"><div class="cs-val" style="color:var(--amb)">${warn}</div><div class="cs-label">Warning</div></div>
    <div class="comp-stat"><div class="cs-val" style="color:var(--t2)">${par}</div><div class="cs-label">Parity</div></div>
    <div class="comp-stat"><div class="cs-val" style="color:var(--grn)">${good}</div><div class="cs-label">We're cheaper</div></div>`;

  $('comp-sku-grid').innerHTML = skus.length ? skus.map(s => {
    const diff = s.diff_pct_normalised ?? s.diff_pct;
    const tier = getTier(diff);
    const raw  = s.competitor_price ? parseFloat(s.competitor_price) : null;
    const vat  = s.competitor_vat || s.competitor_vat_default || 'unknown';
    const ex   = raw ? normalisePrice(raw, vat) : null;
    return `<div class="sku-card tier-${tier}" onclick="openDrawer('${s.sku_id}','${(s.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(s.our_price)}','comp-detail')">
      <div class="sc-id">${s.sku_id}</div>
      <div class="sc-name">${s.short_title||''}</div>
      <div class="sc-prices"><span>Ours: ${fmtPrice(s.our_price)}</span><span>Theirs: ${ex?fmtPrice(ex):'—'}</span></div>
      <div class="sc-diff">${diffLabel(diff)}</div>
      ${s.availability==='out_of_stock'?'<div class="sc-oos">OOS at competitor</div>':''}
    </div>`;
  }).join('') : '<div style="color:var(--t2);font-size:12px;padding:20px;text-align:center">No matched SKUs found</div>';

  $('comp-sku-tbody').innerHTML = skus.length ? skus.map(s => {
    const diff = s.diff_pct_normalised ?? s.diff_pct;
    const raw  = s.competitor_price ? parseFloat(s.competitor_price) : null;
    const vat  = s.competitor_vat || s.competitor_vat_default || 'unknown';
    const ex   = raw ? normalisePrice(raw, vat) : null;
    return `<tr class="${rowClass(diff)} tr-link" onclick="openDrawer('${s.sku_id}','${(s.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(s.our_price)}','comp-detail')">
      <td><span style="font-family:'SF Mono',monospace;font-size:11px;color:var(--blu)">${s.sku_id}</span></td>
      <td style="font-size:11px;color:var(--t2);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.short_title||''}</td>
      <td style="font-weight:500">${fmtPrice(s.our_price)}</td>
      <td style="font-weight:500">${ex?fmtPrice(ex):'—'}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span></td>
      <td>${stockBadge(s.availability)}</td>
      <td style="font-size:10px;color:var(--t3)">${ts(s.scraped_at)}</td>
      <td><button class="btn sm ghost" onclick="event.stopPropagation();openDrawer('${s.sku_id}','${(s.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(s.our_price)}','comp-detail')">→</button></td>
    </tr>`;
  }).join('') : '<tr><td colspan="8" style="text-align:center;color:var(--t2);padding:20px">No matched SKUs</td></tr>';
}

function setCompView(mode) {
  compViewMode = mode;
  $('comp-grid-wrap').style.display = mode === 'grid' ? '' : 'none';
  $('comp-list-wrap').style.display = mode === 'list' ? '' : 'none';
  $('tog-grid').classList.toggle('active', mode === 'grid');
  $('tog-list').classList.toggle('active', mode === 'list');
}

function filterCompSKUs(q) {
  compSkusFiltered = q
    ? compSkusAll.filter(s => s.sku_id.toLowerCase().includes(q.toLowerCase()) || (s.short_title||'').toLowerCase().includes(q.toLowerCase()))
    : [...compSkusAll];
  renderCompDetail();
}

/* ════════════════════════════════════════
   SKU DETAIL PAGE
════════════════════════════════════════ */
async function loadSkuDetail(opts) {
  currentSkuId = opts.skuId;
  $('sku-detail-url').textContent = `pricewatch.ukpos.com/sku/${currentSkuId}`;
  history.replaceState(null, '', '#sku/' + currentSkuId);
  buildLegend('sku-detail-legend');

  $('sku-hero-content').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
  $('sku-comp-tbody').innerHTML = '<tr><td colspan="8"><div class="loading"><span class="spinner"></span></div></td></tr>';

  const backBtn = $('sku-detail-back');
  if (opts.fromPanel === 'comp-detail' && currentCompName) {
    backBtn.innerHTML = `<i class="ti ti-arrow-left"></i> ${currentCompName}`;
    backBtn.onclick = () => go('comp-detail', { compId: currentCompId, compName: currentCompName, compSlug: currentCompSlug });
  } else if (opts.fromPanel === 'bycat') {
    backBtn.innerHTML = '<i class="ti ti-arrow-left"></i> Category';
    backBtn.onclick = () => go('bycat');
  } else {
    backBtn.innerHTML = '<i class="ti ti-arrow-left"></i> All SKUs';
    backBtn.onclick = () => go('skus');
  }

  try {
    const { data: skuArr } = await sb.from('skus').select('*').eq('sku_id', currentSkuId).limit(1);
    const sku = skuArr?.[0];
    if (!sku) throw new Error('SKU not found');

    const ourPriceEx  = parseFloat(sku.price_ex_vat || 0);
    const ourPriceInc = ourPriceEx * 1.2;
    const unitQty     = sku.unit_qty && sku.unit_qty > 1 ? sku.unit_qty : null;
    const ourPerUnit  = unitQty ? ourPriceEx / unitQty : null;
    const siteUrl = sku.product_url || (sku.slug ? `https://www.ukpos.com/${sku.slug}?vat=0` : '#');
    $('sku-site-link').href = siteUrl;

    $('sku-hero-content').innerHTML = `
      <div class="sku-img">${sku.image_url
        ? `<img src="${thumbUrl(sku.image_url,144,144)}" style="width:100%;height:100%;object-fit:contain;border-radius:var(--r)" onerror="this.style.display='none'">`
        : `<i class="ti ti-photo" style="font-size:24px"></i>`}</div>
      <div style="flex:1">
        <div class="sku-id-badge">${sku.sku_id}</div>
        <div class="sku-name">${sku.short_title||sku.sku_id}</div>
        <div style="display:flex;align-items:baseline;gap:8px;margin:4px 0;flex-wrap:wrap">
          <span class="sku-price-main">${fmtPrice(ourPriceEx)}</span>
          <span class="vat vex">ex-VAT</span>
          <span style="font-size:12px;color:var(--t2)">${fmtPrice(ourPriceInc)} inc-VAT</span>
          ${ourPerUnit ? `<span style="font-size:12px;background:var(--ab);color:var(--amb);border-radius:4px;padding:1px 7px;font-weight:500">${fmtPrice(ourPerUnit)} per unit (pack of ${unitQty})</span>` : ''}
        </div>
        <div class="sku-attrs">
          ${sku.category ? `<span class="sku-attr">${sku.category}</span>` : ''}
          ${sku.material ? `<span class="sku-attr">${sku.material}</span>` : ''}
          ${sku.color    ? `<span class="sku-attr">${sku.color}</span>`    : ''}
          ${unitQty      ? `<span class="sku-attr" style="background:var(--ab);color:var(--amb);border-color:var(--abd)">Pack of ${unitQty}</span>` : ''}
          ${sku.mpn      ? `<span class="sku-attr">MPN: ${sku.mpn}</span>` : ''}
        </div>
      </div>`;

    const { data: snaps } = await sb.from('latest_snapshots').select('*,skus(unit_qty)').eq('sku_id', currentSkuId).order('diff_pct_normalised', {ascending:true, nullsFirst:false});
    const rows = snaps || [];

    if (!rows.length) {
      $('sku-comp-tbody').innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--t2);padding:20px">No competitor data yet</td></tr>';
      return;
    }

    skuCompData = rows;
    sortState.skuComp = { col: null, dir: 1 };
    updateSortHeaders('sku-comp-table', 'skuComp', null);
    renderSkuCompRows(rows);

  } catch (e) {
    $('sku-hero-content').innerHTML = `<div style="color:var(--red);padding:8px">${e.message}</div>`;
  }
}

/* ════════════════════════════════════════
   DRAWER
════════════════════════════════════════ */
async function openDrawer(skuId, name, price, fromPanel) {
  drawerSkuId  = skuId;
  drawerFromPanel = fromPanel;
  $('dr-id').textContent    = skuId;
  $('dr-name').textContent  = name;
  $('dr-price').textContent = price;

  const fpHandler = () => { closeDrawer(); go('sku-detail', { skuId, fromPanel }); };
  $('dr-fullpage-btn').onclick  = fpHandler;
  $('dr-fullpage-btn2').onclick = fpHandler;
  $('dr-scrape-btn').onclick = () => lookupSKU(skuId, $('dr-scrape-btn'));
  $('dr-review-btn').onclick = () => { closeDrawer(); go('review'); };

  $('drawer-overlay').classList.add('open');
  $('sku-drawer').classList.add('open');
  $('dr-rows').innerHTML = '<div class="loading"><span class="spinner"></span></div>';

  try {
    const { data: snaps } = await sb.from('latest_snapshots').select('*').eq('sku_id', skuId).order('diff_pct_normalised', {ascending:true, nullsFirst:false});
    const rows = snaps || [];
    if (!rows.length) {
      $('dr-rows').innerHTML = '<div style="color:var(--t2);font-size:12px;padding:8px">No competitor data yet — run a scrape first.</div>';
      return;
    }

    const ourPriceEx = rows[0]?.our_price ? parseFloat(rows[0].our_price) : null;

    const entries = rows
      .filter(r => r.competitor_price)
      .map(r => {
        const raw = parseFloat(r.competitor_price);
        const vat = r.competitor_vat || r.competitor_vat_default || 'unknown';
        const ex  = normalisePrice(raw, vat);
        return { name: r.competitor_name||'—', domain: r.competitor_domain||'', vat, ex, raw, diff: r.diff_pct_normalised??r.diff_pct, avail: r.availability, isUs: false };
      });

    if (ourPriceEx) {
      entries.push({ name: 'UKPOS', domain: 'ukpos.com', vat: 'ex', ex: ourPriceEx, raw: ourPriceEx, diff: 0, avail: 'in_stock', isUs: true });
    }

    entries.sort((a, b) => (a.ex||999999) - (b.ex||999999));

    $('dr-rows').innerHTML = entries.map((e, i) => {
      const rank = i + 1;
      const tier = e.isUs ? 'us' : getTier(e.diff);
      const tierColors = {
        r: { bg: 'var(--rb)', border: 'var(--rbd)', txt: 'var(--red)' },
        a: { bg: 'var(--ab)', border: 'var(--abd)', txt: 'var(--amb)' },
        g: { bg: 'var(--gb)', border: 'var(--gbd)', txt: 'var(--grn)' },
        p: { bg: 'var(--bg)', border: 'var(--border)', txt: 'var(--t2)' },
        us:{ bg: '#111110',   border: 'var(--orange)', txt: '#fff' },
      };
      const c = tierColors[tier] || tierColors.p;
      const rankColor = rank === 1 ? '#f59e0b' : e.isUs ? 'rgba(255,255,255,.5)' : 'var(--t3)';
      const diffHtml = e.isUs
        ? `<span style="font-size:10px;color:rgba(255,255,255,.4)">our price</span>`
        : `<span class="${diffClass(e.diff)}" style="${tier==='us'?'color:#fff':''}">${diffLabel(e.diff)}</span>`;
      const oosHtml = e.avail === 'out_of_stock' ? `<span style="font-size:10px;opacity:.55;margin-top:2px;display:block">OOS</span>` : '';

      return `<div style="display:grid;grid-template-columns:28px 1fr auto;align-items:center;gap:10px;padding:9px 12px;border-radius:var(--r);border:1px solid ${c.border};background:${c.bg};margin-bottom:6px${e.isUs?';box-shadow:0 0 0 2px var(--orange)':''}">
        <div style="font-size:13px;font-weight:700;color:${rankColor};text-align:center;line-height:1">${rank}</div>
        <div>
          <div style="font-weight:${e.isUs?'700':'500'};font-size:12px;color:${e.isUs?'#fff':'var(--text)'}">${e.name}</div>
          <div style="font-size:10px;color:${e.isUs?'rgba(255,255,255,.45)':'var(--t2)'}">${e.domain} ${e.isUs?'':vatPill(e.vat)}</div>
          ${oosHtml}
        </div>
        <div style="text-align:right">
          <div style="font-size:16px;font-weight:600;color:${e.isUs?'#fff':'var(--text)'}">${e.ex ? fmtPrice(e.ex) : '—'}</div>
          <div style="margin-top:2px">${diffHtml}</div>
        </div>
      </div>`;
    }).join('');

  } catch (e) {
    $('dr-rows').innerHTML = `<div style="color:var(--red);font-size:12px;padding:8px">${e.message}</div>`;
  }
}

function closeDrawer() {
  $('drawer-overlay').classList.remove('open');
  $('sku-drawer').classList.remove('open');
}

/* ════════════════════════════════════════
   SCHEDULE + RUN HISTORY
════════════════════════════════════════ */
async function loadRuns() {
  try {
    const [{ data: rows }, { data: progress }] = await Promise.all([
      sb.from('sync_runs').select('*').order('started_at', {ascending: false}).limit(20),
      sb.from('scrape_progress').select('batch_id,sku_id').order('attempted_at', {ascending: false}).limit(5000),
    ]);

    const totalSkus = 2634;
    if (progress?.length) {
      const batches = {};
      progress.forEach(p => { batches[p.batch_id] = (batches[p.batch_id]||0) + 1; });
      const batchLines = Object.entries(batches).map(([id, done]) => {
        const pct  = Math.round(done / totalSkus * 100);
        const remaining = totalSkus - done;
        const estDate   = new Date(Date.now() + (remaining/570)*24*60*60*1000);
        const estStr    = estDate.toLocaleDateString('en-GB',{day:'numeric',month:'short'});
        return `<div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span style="font-weight:500">Batch: ${id}</span>
            <span style="color:var(--t2)">${done.toLocaleString()} / ${totalSkus.toLocaleString()} SKUs · ${pct}% · est. complete ${estStr}</span>
          </div>
          <div style="height:8px;background:var(--bb);border-radius:4px;overflow:hidden">
            <div style="height:100%;width:${pct}%;background:var(--blu);border-radius:4px;transition:width .3s"></div>
          </div>
        </div>`;
      }).join('');
      $('run-summary-cards').insertAdjacentHTML('beforebegin', `
        <div class="card" style="margin-bottom:14px;padding:14px 16px">
          <div class="card-hd" style="padding:0 0 10px"><div class="card-title">Full crawl progress</div></div>
          ${batchLines}
        </div>`);
    }

    if (!rows?.length) {
      $('run-history').innerHTML = '<div style="color:var(--t2);padding:8px">No runs yet.</div>';
      return;
    }

    const last = rows.find(r => r.status === 'complete') || rows[0];
    const dur  = last.completed_at
      ? Math.round((new Date(last.completed_at) - new Date(last.started_at)) / 60000)
      : null;

    const cards = [
      { label: 'Pairs attempted',   val: (last.pairs_attempted||last.skus_attempted||0).toLocaleString(), icon: 'ti-refresh',      color: 'var(--blu)' },
      { label: 'Prices found',      val: (last.prices_found||0).toLocaleString(),                         icon: 'ti-currency-pound',color: 'var(--grn)' },
      { label: 'Confirmed matches', val: (last.matches_confirmed||0).toLocaleString(),                    icon: 'ti-circle-check', color: 'var(--grn)' },
      { label: 'In review queue',   val: (last.matches_review||last.review_queue||0).toLocaleString(),    icon: 'ti-eye-check',    color: 'var(--amb)' },
      { label: 'Failed',            val: (last.skus_failed||0).toLocaleString(),                          icon: 'ti-alert-circle', color: 'var(--red)' },
      { label: 'OOS flagged',       val: (last.oos_flagged||0).toLocaleString(),                          icon: 'ti-package-off',  color: 'var(--t2)'  },
      { label: 'Duration',          val: dur != null ? dur+'m' : '—',                                     icon: 'ti-clock',        color: 'var(--t2)'  },
      { label: 'Mode',              val: last.scrape_mode || last.trigger || '—',                         icon: 'ti-settings',     color: 'var(--t2)'  },
    ];
    $('run-summary-cards').innerHTML = cards.map(c => `
      <div class="card" style="padding:12px 14px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <i class="ti ${c.icon}" style="font-size:14px;color:${c.color}"></i>
          <span style="font-size:12px;color:var(--t2)">${c.label}</span>
        </div>
        <div style="font-size:22px;font-weight:600;color:${c.color}">${c.val}</div>
        <div style="font-size:11px;color:var(--t3);margin-top:2px">last run · ${ts(last.started_at)}</div>
      </div>`).join('');

    $('run-history-sub').textContent = `${rows.length} most recent runs`;
    $('run-history').innerHTML = `
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>
          <th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Started</th>
          <th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Mode</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Pairs</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Prices found</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Confirmed</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Review</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Failed</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">OOS</th>
          <th style="text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Duration</th>
          <th style="text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--t2)">Status</th>
        </tr></thead><tbody>
        ${rows.map(r => {
          const d    = r.completed_at ? Math.round((new Date(r.completed_at)-new Date(r.started_at))/60000)+'m' : '…';
          const bc   = r.status==='complete'?'b-g':r.status==='running'?'b-blu':'b-a';
          const pairs = r.pairs_attempted || r.skus_attempted || 0;
          const pricePct = pairs > 0 ? Math.round((r.prices_found||0)/pairs*100) : null;
          return `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:7px 10px">${ts(r.started_at)}</td>
            <td style="padding:7px 10px;color:var(--t2)">${r.scrape_mode||r.trigger||'—'}</td>
            <td style="padding:7px 10px;text-align:right">${pairs.toLocaleString()}</td>
            <td style="padding:7px 10px;text-align:right">
              ${(r.prices_found||0).toLocaleString()}
              ${pricePct!=null ? `<span style="font-size:11px;color:var(--t3);margin-left:4px">${pricePct}%</span>` : ''}
            </td>
            <td style="padding:7px 10px;text-align:right;color:var(--grn);font-weight:500">${(r.matches_confirmed||0).toLocaleString()}</td>
            <td style="padding:7px 10px;text-align:right;color:var(--amb)">${(r.matches_review||r.review_queue||0).toLocaleString()}</td>
            <td style="padding:7px 10px;text-align:right;color:${r.skus_failed>0?'var(--red)':'var(--t3)'}">${(r.skus_failed||0).toLocaleString()}</td>
            <td style="padding:7px 10px;text-align:right;color:var(--t2)">${(r.oos_flagged||0).toLocaleString()}</td>
            <td style="padding:7px 10px;text-align:right;color:var(--t2)">${d}</td>
            <td style="padding:7px 10px"><span class="badge ${bc}">${r.status||'—'}</span></td>
          </tr>`;
        }).join('')}
        </tbody></table>
      </div>`;
  } catch (e) {
    $('run-history').innerHTML = `<div style="color:var(--red);padding:8px">${e.message}</div>`;
  }
}

/* ════════════════════════════════════════
   SETTINGS — COMPETITORS
════════════════════════════════════════ */
async function loadCompetitorSettings() {
  try {
    const { data: comps } = await sb.from('competitors').select('*').order('name');
    if (!comps?.length) return;
    const unknown = comps.filter(c=>c.vat_status==='unknown').length;
    $('comp-sett-sub').textContent = `${comps.filter(c=>c.active).length} active · ${unknown} unknown VAT`;
    if (unknown > 0) {
      $('comp-vat-note-text').textContent = `${unknown} competitor${unknown>1?'s':''} have unknown VAT status — set these before trusting differentials.`;
      $('comp-vat-note').style.display = '';
    }
    $('comp-sett-tbody').innerHTML = comps.map(c => {
      const exCls  = c.vat_status==='ex'  ? 's-ex'  : '';
      const incCls = c.vat_status==='inc' ? 's-inc' : '';
      const unkCls = c.vat_status==='unknown' ? 's-unk' : '';
      const rowBg  = c.vat_status==='unknown' ? 'background:rgba(133,79,11,.03)' : '';
      return `<tr style="${rowBg}${!c.active?';opacity:.5':''}">
        <td style="font-family:'SF Mono',monospace;font-size:11px;color:var(--t3)">${c.id}</td>
        <td style="font-weight:500">${c.name}</td>
        <td style="font-size:11px;color:var(--t2)">${c.domain}</td>
        <td>
          <div class="vat-toggle" id="vtog-${c.id}">
            <button class="vt-opt ${exCls}"  onclick="setVatStatus(${c.id},'ex')">ex</button>
            <button class="vt-opt ${incCls}" onclick="setVatStatus(${c.id},'inc')">inc</button>
            <button class="vt-opt ${unkCls}" onclick="setVatStatus(${c.id},'unknown')">?</button>
          </div>
        </td>
        <td><span class="badge ${c.feed_url?'b-g':'b-gray'}" style="font-size:9px">${c.feed_url?'Feed':'Scrape'}</span></td>
        <td><input type="checkbox" ${c.active?'checked':''} style="accent-color:var(--orange)" onchange="setCompActive(${c.id},this.checked)"></td>
      </tr>`;
    }).join('');
  } catch (e) { $('comp-sett-tbody').innerHTML = `<tr><td colspan="6" style="color:var(--red);padding:8px">${e.message}</td></tr>`; }
}

async function setVatStatus(compId, val) {
  try {
    const { error } = await sb.from('competitors').update({ vat_status: val }).eq('id', compId);
    if (error) throw new Error(error.message);
    const tog = $('vtog-'+compId);
    if (tog) {
      tog.querySelectorAll('.vt-opt').forEach(b => {
        b.className = 'vt-opt';
        if (b.textContent.trim()==='ex'  && val==='ex')      b.className='vt-opt s-ex';
        if (b.textContent.trim()==='inc' && val==='inc')     b.className='vt-opt s-inc';
        if (b.textContent.trim()==='?'   && val==='unknown') b.className='vt-opt s-unk';
      });
    }
  } catch (e) { alert('VAT update failed: '+e.message); }
}

async function setCompActive(compId, active) {
  try {
    const { error } = await sb.from('competitors').update({ active }).eq('id', compId);
    if (error) throw new Error(error.message);
  } catch (e) { alert('Update failed: '+e.message); }
}

/* ════════════════════════════════════════
   SETTINGS — USERS
════════════════════════════════════════ */
async function loadUsers() {
  if (currentProfile?.role !== 'super_admin') {
    $('users-list').innerHTML = '<div style="color:var(--t2);font-size:12px;padding:12px">Admin access required to manage users.</div>';
    $('pending-list').innerHTML = '';
    return;
  }
  try {
    const { data: profiles } = await sb.from('profiles').select('*').eq('status','approved').order('requested_at');
    const { data: requests } = await sb.from('access_requests').select('*').order('requested_at');
    $('users-sub').textContent = `${(profiles||[]).length} active users`;

    $('users-list').innerHTML = (profiles||[]).map(p => {
      const initials = (p.full_name||p.email||'?').split(/[\s@]/)[0].slice(0,2).toUpperCase();
      const isMe = p.id === currentProfile.id;
      return `<div class="user-row" style="padding:10px 14px">
        <div class="avatar" style="${p.role==='super_admin'?'background:var(--os);color:var(--orange)':''}">${initials}</div>
        <div style="flex:1">
          <div style="font-weight:500;font-size:12px">${p.email}</div>
          <div style="font-size:11px;color:var(--t2)">${p.full_name||'—'} · ${p.role==='super_admin'?'Admin':'User'}</div>
        </div>
        <span class="badge ${p.role==='super_admin'?'':'b-g'}" style="${p.role==='super_admin'?'background:var(--os);color:var(--orange)':''}">${p.role==='super_admin'?'Admin':'Active'}</span>
        ${!isMe ? `<button class="btn sm ghost danger" onclick="revokeUser('${p.id}',this)"><i class="ti ti-user-off"></i></button>` : ''}
      </div>`;
    }).join('') || '<div style="color:var(--t2);font-size:12px;padding:12px">No approved users.</div>';

    $('pending-list').innerHTML = (requests||[]).length ? (requests||[]).map(r => `
      <div class="user-row" style="padding:10px 14px">
        <div class="avatar" style="background:var(--ab);color:var(--amb)">${(r.email||'?').slice(0,2).toUpperCase()}</div>
        <div style="flex:1">
          <div style="font-weight:500;font-size:12px">${r.email}</div>
          <div style="font-size:11px;color:var(--t2)">${r.full_name||'—'} · Requested ${ts(r.requested_at)}</div>
        </div>
        <button class="btn sm prim" onclick="approveUser('${r.email}',this)"><i class="ti ti-check"></i> Approve</button>
        <button class="btn sm danger" onclick="rejectUser('${r.email}',this)"><i class="ti ti-x"></i> Reject</button>
      </div>`).join('') :
      '<div style="text-align:center;padding:24px;color:var(--t3);font-size:12px"><i class="ti ti-circle-check" style="font-size:22px;display:block;margin-bottom:6px;color:var(--grn)"></i>No pending requests</div>';
  } catch (e) {
    $('users-list').innerHTML = `<div style="color:var(--red);font-size:12px;padding:12px">${e.message}</div>`;
  }
}

async function approveUser(email, btn) {
  btn.disabled = true;
  try {
    const { data: { session } } = await sb.auth.getSession();
    const res = await fetch(`${SUPABASE_URL}/functions/v1/approve-user`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + session.access_token },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) { const err = await res.json().catch(()=>{}); throw new Error(err?.error || 'Approve failed'); }
    await loadUsers();
  } catch (e) { alert('Approve failed: '+e.message); btn.disabled = false; }
}

async function rejectUser(email, btn) {
  if (!confirm(`Reject access request from ${email}?`)) return;
  btn.disabled = true;
  try {
    await sb.from('access_requests').delete().eq('email', email);
    await loadUsers();
  } catch (e) { alert('Reject failed: '+e.message); btn.disabled = false; }
}

async function revokeUser(id, btn) {
  if (!confirm('Revoke access for this user?')) return;
  btn.disabled = true;
  try {
    await sb.from('profiles').update({ status: 'rejected', rejected_at: new Date().toISOString() }).eq('id', id);
    await loadUsers();
  } catch (e) { alert('Revoke failed: '+e.message); btn.disabled = false; }
}

/* ════════════════════════════════════════
   THRESHOLDS (configurables)
════════════════════════════════════════ */
function updateThreshPreview() {
  const r = parseInt($('t-red').value) || 10;
  const a = parseInt($('t-amb').value) || 5;
  const p = parseInt($('t-par').value) || 2;
  $('t-grn-derived').textContent = p;
  $('prev-r').textContent = `-${(r+2.4).toFixed(1)}%`;
  $('prev-a').textContent = `-${((r+a)/2).toFixed(1)}%`;
  $('prev-p').textContent = `±${(p*0.6).toFixed(1)}%`;

  const live = $('thresh-live');
  if (!live) return;
  const testDiff = -8;
  let label, fg, bg;
  if (testDiff <= -r)      { label='Critical'; fg='var(--red)'; bg='var(--rb)'; }
  else if (testDiff <= -a) { label='Warning';  fg='var(--amb)'; bg='var(--ab)'; }
  else if (Math.abs(testDiff) <= p) { label='Parity'; fg='var(--t2)'; bg='var(--bg)'; }
  else                     { label='We\'re cheaper'; fg='var(--grn)'; bg='var(--gb)'; }
  live.innerHTML = `<span style="color:var(--t2)">A <strong>−8%</strong> differential would be classified as:</span> <span style="padding:3px 10px;border-radius:5px;font-weight:600;background:${bg};color:${fg}">${label}</span>`;
}

function saveThresholds() {
  T.red = parseInt($('t-red').value) || 10;
  T.amb = parseInt($('t-amb').value) || 5;
  T.par = parseInt($('t-par').value) || 2;
  try { localStorage.setItem('pw_thresholds', JSON.stringify(T)); } catch(e) {}
  applyThresholdCSS();
  buildLegend('comp-detail-legend');
  buildLegend('sku-detail-legend');
  if (compSkusAll.length) renderCompDetail();
  if (distChart) buildDistChart({
    cheapest:$('m-cheap').textContent||0,
    parity:0,
    warning:$('m-warn').textContent||0,
    critical:$('m-crit').textContent||0,
    oos:$('m-oos').textContent||0
  });
  const btn = event.target;
  const orig = btn.innerHTML;
  btn.innerHTML = '<i class="ti ti-check"></i> Saved';
  setTimeout(() => btn.innerHTML = orig, 1500);
}

function resetThresholds() {
  $('t-red').value = 10;
  $('t-amb').value = 5;
  $('t-par').value = 2;
  updateThreshPreview();
}

/* ════════════════════════════════════════
   MANUAL LOOKUP / REFRESH
════════════════════════════════════════ */
async function lookupSKU(skuId, btn) {
  const orig = btn.innerHTML;
  btn.innerHTML = '<i class="ti ti-loader"></i> Scraping…';
  btn.disabled = true;
  try {
    await authFetch('/lookup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({sku_id: skuId}) });
    setTimeout(() => { btn.innerHTML = orig; btn.disabled = false; }, 5000);
  } catch { btn.innerHTML = orig; btn.disabled = false; }
}

async function scrapeRow(skuId, competitorId, btn) {
  const orig = btn.innerHTML;
  btn.innerHTML = '<i class="ti ti-loader" style="font-size:13px"></i>';
  btn.disabled = true;
  try {
    await authFetch('/lookup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({sku_id: skuId, competitor_id: competitorId}) });
    setTimeout(async () => {
      btn.innerHTML = '<i class="ti ti-check" style="font-size:13px;color:var(--grn)"></i>';
      await loadSkuDetail({ skuId, fromPanel: drawerFromPanel });
    }, 4000);
  } catch (e) {
    btn.innerHTML = orig;
    btn.disabled = false;
    alert('Scrape failed: ' + e.message);
  }
}

async function manualRefresh() {
  const btn = event.target.closest('button');
  const orig = btn.innerHTML;
  btn.innerHTML = '<i class="ti ti-loader" style="font-size:12px"></i> Refreshing…';
  btn.disabled = true;
  await loadDashboard();
  btn.innerHTML = orig;
  btn.disabled = false;
}

/* ════════════════════════════════════════
   SORTING ENGINE
════════════════════════════════════════ */
const sortState = {
  bycomp:   { col: null, dir: 1 },
  skus:     { col: null, dir: 1 },
  compSku:  { col: null, dir: 1 },
  skuComp:  { col: null, dir: 1 },
};

let bycompData   = [];
let skusData     = [];
let skuCompData  = [];

function updateSortHeaders(tableId, stateKey, col) {
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  tbl.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    const fn = th.getAttribute('onclick') || '';
    const m  = fn.match(/'([^']+)'/);
    if (m && m[1] === col) {
      th.classList.add(sortState[stateKey].dir === 1 ? 'sort-asc' : 'sort-desc');
    }
  });
}

function toggleSort(stateKey, col) {
  const s = sortState[stateKey];
  if (s.col === col) s.dir = s.dir === 1 ? -1 : 1;
  else { s.col = col; s.dir = 1; }
}

function cmpVal(a, b, dir) {
  if (a === null || a === undefined) return 1;
  if (b === null || b === undefined) return -1;
  if (typeof a === 'string' && typeof b === 'string') return a.localeCompare(b) * dir;
  return (a - b) * dir;
}

function sortByCompTable(col) {
  toggleSort('bycomp', col);
  updateSortHeaders('bycomp-table', 'bycomp', col);
  const { dir } = sortState.bycomp;
  const sorted = [...bycompData].sort((a, b) => {
    const getV = r => {
      if (col === 'name')     return (r.name||'').toLowerCase();
      if (col === 'domain')   return (r.domain||'').toLowerCase();
      if (col === 'vat_status') return (r.vat_status||'').toLowerCase();
      if (col === 'matched')  return r._matched  ?? 0;
      if (col === 'critical') return r._critical ?? 0;
      if (col === 'warning')  return r._warning  ?? 0;
      if (col === 'cheaper')  return r._cheaper  ?? 0;
      if (col === 'parity')   return r._parity   ?? 0;
      return '';
    };
    return cmpVal(getV(a), getV(b), dir);
  });
  renderByCompRows(sorted);
}

function renderByCompRows(comps) {
  $('bycomp-tbody').innerHTML = comps.map(c => {
    const slug = slugify(c.name);
    const noData = c._matched === 0;
    return `<tr class="tr-link" onclick="go('comp-detail',{compId:${c.id},compName:'${c.name.replace(/'/g,"\\'")}',compSlug:'${slug}',compDomain:'${c.domain}',compVat:'${c.vat_status}'})">
      <td style="font-weight:500">${c.name}</td>
      <td style="font-size:11px;color:var(--t2)">${c.domain}</td>
      <td>${vatPill(c.vat_status)}</td>
      <td style="color:var(--t2)">${noData ? '<span style="color:var(--t3)">—</span>' : c._matched}</td>
      <td style="font-weight:600">${noData ? '<span style="color:var(--t3)">—</span>' : (c._critical > 0 ? `<span style="color:var(--red)">${c._critical}</span>` : '<span style="color:var(--t3)">0</span>')}</td>
      <td>${noData ? '<span style="color:var(--t3)">—</span>' : (c._warning > 0 ? `<span style="color:var(--amb);font-weight:500">${c._warning}</span>` : '<span style="color:var(--t3)">0</span>')}</td>
      <td>${noData ? '<span style="color:var(--t3)">—</span>' : (c._cheaper > 0 ? `<span style="color:var(--grn)">${c._cheaper}</span>` : '<span style="color:var(--t3)">0</span>')}</td>
      <td style="color:var(--t2)">${noData ? '<span style="color:var(--t3)">—</span>' : c._parity}</td>
    </tr>`;
  }).join('');
}

function sortSkusTable(col) {
  toggleSort('skus', col);
  updateSortHeaders('skus-table', 'skus', col);
  const { dir } = sortState.skus;
  const sorted = [...skusData].sort((a, b) => {
    const getV = r => {
      if (col === 'sku_id')        return (r.sku_id||'').toLowerCase();
      if (col === 'short_title')   return (r.short_title||'').toLowerCase();
      if (col === 'our_price')     return parseFloat(r.our_price||0);
      if (col === 'competitor_name') return (r.competitor_name||'').toLowerCase();
      if (col === 'their_price')   { const raw = r.competitor_price ? parseFloat(r.competitor_price) : null; const vat = r.competitor_vat || r.competitor_vat_default || 'unknown'; return raw ? normalisePrice(raw, vat) : 999999; }
      if (col === 'diff')          return parseFloat(r.diff_pct_normalised??r.diff_pct??0);
      if (col === 'confidence')   return parseFloat(r.confidence??0);
      if (col === 'match_status') { const ms = r._ms; if (!ms) return 0; return (ms.human*3) + (ms.auto*2) + (ms.review); }
      if (col === 'reviewed_at')  { return r._ms?.last_reviewed ? new Date(r._ms.last_reviewed).getTime() : 0; }
      if (col === 'availability')  return (r.availability||'').toLowerCase();
      if (col === 'scraped_at')    return r.scraped_at ? new Date(r.scraped_at).getTime() : 0;
      return '';
    };
    return cmpVal(getV(a), getV(b), dir);
  });
  renderSkusRows(sorted);
}

function renderSkusRows(rows) {
  $('sku-tbody').innerHTML = rows.map(r => {
    const diff     = r.diff_pct_normalised ?? r.diff_pct;
    const raw      = r.competitor_price ? parseFloat(r.competitor_price) : null;
    const vat      = r.competitor_vat || r.competitor_vat_default || 'unknown';
    const ex       = raw ? normalisePrice(raw, vat) : null;
    const thumb    = thumbUrl(r.image_url || '', 50, 50);
    const unitQty  = r.unit_qty && r.unit_qty > 1 ? r.unit_qty : null;

    const normDiff   = r.diff_pct_normalised;
    const packDiff   = r.diff_pct;
    const showPerUnit = unitQty && normDiff != null && packDiff != null
                        && Math.abs(normDiff - packDiff) > 0.5;

    const ourPrice  = r.our_price ? parseFloat(r.our_price) : null;
    const ourPerUnit = (showPerUnit && ourPrice) ? ourPrice / unitQty : null;

    return `<tr class="${rowClass(diff)} tr-link" onclick="go('sku-detail',{skuId:'${r.sku_id}',fromPanel:'skus'})">
      <td style="padding:4px 8px;width:58px">
        <div style="width:50px;height:50px;border:1px solid var(--border);border-radius:5px;overflow:hidden;background:var(--bb);display:flex;align-items:center;justify-content:center;flex-shrink:0">
          ${thumb
            ? `<img src="${thumb}" style="width:100%;height:100%;object-fit:contain" onerror="this.style.display='none'">`
            : `<i class="ti ti-photo" style="font-size:16px;color:var(--t3)"></i>`}
        </div>
      </td>
      <td>${skuLink(r)}</td>
      <td style="color:var(--t2);max-width:160px;white-space:normal;line-height:1.3">${r.short_title||''}</td>
      <td style="font-weight:500">
        ${fmtPrice(r.our_price)}
        ${ourPerUnit ? `<div style="font-size:12px;color:var(--t2)">${fmtPrice(ourPerUnit)}/unit</div>` : ''}
      </td>
      <td style="color:var(--t2)">${r.competitor_name||'—'}</td>
      <td style="font-weight:500">${ex ? fmtPrice(ex) : '—'}${ex ? ` <span style="font-size:9px;color:var(--t3)">${vatPill(vat)}</span>` : ''}</td>
      <td>
        <span class="${diffClass(diff)}">${diffLabel(diff)}</span>
        ${showPerUnit ? `<div style="font-size:12px;color:var(--t2)" title="Pack of ${unitQty} vs per-unit comparison">per unit · pack: <span class="${diffClass(packDiff)}">${diffLabel(packDiff)}</span></div>` : ''}
      </td>
      <td>${confWidget(r.confidence)}</td>
      <td style="white-space:nowrap">
        ${r._ms ? matchStatusMini(r._ms) : '<span style="color:var(--t3);font-size:12px">—</span>'}
      </td>
      <td style="font-size:12px;color:var(--t3)">${r._ms?.last_reviewed ? ts(r._ms.last_reviewed) : '—'}</td>
      <td>${stockBadge(r.availability)}</td>
      <td style="color:var(--t3)">${ts(r.scraped_at)}</td>
      <td style="white-space:nowrap"><button class="btn sm ghost" onclick="event.stopPropagation();openDrawer('${r.sku_id}','${(r.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(r.our_price)}','skus')" style="font-size:11px;padding:4px 8px">Quick view</button></td>
    </tr>`;
  }).join('');
}

function sortCompSkuTable(col) {
  toggleSort('compSku', col);
  updateSortHeaders('comp-sku-table', 'compSku', col);
  const { dir } = sortState.compSku;
  const sorted = [...compSkusFiltered].sort((a, b) => {
    const getV = r => {
      const diff = r.diff_pct_normalised ?? r.diff_pct;
      const raw  = r.competitor_price ? parseFloat(r.competitor_price) : null;
      const vat  = r.competitor_vat || r.competitor_vat_default || 'unknown';
      const ex   = raw ? normalisePrice(raw, vat) : null;
      if (col === 'sku_id')      return (r.sku_id||'').toLowerCase();
      if (col === 'short_title') return (r.short_title||'').toLowerCase();
      if (col === 'our_price')   return parseFloat(r.our_price||0);
      if (col === 'their_price') return ex ?? 999999;
      if (col === 'diff')        return parseFloat(diff??0);
      if (col === 'availability')return (r.availability||'').toLowerCase();
      if (col === 'scraped_at')  return r.scraped_at ? new Date(r.scraped_at).getTime() : 0;
      return '';
    };
    return cmpVal(getV(a), getV(b), dir);
  });
  $('comp-sku-tbody').innerHTML = sorted.map(s => {
    const diff = s.diff_pct_normalised ?? s.diff_pct;
    const raw  = s.competitor_price ? parseFloat(s.competitor_price) : null;
    const vat  = s.competitor_vat || s.competitor_vat_default || 'unknown';
    const ex   = raw ? normalisePrice(raw, vat) : null;
    return `<tr class="${rowClass(diff)} tr-link" onclick="openDrawer('${s.sku_id}','${(s.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(s.our_price)}','comp-detail')">
      <td><span style="font-family:'SF Mono',monospace;font-size:11px;color:var(--blu)">${s.sku_id}</span></td>
      <td style="font-size:11px;color:var(--t2);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.short_title||''}</td>
      <td style="font-weight:500">${fmtPrice(s.our_price)}</td>
      <td style="font-weight:500">${ex?fmtPrice(ex):'—'}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span></td>
      <td>${stockBadge(s.availability)}</td>
      <td style="font-size:10px;color:var(--t3)">${ts(s.scraped_at)}</td>
      <td><button class="btn sm ghost" onclick="event.stopPropagation();openDrawer('${s.sku_id}','${(s.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(s.our_price)}','comp-detail')">→</button></td>
    </tr>`;
  }).join('');
}

function sortSkuCompTable(col) {
  toggleSort('skuComp', col);
  updateSortHeaders('sku-comp-table', 'skuComp', col);
  const { dir } = sortState.skuComp;
  const sorted = [...skuCompData].sort((a, b) => {
    const getV = r => {
      const diff = r.diff_pct_normalised ?? r.diff_pct;
      const raw  = r.competitor_price ? parseFloat(r.competitor_price) : null;
      const vat  = r.competitor_vat || r.competitor_vat_default || 'unknown';
      const ex   = raw ? normalisePrice(raw, vat) : null;
      if (col === 'name')        return (r.competitor_name||'').toLowerCase();
      if (col === 'price')       return ex ?? 999999;
      if (col === 'vat')         return (vat||'').toLowerCase();
      if (col === 'diff')        return parseFloat(diff??0);
      if (col === 'availability')return (r.availability||'').toLowerCase();
      if (col === 'confidence')  return parseFloat(r.confidence||0);
      if (col === 'scraped_at')  return r.scraped_at ? new Date(r.scraped_at).getTime() : 0;
      return '';
    };
    return cmpVal(getV(a), getV(b), dir);
  });
  renderSkuCompRows(sorted);
}

function renderSkuCompRows(rows) {
  $('sku-comp-tbody').innerHTML = rows.map(r => {
    const diff  = r.diff_pct_normalised ?? r.diff_pct;
    const raw   = r.competitor_price ? parseFloat(r.competitor_price) : null;
    const vat   = r.competitor_vat || r.competitor_vat_default || 'unknown';
    const ex    = raw ? normalisePrice(raw, vat) : null;
    const tier  = getTier(diff);
    const barW  = Math.min(100, Math.abs(diff||0) * 4);
    const barC  = {r:'var(--red)',a:'var(--amb)',g:'var(--grn)',p:'var(--t3)'}[tier];
    const rowId = `${r.sku_id}-${r.competitor_id}`;
    const hasUrl = !!r.competitor_url;
    return `<tr class="${rowClass(diff)}" id="skurow-${rowId}">
      <td style="font-weight:500">${r.competitor_name||'—'}<div style="font-size:10px;color:var(--t2)">${r.competitor_domain||''}</div></td>
      <td style="font-weight:500">${ex
        ? (r.competitor_url
            ? `<a href="${r.competitor_url}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:underline;text-decoration-color:rgba(0,0,0,.2);text-underline-offset:2px" onclick="event.stopPropagation()">${fmtPrice(ex)} <i class="ti ti-external-link" style="font-size:10px;color:var(--t3)"></i></a>`
            : fmtPrice(ex))
        : '—'}</td>
      <td>${vatPill(vat)}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span></td>
      <td><div class="dbar-wrap"><div class="dbar-track"><div class="dbar-fill" style="width:${barW}%;background:${barC}"></div></div></div></td>
      <td>${stockBadge(r.availability)}</td>
      <td>${confWidget(r.confidence)}</td>
      <td style="font-size:10px;color:var(--t3)">${ts(r.scraped_at)}</td>
      <td>
        <button class="btn sm ghost" onclick="editMatchUrl('${rowId}','${r.sku_id}',${r.competitor_id},'${(r.competitor_url||'').replace(/'/g,"\\'")}',this)"
          style="font-size:10px;color:${hasUrl?'var(--grn)':'var(--t3)'}" title="${hasUrl?'Edit matching URL':'Add matching URL'}">
          <i class="ti ${hasUrl?'ti-link':'ti-link-plus'}" style="font-size:13px"></i>
          ${hasUrl?'Edit URL':'Add URL'}
        </button>
      </td>
      <td><button class="btn sm ghost" id="scrape-${rowId}" onclick="scrapeRow('${r.sku_id}',${r.competitor_id},this)" title="Re-scrape this competitor now"><i class="ti ti-refresh" style="font-size:13px"></i></button></td>
    </tr>
    <tr id="skurow-edit-${rowId}" style="display:none;background:var(--bb)">
      <td colspan="10" style="padding:10px 12px">
        <div style="font-size:11px;font-weight:500;margin-bottom:6px;color:var(--blu)">
          <i class="ti ti-link" style="font-size:12px;margin-right:4px"></i>
          Match URL for <strong>${r.competitor_name}</strong> — paste the exact product page URL
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="match-url-${rowId}" type="text" value="${r.competitor_url||''}" placeholder="https://competitor.com/product-page"
            autocomplete="new-password" spellcheck="false"
            style="flex:1;min-width:260px;padding:6px 10px;font-size:12px;border:1px solid var(--bbd);border-radius:var(--r);font-family:inherit;outline:none;background:var(--surface)"
            onkeydown="if(event.key==='Enter')saveMatchUrl('${rowId}','${r.sku_id}',${r.competitor_id},this.closest('tr').previousElementSibling,event)">
          <button class="btn sm prim" onclick="saveMatchUrl('${rowId}','${r.sku_id}',${r.competitor_id},this)">
            <i class="ti ti-device-floppy"></i> Save &amp; scrape
          </button>
          <button class="btn sm ghost" onclick="cancelEditUrl('${rowId}')">Cancel</button>
        </div>
        <div id="match-url-msg-${rowId}" style="font-size:11px;margin-top:6px;display:none"></div>
      </td>
    </tr>`;
  }).join('');
}

function editMatchUrl(rowId, skuId, competitorId, currentUrl, btn) {
  document.querySelectorAll('[id^="skurow-edit-"]').forEach(r => r.style.display = 'none');
  const editRow = $(`skurow-edit-${rowId}`);
  if (editRow) {
    editRow.style.display = '';
    const input = $(`match-url-${rowId}`);
    if (input) { input.value = currentUrl; input.focus(); input.select(); }
  }
}

function cancelEditUrl(rowId) {
  const editRow = $(`skurow-edit-${rowId}`);
  if (editRow) editRow.style.display = 'none';
}

async function saveMatchUrl(rowId, skuId, competitorId, btn) {
  const input = $(`match-url-${rowId}`);
  const msgEl = $(`match-url-msg-${rowId}`);
  const url   = (input?.value || '').trim();

  if (!url || !url.startsWith('http')) {
    input.style.borderColor = 'var(--red)';
    if (msgEl) { msgEl.textContent = 'Must be a valid URL starting with https://'; msgEl.style.color = 'var(--red)'; msgEl.style.display = ''; }
    return;
  }

  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="ti ti-loader"></i> Saving…';
  if (msgEl) msgEl.style.display = 'none';

  try {
    const { error } = await sb.from('competitor_matches').upsert({
      sku_id:          skuId,
      competitor_id:   competitorId,
      competitor_url:  url,
      match_status:    'matched',
      match_source:    'human',
      human_reviewed:  true,
      reviewed_at:     new Date().toISOString(),
      updated_at:      new Date().toISOString(),
    }, { onConflict: 'sku_id,competitor_id' });

    if (error) throw new Error(error.message);

    if (msgEl) { msgEl.textContent = '✓ Saved — triggering scrape…'; msgEl.style.color = 'var(--grn)'; msgEl.style.display = ''; }

    try {
      await authFetch('/lookup', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ sku_id: skuId, competitor_id: competitorId }) });
    } catch(e) { /* best-effort */ }

    setTimeout(async () => {
      cancelEditUrl(rowId);
      await loadSkuDetail({ skuId, fromPanel: drawerFromPanel });
    }, 3000);

  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = orig;
    if (msgEl) { msgEl.textContent = 'Save failed: ' + e.message; msgEl.style.color = 'var(--red)'; msgEl.style.display = ''; }
  }
}

let skusLoaded = false;

/* ════════════════════════════════════════
   INIT
════════════════════════════════════════ */
updateThreshPreview();
bootstrap();
