/* ════════════════════════════════════════
   CONFIG + GLOBALS
════════════════════════════════════════ */
const SUPABASE_URL  = 'https://uaqakssusydpjzrcznhb.supabase.co';
const SUPABASE_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVhcWFrc3N1c3lkcGp6cmN6bmhiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3NDM3ODUsImV4cCI6MjA5NDMxOTc4NX0.OM0XBJsFb6hlLKXLoNpahuH4zzGcnNR3W-bzKfKPZ-w';
const API_BASE      = 'https://uaqakssusydpjzrcznhb.supabase.co/functions/v1/api';
const ALLOWED_DOMAINS = ['ukpos.com'];

let sb, currentUser, currentProfile;
let skuPage = 1, skuLimit = 50, skuTotal = 0;
let reviewPage = 1, reviewLimit = 50;
let alertTab = 'crit';
let currentCompId = null, currentCompName = null, currentCompSlug = null;
let currentSkuId = null;
let compViewMode = 'grid';
let compSkusAll = [], compSkusFiltered = [];
let drawerSkuId = null, drawerFromPanel = null;
let distChart = null;
let appBootstrapped = false; // true after first sign-in load; gates redundant alerts re-fetch

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

/* Per-unit display helper.
   Given an ex-VAT price and a pack qty, returns a small muted "per unit" label
   when qty > 1, else ''. Uses 3 dp for sub-£1 unit prices, 2 dp otherwise. */
function perUnitLabel(exPrice, qty) {
  if (!exPrice || !qty || qty <= 1) return '';
  const per = exPrice / qty;
  const txt = per < 1 ? `£${per.toFixed(3)}` : `£${per.toFixed(2)}`;
  return `<span style="font-size:10px;color:var(--t3);white-space:nowrap">${txt}/unit ×${qty}</span>`;
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

/* Cached access token — avoids a getSession() round-trip on every fetch */
let cachedToken = null;

/* Always ask the Supabase client for the current session. The client keeps a
   valid token in memory and refreshes it automatically when near expiry, so
   getSession() is cheap when the token is fresh and self-heals when it isn't.
   We no longer trust a long-lived cached token — doing so caused writes to fail
   with stale-token 401s around the hourly refresh and right after a password
   change (which rotates the token immediately). */
async function getToken() {
  let { data: { session } } = await sb.auth.getSession();
  /* If the session is missing or the token is about to expire, force a refresh
     so we never send a token the server will reject. */
  const nearExpiry = session?.expires_at
    ? (session.expires_at * 1000 - Date.now()) < 60_000
    : false;
  if (!session || nearExpiry) {
    const { data } = await sb.auth.refreshSession();
    if (data?.session) session = data.session;
  }
  cachedToken = session?.access_token || null;
  return cachedToken;
}

/* Ensure the Supabase client holds a valid (non-expired) session before a
   direct sb.from(...).update() / sb.auth call. These bypass authFetch's retry,
   so we proactively refresh when near expiry to avoid the stale-token failures
   that made notes/URL/password saves need several attempts. */
async function ensureFreshSession() {
  const { data: { session } } = await sb.auth.getSession();
  const nearExpiry = session?.expires_at
    ? (session.expires_at * 1000 - Date.now()) < 60_000
    : true;
  if (nearExpiry) {
    const { data } = await sb.auth.refreshSession();
    cachedToken = data?.session?.access_token || null;
  }
}

async function authFetch(path, opts = {}, _retried = false) {
  opts.headers = opts.headers || {};
  const token = await getToken();
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(API_BASE + path, opts);
  /* If the token was rejected, force one refresh and retry exactly once.
     This makes a stale token heal inside a single user action instead of
     failing and needing a manual second attempt. */
  if (res.status === 401 && !_retried) {
    cachedToken = null;
    await sb.auth.refreshSession();
    return authFetch(path, opts, true);
  }
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
  /* Hide all panels */
  document.querySelectorAll('.panel').forEach(p => {
    p.classList.remove('active');
    p.style.display = '';
  });
  /* Remove active from all nav items */
  NAV_ITEMS.forEach(n => {
    const el = $('nav-' + n);
    if (el) el.classList.remove('active');
  });

  const panelId = 'p-' + name;
  const panel = $(panelId);
  if (!panel) { go('alerts'); return; }
  panel.classList.add('active');

  /* Update URL hash */
  let hash = '#' + name;
  if (opts.skuId) { hash = '#sku/' + opts.skuId; }
  else if (opts.compSlug) { hash = '#competitor/' + opts.compSlug; }
  history.replaceState(null, '', hash);

  /* Update sidebar active */
  const navKey = {
    'alerts':'alerts','review':'review','skus':'skus','bycat':'bycat',
    'bycomp':'bycomp','comp-detail':'bycomp','sku-detail':'skus',
    'schedule':'schedule','settings':'settings'
  }[name];
  if (navKey && $('nav-' + navKey)) $('nav-' + navKey).classList.add('active');

  /* Panel-specific init — every panel re-fetches on entry so data is always
     fresh. (All-SKUs previously loaded only once via a skusLoaded guard, which
     is why it showed stale data until a manual refresh — guard removed.) */
  if (name === 'skus') { const q = $('skuQ'); if (q && q.value === (currentUser?.email||'')) q.value = ''; loadSKUs(); }
  if (name === 'alerts')      { if (appBootstrapped) loadDashboard(); }
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
    /* Try to find comp by slug */
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

/* ════════════════════════════════════════
   AUTH
════════════════════════════════════════ */
function showView(v) {
  ['view-loading','view-login','view-pending','view-rejected','view-app'].forEach(id => {
    const el = $(id);
    if (!el) return;
    if (id !== v) { el.style.display = 'none'; return; }
    /* view-app is .shell which needs display:grid, auth screens are block */
    el.style.display = (id === 'view-app') ? 'grid' : '';
  });
}

function showAuthTab(which) {
  $('tab-login').classList.toggle('active', which === 'login');
  $('tab-register').classList.toggle('active', which === 'register');
  $('form-login').style.display    = which === 'login'    ? '' : 'none';
  $('form-register').style.display = which === 'register' ? '' : 'none';
  $('login-msg').innerHTML = '';
  $('reg-msg').innerHTML = '';
}

async function bootstrap() {
  sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
  const { data: { session } } = await sb.auth.getSession();
  if (!session) { showView('view-login'); return; }
  await onSignedIn(session);

  sb.auth.onAuthStateChange(async (event, session) => {
    /* Always invalidate cached token on any auth event */
    cachedToken = session?.access_token || null;
    if (event === 'SIGNED_OUT' || !session) { currentUser = null; currentProfile = null; showView('view-login'); return; }
    if (['SIGNED_IN','TOKEN_REFRESHED','USER_UPDATED','INITIAL_SESSION'].includes(event)) await onSignedIn(session);
  });
}

async function onSignedIn(session) {
  currentUser  = session.user;
  cachedToken  = session.access_token || null;  /* prime cache immediately */
  const { data: profiles } = await sb.from('profiles').select('*').eq('id', session.user.id).limit(1);
  if (!profiles || !profiles.length) { showView('view-pending'); return; }
  currentProfile = profiles[0];
  $('header-email').textContent = currentProfile.email || '—';
  $('ma-email').value = currentProfile.email || '';
  $('ma-name').value  = currentProfile.full_name || '';

  if (currentProfile.status === 'pending')  { showView('view-pending'); return; }
  if (currentProfile.status === 'rejected') { showView('view-rejected'); return; }

  showView('view-app');
  if (currentProfile.role === 'super_admin') {
    const usersTab = $('sn-users');
    if (usersTab) usersTab.style.display = '';
  }
  /* loadDashboard() populates global chrome (sidebar badges, footer counts,
     sync status) shown on every page, plus the alerts panel — so call it once
     at sign-in regardless of landing route. Navigation reloads are handled in
     go(); the bootstrapped flag below prevents a double-fetch when the landing
     route is the alerts panel. */
  loadDashboard();
  restoreRoute();
  appBootstrapped = true;
  /* Pre-fetch SKU list in the background so By Category loads instantly */
  if (!allSkus.length) {
    sb.from('skus')
      .select('sku_id,short_title,price_ex_vat,product_url,availability,cat_l4,cat_l5,image_url,slug')
      .eq('active', true)
      .order('sku_id')
      .then(({ data }) => { if (data) allSkus = data; });
  }
  setInterval(() => { if ($('p-alerts').classList.contains('active')) loadDashboard(); }, 5 * 60 * 1000);
}

async function handleLogin(e) {
  e.preventDefault();
  $('login-btn').disabled = true;
  const { error } = await sb.auth.signInWithPassword({ email: $('login-email').value.trim(), password: $('login-password').value });
  $('login-btn').disabled = false;
  if (error) showMsg('login-msg', error.message, 'err');
}

async function handleForgotPassword() {
  const email = $('login-email').value.trim();
  if (!email) { showMsg('login-msg', 'Enter your email above first.', 'info'); return; }
  const { error } = await sb.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin + '/' });
  if (error) showMsg('login-msg', error.message, 'err');
  else showMsg('login-msg', 'Check your inbox for a reset link.', 'ok');
}

async function handleRequestAccess(e) {
  e.preventDefault();
  $('reg-btn').disabled = true;
  const email = $('reg-email').value.trim().toLowerCase();
  const full_name = $('reg-name').value.trim();
  const domain = email.split('@')[1] || '';
  if (!ALLOWED_DOMAINS.includes(domain)) {
    showMsg('reg-msg', 'Only @ukpos.com email addresses are permitted.', 'err');
    $('reg-btn').disabled = false;
    return;
  }
  const { error } = await sb.from('access_requests').upsert({ email, full_name }, { onConflict: 'email' });
  $('reg-btn').disabled = false;
  if (error) { showMsg('reg-msg', error.message || 'Request failed.', 'err'); return; }
  showMsg('reg-msg', 'Request submitted — you will be notified once approved.', 'ok');
  $('form-register').reset();
}

async function signOut() {
  if (sb) await sb.auth.signOut();
}

function openMyAccount() { go('settings'); goSett('account'); }

async function saveMyAccount() {
  const name = $('ma-name').value.trim();
  const pw   = $('ma-password').value;
  $('ma-msg').style.display = 'none';
  try {
    await ensureFreshSession();
    const { error: dbErr } = await sb.from('profiles').update({ full_name: name }).eq('id', currentProfile.id);
    if (dbErr) throw new Error(dbErr.message);
    /* Password change goes LAST: it rotates the access token immediately, which
       would invalidate the token any subsequent write in this block relies on. */
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

    // Sidebar meta
    $('sidebar-foot').innerHTML = `<strong>${(d.sku_count||0).toLocaleString()}</strong> SKUs · <strong>${d.competitor_count||23}</strong> competitors<br><strong>${(d.snapshot_count||0).toLocaleString()}</strong> snapshots`;

    // Sync status
    if (d.last_run) {
      const r = d.last_run;
      $('sync-status').innerHTML = `Last sync <strong style="color:rgba(255,255,255,.65)">${ts(r.completed_at||r.started_at)}</strong> · ${r.skus_succeeded||0} SKUs`;
    }

    // Data note if review queue is large
    if (reviewCount > 100) {
      $('alerts-data-note').style.display = '';
    }
    $('alerts-sub').textContent = `${critCount} critical · ${m.warning||0} warning · ${m.oos||0} competitor OOS`;

    // Priority strip
    renderPriorityStrip(d.worst || []);

    // Alert render
    renderAlertList(d.alerts || []);
    if (d.alerts?.length) $('alert-ts').textContent = `— ${d.alerts.length} active`;

    // Worst table
    renderWorstTable(d.worst || []);

    // Chart
    buildDistChart(m);

    // Review note
    const matched = d.matched_count || 0;
    const total   = d.match_count   || 0;
    $('review-confirmed').textContent = matched.toLocaleString();
    $('review-total').textContent     = total.toLocaleString();
    if (total > 0) $('review-note').style.display = '';

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
  /* Re-render alert list filtered by tab */
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
  window._lastAlerts = alerts;  /* cache for tab switching and export */
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
    $('skus-sub').textContent = `${skuTotal.toLocaleString()} SKUs`;

    if (!rows.length) {
      $('sku-tbody').innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--t2);padding:20px">No matches found</td></tr>';
      $('sku-pagination').innerHTML = '';
      return;
    }

    skusData = rows;
    sortState.skus = { col: null, dir: 1 };
    updateSortHeaders('skus-table', 'skus', null);
    renderSkusRows(rows);

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
  } catch (e) {
    $('sku-tbody').innerHTML = `<tr><td colspan="9" style="color:var(--red);padding:8px">${e.message}</td></tr>`;
  }
}

function filterSKUs() { skuPage = 1; loadSKUs(); }

/* ════════════════════════════════════════
   REVIEW QUEUE
════════════════════════════════════════ */
let reviewAllRows = [];

async function loadReview() {
  $('review-list').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
  try {
    const d = await authFetch('/review?page=1&limit=500');
    const matches = d.data || [];

    /* Fetch latest snapshot price for every match in one query */
    const matchIds = matches.map(m => `(sku_id.eq.${m.sku_id},competitor_id.eq.${m.competitor_id})`);
    let snapMap = {};
    if (matches.length) {
      /* Supabase doesn't support tuple IN — fetch latest_snapshots for all relevant SKUs then filter */
      const skuIds = [...new Set(matches.map(m => m.sku_id))];
      const { data: snaps } = await sb.from('latest_snapshots')
        .select('sku_id,competitor_id,competitor_price,competitor_vat,availability')
        .in('sku_id', skuIds);
      (snaps || []).forEach(s => { snapMap[`${s.sku_id}__${s.competitor_id}`] = s; });
    }

    /* Merge snapshot data into each match row */
    reviewAllRows = matches.map(m => ({
      ...m,
      _snap: snapMap[`${m.sku_id}__${m.competitor_id}`] || null,
    }));

    /* Build competitor dropdown from ALL active competitors */
    const cf = $('review-comp-filter');
    if (cf && cf.options.length === 1) {
      const { data: allComps } = await sb.from('competitors').select('id,name').eq('active',true).order('name');
      (allComps || []).forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.id; opt.textContent = c.name;
        cf.appendChild(opt);
      });
    }
    filterReview();
  } catch (e) {
    $('review-list').innerHTML = `<div style="color:var(--red);padding:8px">${e.message}</div>`;
  }
}

function filterReview() {
  const q    = ($('review-search')?.value || '').toLowerCase().trim();
  const comp = $('review-comp-filter')?.value || '';

  let rows = reviewAllRows;
  if (q) {
    rows = rows.filter(r =>
      r.sku_id?.toLowerCase().includes(q) ||
      (r.skus?.short_title||'').toLowerCase().includes(q) ||
      (r.competitors?.name||'').toLowerCase().includes(q) ||
      (r.competitor_title||'').toLowerCase().includes(q)
    );
  }
  if (comp) rows = rows.filter(r => String(r.competitor_id) === String(comp));

  const total   = reviewAllRows.length;
  const showing = rows.length;
  $('review-sub').textContent = (q || comp)
    ? `${showing} of ${total} matches (filtered)`
    : `${total} matches awaiting confirmation`;

  if (!rows.length) {
    $('review-list').innerHTML = '<div style="text-align:center;color:var(--t2);padding:40px;font-size:12px">'
      + (q || comp
          ? '<i class="ti ti-search" style="font-size:24px;display:block;margin-bottom:8px;opacity:.3"></i>No matches for that filter'
          : '<i class="ti ti-circle-check" style="font-size:24px;display:block;margin-bottom:8px;color:var(--grn)"></i>Queue is empty')
      + '</div>';
    $('review-pagination').innerHTML = '';
    return;
  }

  const lim   = reviewLimit === 999999 ? rows.length : reviewLimit;
  const start = (reviewPage - 1) * lim;
  const page  = rows.slice(start, start + lim);
  const from  = start + 1;
  const to    = start + page.length;

  $('review-list').innerHTML = page.map(r => {
    const sku      = r.skus || {};
    const comp     = r.competitors || {};
    const snap     = r._snap || {};
    const ourUrl   = sku.product_url || (sku.slug ? `https://www.ukpos.com/${sku.slug}?vat=0` : '#');
    const ourImg   = sku.image_url || '';
    const compImg  = r.competitor_image_url
      ? r.competitor_image_url
      : `https://www.google.com/s2/favicons?domain=${comp.domain||''}&sz=64`;
    const ourPrice = sku.price_ex_vat ? parseFloat(sku.price_ex_vat) : null;
    const theirRaw = snap.competitor_price ? parseFloat(snap.competitor_price) : null;
    const theirEx  = theirRaw ? normalisePrice(theirRaw, snap.competitor_vat || comp.vat_status || 'unknown') : null;
    const compVat  = snap.competitor_vat || comp.vat_status || 'unknown';

    return `<div class="rev-card" id="rev-${r.id}">

      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:6px">
        <div style="display:flex;align-items:center;gap:8px">
          ${confWidget(r.confidence)}
          ${r.notes ? `<span style="font-size:10px;color:var(--t2);background:var(--bb);padding:2px 7px;border-radius:10px"><i class="ti ti-info-circle" style="font-size:10px"></i> ${r.notes}</span>` : ''}
        </div>
        <div style="font-size:10px;color:var(--t3)">${r.match_method||''}</div>
      </div>

      <div class="rev-pair">

        <!-- UKPOS side -->
        <div class="rev-side">
          <div class="rs-label" style="color:var(--blu)">UKPOS</div>
          <div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px">
            <div style="width:56px;height:56px;border:1px solid var(--border);border-radius:6px;overflow:hidden;flex-shrink:0;background:var(--bb);display:flex;align-items:center;justify-content:center">
              ${ourImg
                ? `<img src="${ourImg}" style="width:100%;height:100%;object-fit:contain" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
                : ''}<i class="ti ti-photo" style="font-size:20px;color:var(--t3);${ourImg?'display:none':''}"></i>
            </div>
            <div style="flex:1;min-width:0">
              <a href="#sku/${r.sku_id}" onclick="event.preventDefault();go('sku-detail',{skuId:'${r.sku_id}',fromPanel:'review'})" style="font-family:'SF Mono',monospace;font-size:11px;color:var(--blu);text-decoration:none">${r.sku_id}</a>
              <div class="rs-name" style="margin-top:2px">${sku.short_title||r.sku_id}</div>
            </div>
          </div>
          <a class="rs-url" href="${ourUrl}" target="_blank" rel="noopener" title="${ourUrl}">
            <i class="ti ti-external-link" style="font-size:10px"></i> ${ourUrl.replace('https://','').slice(0,55)}
          </a>
          <div class="rs-price" style="margin-top:6px">
            ${ourPrice ? `<strong>${fmtPrice(ourPrice)}</strong> <span class="vat vex">ex-VAT</span>` : '<span style="color:var(--t3)">No price</span>'}
          </div>
        </div>

        <div style="display:flex;align-items:center;justify-content:center;padding:0 8px;color:var(--t3);font-size:18px">⇄</div>

        <!-- Competitor side -->
        <div class="rev-side">
          <div class="rs-label" style="color:var(--amb)">${comp.name||'Competitor'}</div>
          <div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px">
            <div style="width:56px;height:56px;border:1px solid var(--border);border-radius:6px;overflow:hidden;flex-shrink:0;background:var(--bb);display:flex;align-items:center;justify-content:center">
              ${r.competitor_image_url
                ? `<img src="${r.competitor_image_url}" style="width:100%;height:100%;object-fit:contain" onerror="this.src='https://www.google.com/s2/favicons?domain=${comp.domain||''}&sz=64';this.style.width='32px';this.style.height='32px'">`
                : `<img src="${compImg}" style="width:32px;height:32px;object-fit:contain" onerror="this.style.display='none'">`}
            </div>
            <div style="flex:1;min-width:0">
              <div style="font-size:11px;color:var(--t2)">${comp.domain||''}</div>
              <div class="rs-name" style="margin-top:2px">${r.competitor_title||'—'}</div>
            </div>
          </div>
          <a class="rs-url" href="${r.competitor_url||'#'}" target="_blank" rel="noopener" title="${r.competitor_url||''}">
            <i class="ti ti-external-link" style="font-size:10px"></i> ${(r.competitor_url||'No URL set').replace('https://','').slice(0,55)}
          </a>
          <div class="rs-price" style="margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            ${theirEx
              ? `<strong>${fmtPrice(theirEx)}</strong> ${vatPill(compVat)}`
              : `<span style="color:var(--t3)">Not yet scraped</span> ${vatPill(compVat)}`}
            ${snap.availability && snap.availability !== 'in_stock' ? stockBadge(snap.availability) : ''}
          </div>
        </div>

      </div>

      <div id="rev-actions-${r.id}" style="display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
        <button class="btn sm prim" onclick="reviewDecision(${r.id},'approve',this)"><i class="ti ti-check"></i> Confirm match</button>
        <button class="btn sm danger" onclick="showCorrectUrl(${r.id})"><i class="ti ti-x"></i> Reject</button>
        <button class="btn sm ghost" onclick="skipReview(${r.id})"><i class="ti ti-skip-forward"></i> Skip</button>
      </div>
      <div id="rev-url-wrap-${r.id}" style="display:none;margin-top:8px">
        <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Paste the correct URL for this product (optional — leave blank to reject without one):</div>
        <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
          <input id="rev-url-${r.id}" type="url" name="rev-url-${r.id}" placeholder="https://competitor.com/correct-product-page"
            autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-form-type="other"
            style="flex:1;min-width:200px;padding:6px 10px;font-size:11px;border:1px solid var(--bm);border-radius:var(--r);font-family:inherit;outline:none">
          <button class="btn sm prim" onclick="saveCorrectUrl(${r.id},this)"><i class="ti ti-device-floppy"></i> Save URL &amp; reject</button>
          <button class="btn sm danger" onclick="reviewDecision(${r.id},'reject',this)">Reject without URL</button>
          <button class="btn sm ghost" onclick="cancelReject(${r.id})">Cancel</button>
        </div>
      </div>
    </div>`;
  }).join('');

  $('review-pagination').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>${from}–${to} of ${showing}${q||comp?' (filtered)':''}</span>
      <select onchange="reviewLimit=+this.value===0?999999:+this.value;reviewPage=1;filterReview()" style="padding:3px 6px;border-radius:5px;border:1px solid var(--bm);background:var(--surface);font-size:11px">
        <option value="50">50/page</option><option value="100">100/page</option><option value="250">250/page</option>
      </select>
    </div>
    <div style="display:flex;gap:6px">
      ${reviewPage>1?`<button class="btn sm" onclick="reviewPage--;filterReview()">← Prev</button>`:''}
      ${to<showing?`<button class="btn sm" onclick="reviewPage++;filterReview()">Next →</button>`:''}
    </div>`;
}

async function reviewDecision(matchId, decision, btn) {
  btn.disabled = true;
  try {
    await authPost(`/review/${matchId}`, { decision });
    const row = $(`rev-${matchId}`);
    if (row) { row.style.opacity = '.4'; row.style.pointerEvents = 'none'; btn.textContent = decision === 'approve' ? '✓ Confirmed' : 'Rejected'; }
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
    await ensureFreshSession();
    /* Update the competitor_matches URL then reject */
    const { error } = await sb.from('competitor_matches')
      .update({ competitor_url: newUrl, match_status: 'rejected', human_reviewed: true, reviewed_at: new Date().toISOString() })
      .eq('id', matchId);
    if (error) throw new Error(error.message);
    const row = $(`rev-${matchId}`);
    if (row) {
      row.style.opacity = '.45';
      row.style.pointerEvents = 'none';
      $(`rev-url-wrap-${matchId}`).innerHTML =
        `<div style="color:var(--grn);font-size:11px;padding:4px 0"><i class="ti ti-check"></i> URL saved — will be scraped on next run</div>`;
    }
    /* Refresh local dataset */
    reviewAllRows = reviewAllRows.filter(r => r.id !== matchId);
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
    /* Top level — category tiles */
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

  /* L4 selected — show subcategory tiles if multiple L5s exist, plus table below */
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

  /* Subcategory filter tiles (only shown at L4 level when multiple L5s exist) */
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

  /* Paginated SKU table */
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
    /* Fetch competitors and snapshot stats in parallel */
    const [{ data: comps }, { data: snaps }] = await Promise.all([
      sb.from('competitors').select('id,name,domain,vat_status,active').eq('active',true).order('name'),
      sb.from('latest_snapshots').select('competitor_id,diff_pct_normalised,diff_pct,competitor_price').not('competitor_price','is',null)
    ]);

    if (!comps?.length) {
      $('bycomp-tbody').innerHTML = '<tr><td colspan="8" style="padding:20px;text-align:center;color:var(--t2)">No competitors found</td></tr>';
      return;
    }

    /* Aggregate stats per competitor using live thresholds */
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

  // Fetch snapshots for this competitor
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

  // Set view from preference
  const defaultGrid = $('pref-default-grid')?.checked !== false;
  setCompView(defaultGrid ? 'grid' : 'list');

  // Load saved notes for this competitor
  loadCompNotes();
}

/* ── Competitor notes ── */
let compNotesDirty = false;

async function loadCompNotes() {
  const ta = $('comp-notes-text');
  const status = $('comp-notes-status');
  if (!ta) return;
  ta.value = '';
  compNotesDirty = false;
  $('comp-notes-dirty').style.display = 'none';
  status.textContent = 'Loading…';
  try {
    const { data, error } = await sb.from('competitors').select('notes').eq('id', currentCompId).single();
    if (error) throw new Error(error.message);
    ta.value = data?.notes || '';
    status.textContent = data?.notes ? 'Saved' : 'No notes yet';
  } catch (e) {
    status.textContent = 'Could not load notes';
  }
}

function markCompNotesDirty() {
  compNotesDirty = true;
  $('comp-notes-dirty').style.display = '';
  $('comp-notes-status').textContent = '';
}

async function saveCompNotes() {
  const ta  = $('comp-notes-text');
  const btn = $('comp-notes-save');
  const status = $('comp-notes-status');
  if (!ta || currentCompId == null) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="ti ti-loader"></i> Saving…';
  try {
    await ensureFreshSession();
    const { error } = await sb.from('competitors').update({ notes: ta.value }).eq('id', currentCompId);
    if (error) throw new Error(error.message);
    compNotesDirty = false;
    $('comp-notes-dirty').style.display = 'none';
    status.textContent = 'Saved';
    btn.innerHTML = '<i class="ti ti-check"></i> Saved';
    setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 1500);
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = orig;
    status.textContent = 'Save failed: ' + e.message;
  }
}

function scrollToCompNotes() {
  const footer = $('comp-notes-footer');
  const ta = $('comp-notes-text');
  if (footer) footer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (ta) setTimeout(() => ta.focus(), 300);
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

  // GRID
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

  // LIST
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

  // Set back button context
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
    // Fetch SKU info
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
      <div class="sku-img"><i class="ti ti-photo" style="font-size:24px"></i></div>
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

    // Fetch competitor snapshots for this SKU — include unit_qty via skus join
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

  // Full page buttons
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

    // Build league table: insert UKPOS among competitors ranked by ex-VAT price ascending
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

    // Sort by ex-VAT price ascending (cheapest first = rank 1)
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
  $('run-history').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
  try {
    const d = await authFetch('/runs');
    const runs = d.data || [];

    /* Batch progress summary */
    let progressHtml = '';
    try {
      const { data: prog } = await sb.from('scrape_progress').select('*').order('updated_at',{ascending:false}).limit(1);
      const p = prog?.[0];
      if (p) {
        const totalSkus = 2634;
        const done = p.last_offset || 0;
        const pct  = Math.min(100, Math.round(done / totalSkus * 100));
        const daysLeft = Math.max(0, Math.ceil((totalSkus - done) / 570));
        progressHtml = `
          <div style="grid-column:1/-1;background:var(--bb);border:1px solid var(--bbd);border-radius:var(--rl);padding:12px 14px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <div style="font-weight:500;font-size:13px;color:#042c53">Batch search progress — full SKU pass</div>
              <div style="font-size:12px;color:var(--blu)">${done.toLocaleString()} / ${totalSkus.toLocaleString()} SKUs · ~${daysLeft} weekday${daysLeft===1?'':'s'} left</div>
            </div>
            <div style="height:8px;border-radius:4px;background:rgba(24,95,165,.15);overflow:hidden">
              <div style="height:100%;width:${pct}%;background:var(--blu);border-radius:4px;transition:width .3s"></div>
            </div>
            <div style="font-size:11px;color:var(--blu);margin-top:4px">${pct}% · cycles back to start when complete · 570 SKUs/day Mon–Fri</div>
          </div>`;
      }
    } catch(e) { /* scrape_progress table optional */ }

    /* Summary cards from most recent run */
    const last = runs[0];
    if (last) {
      $('run-summary-cards').innerHTML = progressHtml + `
        <div class="metric m-gray"><span class="ml">Last run</span><span class="mv" style="font-size:18px">${ts(last.completed_at||last.started_at)}</span><span class="ms">${last.run_type||'matched'}</span></div>
        <div class="metric m-g"><span class="ml">Succeeded</span><span class="mv">${last.skus_succeeded||0}</span><span class="ms">prices captured</span></div>
        <div class="metric m-a"><span class="ml">Failed</span><span class="mv">${last.skus_failed||0}</span><span class="ms">retried next run</span></div>
        <div class="metric m-gray"><span class="ml">Duration</span><span class="mv" style="font-size:18px">${last.duration_sec?Math.round(last.duration_sec/60)+'m':'—'}</span><span class="ms">${last.status||''}</span></div>`;
    } else {
      $('run-summary-cards').innerHTML = progressHtml || '<div style="color:var(--t2);font-size:12px;padding:8px">No runs recorded yet</div>';
    }

    /* History table */
    if (!runs.length) {
      $('run-history').innerHTML = '<div style="color:var(--t2);font-size:12px;padding:8px">No run history</div>';
      return;
    }
    $('run-history-sub').textContent = `last ${runs.length} runs`;
    $('run-history').innerHTML = `<table class="run-table"><thead><tr>
      <th>Started</th><th>Type</th><th>Status</th><th>OK</th><th>Fail</th><th>Duration</th>
    </tr></thead><tbody>${runs.map(r => {
      const statusColor = r.status==='completed'?'var(--grn)':r.status==='running'?'var(--blu)':r.status==='failed'?'var(--red)':'var(--t2)';
      return `<tr>
        <td>${ts(r.started_at)}</td>
        <td>${r.run_type||'—'}</td>
        <td><span style="color:${statusColor};font-weight:500">${r.status||'—'}</span></td>
        <td style="color:var(--grn)">${r.skus_succeeded||0}</td>
        <td style="color:${r.skus_failed?'var(--red)':'var(--t3)'}">${r.skus_failed||0}</td>
        <td>${r.duration_sec?Math.round(r.duration_sec/60)+'m':'—'}</td>
      </tr>`;
    }).join('')}</tbody></table>`;
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
    if (!comps?.length) { $('comp-sett-tbody').innerHTML = '<tr><td colspan="5" style="padding:16px;text-align:center;color:var(--t2)">No competitors</td></tr>'; return; }

    const unknownCount = comps.filter(c => !c.vat_status || c.vat_status === 'unknown').length;
    if (unknownCount > 0) {
      $('comp-vat-note').style.display = '';
      $('comp-vat-note-text').textContent = `${unknownCount} competitor${unknownCount===1?'':'s'} ${unknownCount===1?'has':'have'} unknown VAT status — set these before trusting differentials.`;
    }
    $('comp-sett-sub').textContent = `${comps.length} competitors · ${comps.filter(c=>c.active).length} active`;

    $('comp-sett-tbody').innerHTML = comps.map(c => `
      <tr>
        <td style="font-weight:500">${c.name}</td>
        <td style="color:var(--t2)">${c.domain||'—'}</td>
        <td>
          <div class="vat-toggle">
            <button class="vt-opt ${c.vat_status==='ex'?'s-ex':''}"   onclick="setVatStatus(${c.id},'ex',this)">Ex</button>
            <button class="vt-opt ${c.vat_status==='inc'?'s-inc':''}" onclick="setVatStatus(${c.id},'inc',this)">Inc</button>
            <button class="vt-opt ${(!c.vat_status||c.vat_status==='unknown')?'s-unk':''}" onclick="setVatStatus(${c.id},'unknown',this)">?</button>
          </div>
        </td>
        <td style="color:var(--t2);font-size:11px">${c.scrape_method||'auto'}</td>
        <td><label style="cursor:pointer"><input type="checkbox" ${c.active?'checked':''} onchange="setCompActive(${c.id},this.checked)" style="accent-color:var(--orange)"></label></td>
      </tr>`).join('');
  } catch (e) {
    $('comp-sett-tbody').innerHTML = `<tr><td colspan="5" style="color:var(--red);padding:8px">${e.message}</td></tr>`;
  }
}

async function setVatStatus(compId, status, btn) {
  const wrap = btn.parentElement;
  wrap.querySelectorAll('.vt-opt').forEach(b => b.className = 'vt-opt');
  btn.className = 'vt-opt ' + (status==='ex'?'s-ex':status==='inc'?'s-inc':'s-unk');
  try {
    const { error } = await sb.from('competitors').update({ vat_status: status }).eq('id', compId);
    if (error) throw new Error(error.message);
  } catch (e) { alert('Update failed: ' + e.message); }
}

async function setCompActive(compId, active) {
  try {
    const { error } = await sb.from('competitors').update({ active }).eq('id', compId);
    if (error) throw new Error(error.message);
  } catch (e) { alert('Update failed: ' + e.message); }
}

/* ════════════════════════════════════════
   SETTINGS — USERS (super_admin only)
════════════════════════════════════════ */
async function loadUsers() {
  if (currentProfile?.role !== 'super_admin') {
    $('users-list').innerHTML = '<div style="padding:12px;color:var(--t2);font-size:12px">Only administrators can manage users.</div>';
    $('pending-list').innerHTML = '';
    return;
  }
  try {
    const [{ data: profiles }, { data: requests }] = await Promise.all([
      sb.from('profiles').select('*').order('created_at', { ascending: false }),
      sb.from('access_requests').select('*').order('requested_at', { ascending: false }),
    ]);

    const active  = (profiles||[]).filter(p => p.status === 'approved');
    const pending = (profiles||[]).filter(p => p.status === 'pending');

    $('users-sub').textContent = `${active.length} active user${active.length===1?'':'s'}`;

    $('users-list').innerHTML = active.length ? active.map(u => {
      const initials = (u.full_name||u.email||'?').split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();
      return `<div class="user-row">
        <div class="avatar">${initials}</div>
        <div style="flex:1">
          <div style="font-weight:500">${u.full_name||u.email}</div>
          <div style="font-size:11px;color:var(--t2)">${u.email} · ${u.role||'viewer'}</div>
        </div>
        ${u.id !== currentProfile.id && u.role !== 'super_admin'
          ? `<button class="btn sm danger" onclick="revokeUser('${u.id}',this)">Revoke</button>`
          : `<span class="badge b-blu">${u.role==='super_admin'?'Admin':'You'}</span>`}
      </div>`;
    }).join('') : '<div style="padding:12px;color:var(--t2);font-size:12px">No active users</div>';

    // Combine pending profiles + access_requests
    const pendingHtml = [
      ...(requests||[]).map(r => `
        <div class="user-row">
          <div class="avatar" style="background:var(--ab);color:var(--amb)"><i class="ti ti-clock" style="font-size:14px"></i></div>
          <div style="flex:1">
            <div style="font-weight:500">${r.full_name||r.email}</div>
            <div style="font-size:11px;color:var(--t2)">${r.email} · requested ${new Date(r.requested_at).toLocaleDateString('en-GB')}</div>
          </div>
          <button class="btn sm prim" onclick="approveRequest('${r.email}','${(r.full_name||'').replace(/'/g,"\\'")}',this)">Approve</button>
          <button class="btn sm danger" onclick="rejectRequest('${r.email}',this)">Reject</button>
        </div>`),
      ...pending.map(u => `
        <div class="user-row">
          <div class="avatar" style="background:var(--ab);color:var(--amb)"><i class="ti ti-clock" style="font-size:14px"></i></div>
          <div style="flex:1">
            <div style="font-weight:500">${u.full_name||u.email}</div>
            <div style="font-size:11px;color:var(--t2)">${u.email}</div>
          </div>
          <button class="btn sm prim" onclick="approveUser('${u.id}','${u.email}',this)">Approve</button>
          <button class="btn sm danger" onclick="rejectUser('${u.id}',this)">Reject</button>
        </div>`),
    ].join('');

    $('pending-list').innerHTML = pendingHtml || '<div style="padding:12px;color:var(--t2);font-size:12px">No pending requests</div>';

  } catch (e) {
    $('users-list').innerHTML = `<div style="color:var(--red);padding:8px">${e.message}</div>`;
  }
}

async function approveRequest(email, fullName, btn) {
  btn.disabled = true; btn.textContent = 'Approving…';
  try {
    const { data: { session } } = await sb.auth.getSession();
    const res = await fetch('https://uaqakssusydpjzrcznhb.supabase.co/functions/v1/approve-user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + session.access_token },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) { const e = await res.json().catch(()=>{}); throw new Error(e?.error||'Failed'); }
    loadUsers();
  } catch (e) { btn.disabled = false; btn.textContent = 'Approve'; alert('Approve failed: ' + e.message); }
}

async function rejectRequest(email, btn) {
  if (!confirm(`Reject request from ${email}?`)) return;
  btn.disabled = true;
  try {
    const { error } = await sb.from('access_requests').delete().eq('email', email);
    if (error) throw new Error(error.message);
    loadUsers();
  } catch (e) { btn.disabled = false; alert(e.message); }
}

/* ════════════════════════════════════════
   THRESHOLDS
════════════════════════════════════════ */
function updateThreshPreview() {
  const red = +$('t-red').value || 10;
  const amb = +$('t-amb').value || 5;
  const par = +$('t-par').value || 2;
  $('t-grn-derived').textContent = par;
  $('prev-r').textContent = `−${(red+2.4).toFixed(1)}%`;
  $('prev-a').textContent = `−${((red+amb)/2).toFixed(1)}%`;
  $('prev-p').textContent = `±${(par*0.65).toFixed(1)}%`;
  $('prev-g').textContent = `+${(par+3.8).toFixed(1)}%`;
  $('thresh-live').innerHTML = `
    <span style="font-weight:500">Live preview:</span>
    <span class="tc-swatch" style="background:var(--rb);color:var(--red)">Critical ≤ −${red}%</span>
    <span class="tc-swatch" style="background:var(--ab);color:var(--amb)">Warning −${amb}% to −${red}%</span>
    <span class="tc-swatch" style="background:var(--bg);color:var(--t2);border:1px solid var(--border)">Parity ±${par}%</span>
    <span class="tc-swatch" style="background:var(--gb);color:var(--grn)">Cheaper ≥ +${par}%</span>`;
}

function saveThresholds() {
  T.red = +$('t-red').value || 10;
  T.amb = +$('t-amb').value || 5;
  T.par = +$('t-par').value || 2;
  try { localStorage.setItem('pw_thresholds', JSON.stringify(T)); } catch(e) {}
  applyThresholdCSS();
  /* Re-render anything currently visible */
  if ($('p-alerts').classList.contains('active')) loadDashboard();
  if ($('p-bycomp').classList.contains('active')) loadByCompetitor();
  const btn = event?.target?.closest('button');
  if (btn) { const o = btn.innerHTML; btn.innerHTML = '<i class="ti ti-check"></i> Saved'; setTimeout(()=>btn.innerHTML=o, 1500); }
}

function resetThresholds() {
  $('t-red').value = 10; $('t-amb').value = 5; $('t-par').value = 2;
  updateThreshPreview();
  saveThresholds();
}

/* ════════════════════════════════════════
   MANUAL LOOKUP / REFRESH
════════════════════════════════════════ */
async function lookupSKU(skuId, btn) {
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i> Scraping…'; }
  try {
    await authPost('/lookup', { sku_id: skuId });
    if (btn) { btn.innerHTML = '<i class="ti ti-check"></i> Queued'; setTimeout(()=>{ btn.disabled=false; btn.innerHTML=orig; }, 2000); }
    /* Refresh drawer if open on this SKU */
    if (drawerSkuId === skuId) setTimeout(() => openDrawer(skuId, $('dr-name').textContent, $('dr-price').textContent, drawerFromPanel), 800);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
    alert('Lookup failed: ' + e.message);
  }
}

async function scrapeRow(skuId, compId, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i>';
  try {
    await authPost('/lookup', { sku_id: skuId, competitor_id: compId });
    btn.innerHTML = '<i class="ti ti-check" style="font-size:13px;color:var(--grn)"></i>';
    setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 2000);
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    alert('Scrape failed: ' + e.message);
  }
}

async function manualRefresh() {
  const btn = event?.target?.closest('button');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i> Refreshing…'; }
  try {
    loadDashboard();
    if ($('p-skus').classList.contains('active'))  loadSKUs();
    if ($('p-review').classList.contains('active')) loadReview();
    if (btn) { setTimeout(()=>{ btn.disabled=false; btn.innerHTML=orig; }, 1200); }
  } catch (e) {
    if (btn) { btn.disabled=false; btn.innerHTML=orig; }
  }
}

/* ════════════════════════════════════════
   SORTING ENGINE
════════════════════════════════════════ */
const sortState = {
  bycomp:  { col: null, dir: 1 },
  skus:    { col: null, dir: 1 },
  compSku: { col: null, dir: 1 },
  skuComp: { col: null, dir: 1 },
};
let bycompData = [], skusData = [], compSkuData = [], skuCompData = [];

/* Comparable value extractor — handles numbers, prices, dates, strings */
function cmpVal(v) {
  if (v === null || v === undefined) return -Infinity;
  if (typeof v === 'number') return v;
  if (typeof v === 'string') {
    const n = parseFloat(v.replace(/[£,%]/g,''));
    if (!isNaN(n) && /[\d.]/.test(v)) return n;
    return v.toLowerCase();
  }
  return v;
}

function updateSortHeaders(tableId, stateKey, col) {
  const table = $(tableId);
  if (!table) return;
  table.querySelectorAll('th.sortable').forEach(th => th.classList.remove('sort-asc','sort-desc'));
  if (!col) return;
  const dir = sortState[stateKey].dir;
  /* find the th whose onclick references this col */
  table.querySelectorAll('th.sortable').forEach(th => {
    const oc = th.getAttribute('onclick') || '';
    if (oc.includes(`'${col}'`)) th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
  });
}

function toggleSort(stateKey, col) {
  const st = sortState[stateKey];
  if (st.col === col) st.dir *= -1;
  else { st.col = col; st.dir = 1; }
  return st;
}

/* ── By-competitor sort ── */
function sortByCompTable(col) {
  const st = toggleSort('bycomp', col);
  const keyMap = {
    name:'name', domain:'domain', vat_status:'vat_status',
    matched:'_matched', critical:'_critical', warning:'_warning', cheaper:'_cheaper', parity:'_parity'
  };
  const k = keyMap[col] || col;
  bycompData.sort((a,b) => {
    const av = cmpVal(a[k]), bv = cmpVal(b[k]);
    if (av < bv) return -1*st.dir;
    if (av > bv) return  1*st.dir;
    return 0;
  });
  renderByCompRows(bycompData);
  updateSortHeaders('bycomp-table', 'bycomp', col);
}

function renderByCompRows(rows) {
  $('bycomp-tbody').innerHTML = rows.map(c => {
    const slug = slugify(c.name);
    return `<tr class="tr-link" onclick="go('comp-detail',{compId:${c.id},compName:'${(c.name||'').replace(/'/g,"\\'")}',compSlug:'${slug}',compDomain:'${c.domain||''}',compVat:'${c.vat_status||'unknown'}'})">
      <td style="font-weight:500">${c.name}</td>
      <td style="color:var(--t2)">${c.domain||'—'}</td>
      <td>${vatPill(c.vat_status||'unknown')}</td>
      <td style="font-weight:500">${c._matched}</td>
      <td>${c._critical?`<span class="d-r">${c._critical}</span>`:'<span style="color:var(--t3)">0</span>'}</td>
      <td>${c._warning?`<span class="d-a">${c._warning}</span>`:'<span style="color:var(--t3)">0</span>'}</td>
      <td>${c._cheaper?`<span class="d-g">${c._cheaper}</span>`:'<span style="color:var(--t3)">0</span>'}</td>
      <td style="color:var(--t2)">${c._parity}</td>
    </tr>`;
  }).join('');
}

/* ── All-SKUs sort ── */
function sortSkusTable(col) {
  const st = toggleSort('skus', col);
  const keyMap = {
    sku_id:'sku_id', short_title:'short_title', our_price:'our_price',
    competitor_name:'competitor_name', their_price:'competitor_price',
    diff:'diff_pct_normalised', availability:'availability', scraped_at:'scraped_at'
  };
  const k = keyMap[col] || col;
  skusData.sort((a,b) => {
    let av, bv;
    if (col === 'diff') { av = cmpVal(a.diff_pct_normalised??a.diff_pct); bv = cmpVal(b.diff_pct_normalised??b.diff_pct); }
    else { av = cmpVal(a[k]); bv = cmpVal(b[k]); }
    if (av < bv) return -1*st.dir;
    if (av > bv) return  1*st.dir;
    return 0;
  });
  renderSkusRows(skusData);
  updateSortHeaders('skus-table', 'skus', col);
}

function renderSkusRows(rows) {
  $('sku-tbody').innerHTML = rows.map(r => {
    const diff = r.diff_pct_normalised ?? r.diff_pct;
    const raw  = r.competitor_price ? parseFloat(r.competitor_price) : null;
    const vat  = r.competitor_vat || r.competitor_vat_default || 'unknown';
    const ex   = raw ? normalisePrice(raw, vat) : null;
    const compQty = r.competitor_unit_qty || 1;
    const perU = (compQty > 1 && ex) ? ` ${perUnitLabel(ex, compQty)}` : '';
    return `<tr class="${rowClass(diff)} tr-link" onclick="openDrawer('${r.sku_id}','${(r.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(r.our_price)}','skus')">
      <td>${skuLink(r)}</td>
      <td style="font-size:11px;color:var(--t2);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.short_title||''}</td>
      <td style="font-weight:500">${fmtPrice(r.our_price)}</td>
      <td style="font-size:11px;color:var(--t2)">${r.competitor_name||'—'}</td>
      <td style="font-weight:500">${ex ? fmtPrice(ex) : '—'}${ex ? ` <span style="font-size:9px;color:var(--t3)">${vatPill(vat)}</span>` : ''}${perU}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span></td>
      <td style="color:var(--t2)">—</td>
      <td>${stockBadge(r.availability)}</td>
      <td style="font-size:10px;color:var(--t3)">${ts(r.scraped_at)}</td>
      <td><button class="btn sm ghost" onclick="event.stopPropagation();openDrawer('${r.sku_id}','${(r.short_title||'').replace(/'/g,"\\'")}','${fmtPrice(r.our_price)}','skus')">→</button></td>
    </tr>`;
  }).join('');
}

/* ── Competitor-detail list sort ── */
function sortCompSkuTable(col) {
  const st = toggleSort('compSku', col);
  const keyMap = {
    sku_id:'sku_id', short_title:'short_title', our_price:'our_price',
    their_price:'competitor_price', diff:'diff_pct_normalised',
    availability:'availability', scraped_at:'scraped_at'
  };
  const k = keyMap[col] || col;
  compSkusFiltered.sort((a,b) => {
    let av, bv;
    if (col === 'diff') { av = cmpVal(a.diff_pct_normalised??a.diff_pct); bv = cmpVal(b.diff_pct_normalised??b.diff_pct); }
    else if (col === 'their_price') {
      av = a.competitor_price ? normalisePrice(parseFloat(a.competitor_price), a.competitor_vat||a.competitor_vat_default||'unknown') : -Infinity;
      bv = b.competitor_price ? normalisePrice(parseFloat(b.competitor_price), b.competitor_vat||b.competitor_vat_default||'unknown') : -Infinity;
    }
    else { av = cmpVal(a[k]); bv = cmpVal(b[k]); }
    if (av < bv) return -1*st.dir;
    if (av > bv) return  1*st.dir;
    return 0;
  });
  renderCompDetail();
  updateSortHeaders('comp-sku-table', 'compSku', col);
}

/* ── SKU-detail competitor sort ── */
function sortSkuCompTable(col) {
  const st = toggleSort('skuComp', col);
  skuCompData.sort((a,b) => {
    let av, bv;
    if (col === 'diff')   { av = cmpVal(a.diff_pct_normalised??a.diff_pct); bv = cmpVal(b.diff_pct_normalised??b.diff_pct); }
    else if (col === 'price') {
      av = a.competitor_price ? normalisePrice(parseFloat(a.competitor_price), a.competitor_vat||a.competitor_vat_default||'unknown') : -Infinity;
      bv = b.competitor_price ? normalisePrice(parseFloat(b.competitor_price), b.competitor_vat||b.competitor_vat_default||'unknown') : -Infinity;
    }
    else if (col === 'name') { av = cmpVal(a.competitor_name); bv = cmpVal(b.competitor_name); }
    else if (col === 'vat')  { av = cmpVal(a.competitor_vat||a.competitor_vat_default); bv = cmpVal(b.competitor_vat||b.competitor_vat_default); }
    else { av = cmpVal(a[col]); bv = cmpVal(b[col]); }
    if (av < bv) return -1*st.dir;
    if (av > bv) return  1*st.dir;
    return 0;
  });
  renderSkuCompRows(skuCompData);
  updateSortHeaders('sku-comp-table', 'skuComp', col);
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

    // Pack quantities: ours comes from the skus join (our_unit_qty via view, or
    // skus.unit_qty via the embedded select); theirs from the persisted snapshot.
    const ourQty  = (r.our_unit_qty ?? r.skus?.unit_qty) || 1;
    const compQty = r.competitor_unit_qty || 1;
    const perUnitDiffer = ourQty !== compQty && (ourQty > 1 || compQty > 1);

    // Per-unit sub-label under the competitor price. Shown whenever the two
    // sides differ in pack size — including the case where THEIR qty is 1 but
    // ours is a pack, so the "/unit ×1" line makes the like-for-like basis
    // explicit. Built inline (not via perUnitLabel) because that shared helper
    // deliberately suppresses the qty-1 case for the All-SKUs table; here we
    // want it. (Our own per-unit price is shown in the SKU hero above.)
    let theirPerUnit = '';
    if (perUnitDiffer && ex) {
      const per = ex / compQty;
      const perTxt = per < 1 ? `£${per.toFixed(3)}` : `£${per.toFixed(2)}`;
      theirPerUnit = `<div><span style="font-size:10px;color:var(--t3);white-space:nowrap">${perTxt}/unit ×${compQty}</span></div>`;
    }

    // Small tag on the gap cell when the comparison was normalised per-unit
    const basisTag = perUnitDiffer
      ? `<div style="font-size:9px;color:var(--amb);background:var(--ab);border-radius:3px;padding:0 4px;display:inline-block;margin-top:2px">per-unit basis</div>`
      : '';

    return `<tr class="${rowClass(diff)}" id="skurow-${rowId}">
      <td style="font-weight:500">${r.competitor_name||'—'}<div style="font-size:10px;color:var(--t2)">${r.competitor_domain||''}</div></td>
      <td style="font-weight:500">${ex
        ? (r.competitor_url
            ? `<a href="${r.competitor_url}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:underline;text-decoration-color:rgba(0,0,0,.2);text-underline-offset:2px" onclick="event.stopPropagation()">${fmtPrice(ex)} <i class="ti ti-external-link" style="font-size:10px;color:var(--t3)"></i></a>`
            : fmtPrice(ex))
        : '—'}${theirPerUnit}</td>
      <td>${vatPill(vat)}</td>
      <td><span class="${diffClass(diff)}">${diffLabel(diff)}</span>${basisTag}</td>
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
          <input id="match-url-${rowId}" type="url" name="match-url-${rowId}" value="${r.competitor_url||''}" placeholder="https://competitor.com/product-page"
            autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-form-type="other"
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

/* ── Match URL editing (SKU detail table) ── */
function editMatchUrl(rowId, skuId, compId, currentUrl, btn) {
  const editRow = $(`skurow-edit-${rowId}`);
  if (editRow) {
    const showing = editRow.style.display !== 'none';
    document.querySelectorAll('[id^="skurow-edit-"]').forEach(r => r.style.display = 'none');
    editRow.style.display = showing ? 'none' : '';
    if (!showing) { const inp = $(`match-url-${rowId}`); if (inp) inp.focus(); }
  }
}

function cancelEditUrl(rowId) {
  const editRow = $(`skurow-edit-${rowId}`);
  if (editRow) editRow.style.display = 'none';
}

async function saveMatchUrl(rowId, skuId, compId, btn) {
  const input = $(`match-url-${rowId}`);
  const msg   = $(`match-url-msg-${rowId}`);
  const url   = (input?.value || '').trim();
  if (url && !url.startsWith('http')) {
    msg.style.display = 'block'; msg.style.color = 'var(--red)';
    msg.textContent = 'URL must start with http(s)://';
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader"></i> Saving…';
  try {
    await ensureFreshSession();
    const { error } = await sb.from('competitor_matches')
      .update({ competitor_url: url || null, human_reviewed: true, reviewed_at: new Date().toISOString() })
      .eq('sku_id', skuId).eq('competitor_id', compId);
    if (error) throw new Error(error.message);
    msg.style.display = 'block'; msg.style.color = 'var(--grn)';
    msg.innerHTML = '<i class="ti ti-check"></i> Saved — queuing scrape…';
    await authPost('/lookup', { sku_id: skuId, competitor_id: compId }).catch(()=>{});
    setTimeout(() => { cancelEditUrl(rowId); loadSkuDetail({ skuId }); }, 900);
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    msg.style.display = 'block'; msg.style.color = 'var(--red)';
    msg.textContent = 'Save failed: ' + e.message;
  }
}

/* ════════════════════════════════════════
   STATE FLAGS + INIT
════════════════════════════════════════ */
let skusLoaded = false; // retained: no longer gates loading; kept to avoid touching unrelated refs

/* React to hash changes (back/forward buttons) */
window.addEventListener('hashchange', () => {
  if (currentProfile && currentProfile.status === 'active') restoreRoute();
});

/* Keyboard: Esc closes drawer */
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

/* Kick everything off */
updateThreshPreview();
bootstrap();
