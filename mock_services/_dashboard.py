"""Standalone mock dashboard aligned with the latest removed Ops mock-services UI."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Ops Mock Services Control Panel</title>
<style>
:root{
  --bg:#f4f7fb;
  --panel:#ffffff;
  --muted:#64748b;
  --text:#0f172a;
  --border:#dbe4f0;
  --accent:#0f766e;
  --accent-soft:#ccfbf1;
  --danger:#dc2626;
  --danger-soft:#fee2e2;
  --warn:#d97706;
  --warn-soft:#fef3c7;
  --ok:#15803d;
  --ok-soft:#dcfce7;
  --shadow:0 18px 40px rgba(15,23,42,.08);
}
*{box-sizing:border-box}
body{
  margin:0;
  font-family:Inter,Segoe UI,Arial,sans-serif;
  color:var(--text);
  background:
    radial-gradient(circle at top left, #dff6ff 0, transparent 28%),
    radial-gradient(circle at top right, #ecfccb 0, transparent 22%),
    linear-gradient(180deg,#f8fbff 0%,#eef4fb 100%);
}
.shell{max-width:1440px;margin:0 auto;padding:28px 24px 40px}
.hero{
  display:flex;justify-content:space-between;gap:24px;align-items:flex-start;
  padding:28px;border:1px solid rgba(15,118,110,.12);border-radius:24px;
  background:linear-gradient(135deg,rgba(255,255,255,.95),rgba(236,253,245,.88));
  box-shadow:var(--shadow);margin-bottom:24px;
}
.hero h1{margin:0 0 8px;font-size:32px;line-height:1.05}
.hero p{margin:0;color:var(--muted);max-width:760px;line-height:1.6}
.hero-actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.pill{
  display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:999px;
  background:#fff;border:1px solid var(--border);font-size:13px;color:var(--muted)
}
.layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:24px}
.card{
  background:rgba(255,255,255,.96);border:1px solid var(--border);border-radius:22px;
  box-shadow:var(--shadow)
}
.sidebar{padding:20px;position:sticky;top:18px;height:fit-content}
.sidebar h2,.main h2{margin:0 0 8px;font-size:18px}
.subtle{color:var(--muted);font-size:13px;line-height:1.5}
.field{display:flex;flex-direction:column;gap:6px;margin-top:14px}
.field label{font-size:12px;font-weight:700;letter-spacing:.02em;color:#334155}
.field input,.field select,.field textarea{
  width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:12px;
  background:#fff;font-size:14px;color:var(--text)
}
.field textarea{min-height:90px;resize:vertical}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
button{
  border:none;border-radius:12px;padding:10px 14px;font-size:14px;font-weight:700;cursor:pointer;
  transition:transform .12s ease,box-shadow .12s ease,background .12s ease;
}
button:hover{transform:translateY(-1px)}
.btn-primary{background:var(--accent);color:#fff;box-shadow:0 12px 24px rgba(15,118,110,.18)}
.btn-secondary{background:#fff;color:#0f172a;border:1px solid var(--border)}
.btn-danger{background:var(--danger);color:#fff}
.btn-ghost{background:#eef2ff;color:#3730a3}
.session-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.session-chip{
  display:inline-flex;padding:7px 10px;border-radius:999px;
  background:#eef2ff;color:#3730a3;font-size:12px;font-weight:700
}
.main{display:flex;flex-direction:column;gap:20px}
.toolbar{
  display:flex;align-items:center;justify-content:space-between;gap:18px;
  padding:20px 24px
}
.toolbar-copy h2{margin-bottom:6px}
.toolbar-copy p{margin:0;color:var(--muted);font-size:14px}
.statbar{display:flex;gap:10px;flex-wrap:wrap}
.stat{
  min-width:120px;padding:12px 14px;border-radius:16px;border:1px solid var(--border);
  background:#f8fafc
}
.stat strong{display:block;font-size:22px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.panel{padding:20px 20px 18px}
.panel-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:16px}
.panel-head h3{margin:0 0 6px;font-size:18px}
.panel-head p{margin:0;color:var(--muted);font-size:13px;line-height:1.5}
.badge{
  display:inline-flex;align-items:center;border-radius:999px;padding:6px 10px;
  font-size:12px;font-weight:800;letter-spacing:.02em
}
.badge-ok{background:var(--ok-soft);color:var(--ok)}
.badge-danger{background:var(--danger-soft);color:var(--danger)}
.badge-warn{background:var(--warn-soft);color:var(--warn)}
.badge-neutral{background:#e2e8f0;color:#334155}
.group{display:flex;flex-direction:column;gap:12px}
.target{
  border:1px solid var(--border);border-radius:18px;padding:14px;background:#f8fbff
}
.target-top{
  display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px
}
.target-top h4{margin:0 0 4px;font-size:15px}
.target-top small{display:block;color:var(--muted);line-height:1.5}
.target-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.toggle{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600}
.hint{margin-top:10px;color:var(--muted);font-size:12px;line-height:1.5}
.mini-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.mini-actions button{padding:8px 11px;font-size:12px;border-radius:10px}
.job-row{
  display:flex;justify-content:space-between;gap:12px;align-items:center;
  padding:12px 14px;border-radius:14px;background:#f8fafc;border:1px solid #e2e8f0
}
.job-row + .job-row{margin-top:10px}
.job-meta{font-size:12px;color:var(--muted);line-height:1.5}
.mono{
  margin-top:16px;padding:14px;border-radius:16px;background:#0f172a;color:#dbeafe;
  font:12px/1.5 Consolas,monospace;max-height:220px;overflow:auto;white-space:pre-wrap
}
.footer-note{
  margin-top:18px;padding:16px 18px;border:1px dashed #cbd5e1;border-radius:18px;
  color:var(--muted);font-size:13px;line-height:1.6;background:rgba(248,250,252,.9)
}
.toast{
  position:fixed;right:18px;bottom:18px;padding:12px 16px;border-radius:14px;
  background:#0f172a;color:#fff;box-shadow:var(--shadow);opacity:0;pointer-events:none;
  transform:translateY(10px);transition:opacity .2s ease,transform .2s ease;z-index:1000
}
.toast.show{opacity:1;transform:translateY(0)}
@media (max-width:1100px){
  .layout{grid-template-columns:1fr}
  .sidebar{position:static}
  .grid{grid-template-columns:1fr}
}
@media (max-width:760px){
  .hero{flex-direction:column}
  .hero-actions{justify-content:flex-start}
  .toolbar{flex-direction:column;align-items:flex-start}
  .target-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="shell">
  <section class="hero">
    <div>
      <div class="pill">Standalone Mock Console · restored from the latest removed Ops mock-services flow</div>
      <h1>Ops Mock Services Control Panel</h1>
      <p>
        This console drives Mock MMP drift scenarios and provides a lightweight Ops login
        helper. Job/app status and CML application metadata now live in the Mock CML
        Platform dashboard at <a href="http://localhost:9000/" target="_blank">http://localhost:9000/</a>.
      </p>
    </div>
    <div class="hero-actions">
      <button class="btn-secondary" onclick="refreshAll()">Refresh</button>
      <button class="btn-primary" onclick="window.open('/service-ping','_blank')">Service Ping</button>
    </div>
  </section>

  <div class="layout">
    <aside class="card sidebar">
      <h2>Ops Login</h2>
      <p class="subtle">Use a Ops account here so testers can reuse sessions without opening the main Ops frontend.</p>

      <div class="field">
        <label>Username</label>
        <input id="login-user" type="text" value="admin">
      </div>
      <div class="field">
        <label>Password</label>
        <input id="login-pass" type="password" value="admin123">
      </div>
      <div class="field">
        <label>Ops URL</label>
        <input id="login-url" type="text" value="http://host.docker.internal:8000">
      </div>
      <div class="btn-row">
        <button class="btn-primary" onclick="relayopsLogin()">Login</button>
        <button class="btn-secondary" onclick="quickLogin('testuser','test123')">testuser</button>
        <button class="btn-secondary" onclick="quickLogin('bizowner2','biz123')">bizowner2</button>
      </div>
      <div class="btn-row">
        <button class="btn-secondary" onclick="quickLogin('relayopsmember1','relayops123')">relayopsmember1</button>
        <button class="btn-secondary" onclick="quickLogin('relayopsmember2','relayops123')">relayopsmember2</button>
      </div>
      <div id="login-result" class="mono" style="display:none"></div>

      <div class="footer-note">
        <strong>Active Sessions</strong>
        <div id="sessions" class="session-list"></div>
      </div>
    </aside>

    <main class="main">
      <section class="card toolbar">
        <div class="toolbar-copy">
          <h2>Mock MMP</h2>
          <p>Drive MMP drift scenarios. Job/app/application mocks moved to <code>cml_platform.py</code> (port 9000).</p>
        </div>
        <div class="statbar" id="statbar"></div>
      </section>

      <section class="grid">
        <article class="card panel">
          <div class="panel-head">
            <div>
              <h3>Mock MMP</h3>
              <p>Threshold-based drift simulation. API authentication is auto-managed after console login.</p>
            </div>
            <span class="badge badge-neutral" id="mmp-count">0 targets</span>
          </div>

          <div class="group">
            <div class="target">
              <div class="target-grid">
                <div class="field">
                  <label>Ops URL</label>
                  <input id="mmp-relayops-url" type="text">
                </div>
                <div class="field">
                  <label>Service Auth</label>
                  <input id="mmp-auth-mode" type="text" value="Auto-managed by console login" disabled>
                </div>
              </div>
            </div>
            <div id="mmp-targets"></div>
          </div>

          <div class="btn-row">
            <button class="btn-secondary" onclick="addMmpTarget()">Add Target</button>
            <button class="btn-primary" onclick="saveMmp()">Save MMP Mock</button>
          </div>
          <div id="mmp-result" class="mono" style="display:none"></div>
        </article>

      </section>
    </main>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const DEFAULT_MMP_TARGET = () => ({
  mmp_project_id: 'mmp-project-001',
  mmp_model_id: 'model-001',
  job_id: null,
  enabled: false,
  metric_name: 'drift_score',
  metric_value: 0.0,
  threshold: 0.7,
  drift_details: 'Model performance degradation detected',
  interval_seconds: 120,
  last_sent_at: null,
  send_count: 0,
});

let state = null;
let modelOptions = [];
let cachedSessions = [];

function $(id){return document.getElementById(id);}

function toast(message){
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

function pretty(value){
  return JSON.stringify(value, null, 2);
}

function setResult(id, value){
  const el = $(id);
  el.style.display = 'block';
  el.textContent = typeof value === 'string' ? value : pretty(value);
}

async function apiFetch(path, init){
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(init && init.headers ? init.headers : {}) },
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${detail}`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function formatTime(value){
  if (!value) return 'Never';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString();
}

function badgeClass(ok, warn){
  if (warn) return 'badge badge-warn';
  return ok ? 'badge badge-ok' : 'badge badge-danger';
}

function summarizeMmpTarget(target){
  return Number(target.metric_value) >= Number(target.threshold) ? 'Above Threshold' : 'Within Threshold';
}

function renderStatbar(){
  const items = [
    { label: 'MMP Targets', value: state.mock_mmp.targets.length },
  ];
  $('statbar').innerHTML = items.map(item => `
    <div class="stat">
      <span>${item.label}</span>
      <strong>${item.value}</strong>
    </div>
  `).join('');
}

function renderMmp(){
  $('mmp-relayops-url').value = state.mock_mmp.relayops_url || '';
  $('mmp-count').textContent = `${state.mock_mmp.targets.length} targets`;
  $('mmp-targets').innerHTML = state.mock_mmp.targets.map((target, index) => {
    const selectedKey = `${target.mmp_project_id}::${target.mmp_model_id}`;
    return `
      <div class="target">
        <div class="target-top">
          <div>
            <h4>Target #${index + 1}</h4>
            <small>${target.mmp_project_id}/${target.mmp_model_id} · Sent ${target.send_count} · Last ${formatTime(target.last_sent_at)}</small>
          </div>
          <span class="${badgeClass(Number(target.metric_value) < Number(target.threshold), Number(target.metric_value) >= Number(target.threshold))}">${summarizeMmpTarget(target)}</span>
        </div>
          <div class="field">
            <label>Quick Select Target</label>
          <select onchange="selectMmpModel(${index}, this.value)">
            ${modelOptions.map(model => {
              const value = `${model.project_id}::${model.model_id}`;
              const label = model.name ? `${model.name} (${value})` : value;
              return `<option value="${value}" ${value === selectedKey ? 'selected' : ''}>${label}</option>`;
            }).join('')}
          </select>
        </div>
        <div class="target-grid">
          <div class="field">
            <label>Project ID</label>
            <input type="text" value="${target.mmp_project_id}" onchange="patchMmpTarget(${index}, 'mmp_project_id', this.value)">
          </div>
          <div class="field">
            <label>Model ID</label>
            <input type="text" value="${target.mmp_model_id}" onchange="patchMmpTarget(${index}, 'mmp_model_id', this.value)">
          </div>
          <div class="field">
            <label>Job ID</label>
            <input type="number" value="${target.job_id ?? ''}" onchange="patchMmpTarget(${index}, 'job_id', this.value ? Number(this.value) : null)">
          </div>
          <div class="field">
            <label>Metric Name</label>
            <input type="text" value="${target.metric_name}" onchange="patchMmpTarget(${index}, 'metric_name', this.value)">
          </div>
          <div class="field">
            <label>Metric Value</label>
            <input type="number" step="0.01" value="${target.metric_value}" onchange="patchMmpTarget(${index}, 'metric_value', Number(this.value) || 0)">
          </div>
          <div class="field">
            <label>Threshold</label>
            <input type="number" step="0.01" value="${target.threshold}" onchange="patchMmpTarget(${index}, 'threshold', Number(this.value) || 0)">
          </div>
          <div class="field">
            <label>Interval (seconds)</label>
            <input type="number" min="5" value="${target.interval_seconds}" onchange="patchMmpTarget(${index}, 'interval_seconds', Number(this.value) || 120)">
          </div>
          <div class="field">
            <label>Enabled</label>
            <div class="toggle">
              <input type="checkbox" ${target.enabled ? 'checked' : ''} onchange="patchMmpTarget(${index}, 'enabled', this.checked)">
              <span>Participates in scheduled sends</span>
            </div>
          </div>
        </div>
        <div class="field">
          <label>Drift Details</label>
          <input type="text" value="${target.drift_details}" onchange="patchMmpTarget(${index}, 'drift_details', this.value)">
        </div>
        <div class="hint">When metric value is greater than or equal to threshold, the mock report is treated as drift.</div>
        <div class="mini-actions">
          <button class="btn-ghost" onclick="triggerMmpTarget('${target.mmp_project_id}','${target.mmp_model_id}')">Send Now</button>
          <button class="btn-secondary" onclick="removeMmpTarget(${index})" ${state.mock_mmp.targets.length <= 1 ? 'disabled' : ''}>Remove</button>
        </div>
      </div>
    `;
  }).join('');
}

function patchMmpTarget(index, key, value){
  state.mock_mmp.targets[index][key] = value;
}

function addMmpTarget(){
  state.mock_mmp.targets.push(DEFAULT_MMP_TARGET());
  renderMmp();
}

function removeMmpTarget(index){
  if (state.mock_mmp.targets.length <= 1) return;
  state.mock_mmp.targets.splice(index, 1);
  renderMmp();
}

function selectMmpModel(index, value){
  const [projectId, modelId] = value.split('::');
  patchMmpTarget(index, 'mmp_project_id', projectId || '');
  patchMmpTarget(index, 'mmp_model_id', modelId || '');
  renderMmp();
}

async function loadSessions(){
  try{
    const sessions = await apiFetch('/relayops/sessions');
    cachedSessions = Object.keys(sessions);
    const entries = cachedSessions;
    $('sessions').innerHTML = entries.length
      ? entries.map(name => `<span class="session-chip">${name}</span>`).join('')
      : '<span class="subtle">No cached Ops sessions yet.</span>';
    entries.includes($('login-user').value);
  }catch{
    $('sessions').innerHTML = '<span class="subtle">Failed to load sessions.</span>';
  }
}

async function relayopsLogin(){
  try{
    const result = await apiFetch('/relayops/login', {
      method: 'POST',
      body: JSON.stringify({
        username: $('login-user').value,
        password: $('login-pass').value,
        relayops_url: $('login-url').value,
      }),
    });
    setResult('login-result', result);
    if (result.ok) {
      toast(`Logged in as ${result.username}`);
      await loadSessions();
      await refreshAll(true);
    } else {
      toast(result.detail || 'Login failed');
    }
  }catch(error){
    setResult('login-result', String(error));
    toast(error.message || 'Login failed');
  }
}

function quickLogin(username, password){
  $('login-user').value = username;
  $('login-pass').value = password;
  relayopsLogin();
}

async function saveMmp(){
  try{
    state.mock_mmp.relayops_url = $('mmp-relayops-url').value;
    const result = await apiFetch('/control/mock-mmp', {
      method: 'PUT',
      body: JSON.stringify(state.mock_mmp),
    });
    state.mock_mmp = result;
    renderMmp();
    setResult('mmp-result', result);
    toast('Mock MMP updated');
  }catch(error){
    setResult('mmp-result', String(error));
    toast(error.message || 'Failed to save Mock MMP');
  }
}

async function triggerMmpTarget(projectId, modelId){
  if (!projectId || !modelId) {
    toast('Set project/model first');
    return;
  }
  try{
    const query = new URLSearchParams({ project_id: projectId, model_id: modelId });
    const result = await apiFetch(`/control/mock-mmp/trigger-target?${query.toString()}`, {
      method: 'POST',
    });
    setResult('mmp-result', result);
    toast(`Drift report sent for ${projectId}/${modelId}`);
    await refreshAll();
  }catch(error){
    setResult('mmp-result', String(error));
    toast(error.message || 'Failed to trigger MMP target');
  }
}

async function relayopsProxy(path, method='GET', body=null){
  return apiFetch('/relayops/proxy', {
    method: 'POST',
    body: JSON.stringify({
      relayops_url: $('login-url').value,
      username: $('login-user').value,
      method,
      path,
      body,
    }),
  });
}


async function refreshAll(background){
  try{
    const [allState, models] = await Promise.all([
      apiFetch('/control/status'),
      apiFetch('/mmp/models'),
    ]);
    state = allState;
    modelOptions = (models && models.models) ? models.models : [];
    renderStatbar();
    renderMmp();
    await loadSessions();
    if (!background) toast('Mock dashboard refreshed');
  }catch(error){
    toast(error.message || 'Failed to refresh dashboard');
  }
}

refreshAll(true);
loadSessions();
setInterval(() => refreshAll(true), 5000);
</script>
</body>
</html>
"""
