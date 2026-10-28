const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const PAGE_LABELS = { overview: 'Dashboard', animals: 'Animal registry', register: 'Register cattle', verify: 'Verify identity', history: 'Verification history', insurance: 'Insurance policies', alerts: 'Review alerts', analytics: 'Analytics', reports: 'Data exports', system: 'System status', settings: 'Settings', 'farmer-requests': 'Farmer requests', 'not-found': 'Page not found' };
let registryOffset = 0;
let registryQuery = '';
let registrySort = 'created_at';
let registryStatus = 'all';
const pageSize = 25;
let farmerAccount = null;
let farmerRegisterKey = crypto.randomUUID();

const farmerPageLabels = { dashboard: 'My dashboard', animals: 'My cattle', register: 'Register cattle', verify: 'Verify my cattle', insurance: 'My insurance', claims: 'My claims & requests', profile: 'My profile' };

function showAuthScreen(id) {
  const gate = $('#auth-gate');
  gate.hidden = false;
  gate.querySelectorAll(':scope > .auth-card').forEach(card => { card.hidden = card.id !== id; });
  if (id === 'role-selection') $('#login-result').textContent = '';
}

function activateRole(role, account = null) {
  document.body.classList.add('authenticated');
  document.body.classList.toggle('role-farmer', role === 'farmer');
  document.body.classList.toggle('role-admin', role === 'admin');
  $('#auth-gate').hidden = true;
  farmerAccount = account;
  if (role === 'farmer') {
    $('#farmer-shell').hidden = false;
    const name = account?.full_name || 'Farmer';
    $('#farmer-greeting').textContent = name;
    $('#farmer-welcome-name').textContent = `Welcome, ${name}`;
    if (!location.hash.startsWith('#farmer/')) location.hash = '#farmer/dashboard';
    farmerRoute();
  } else {
    $('#farmer-shell').hidden = true;
    route();
  }
}

function clearFarmerViews() {
  ['farmer-animal-rows', 'farmer-policy-rows', 'farmer-request-rows', 'farmer-history', 'farmer-recent-history', 'farmer-expiring-list'].forEach(id => { const el = document.getElementById(id); if (el) el.replaceChildren(); });
  $('#farmer-animal-detail').replaceChildren(); $('#farmer-animal-detail').hidden = true; $('#farmer-animal-list').hidden = false;
  ['farmer-register-form', 'farmer-verify-form', 'farmer-request-form', 'farmer-profile-form', 'farmer-password-form'].forEach(id => document.getElementById(id)?.reset());
  $('#farmer-register-confirmation').replaceChildren(); $('#farmer-image-previews').replaceChildren(); $('#farmer-verify-preview-wrap').hidden = true;
  farmerPreviewUrls.forEach(url => URL.revokeObjectURL(url)); farmerPreviewUrls = [];
  if (farmerVerifyUrl) URL.revokeObjectURL(farmerVerifyUrl); farmerVerifyUrl = null;
  ['farmer-cattle-count', 'farmer-insured-count', 'farmer-expiring-count', 'farmer-open-requests'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = '—'; });
  $('#farmer-greeting').textContent = 'Farmer'; $('#farmer-welcome-name').textContent = 'Welcome';
  farmerAccount = null;
}

async function refreshFarmerDashboard() {
  try {
    const [data, history] = await Promise.all([MuzzleApi.farmerDashboard(), MuzzleApi.farmerHistory()]);
    $('#farmer-cattle-count').textContent = data.total_cattle;
    $('#farmer-insured-count').textContent = data.insured_cattle;
    $('#farmer-expiring-count').textContent = data.upcoming_expirations.length;
    $('#farmer-open-requests').textContent = data.open_requests;
    const expiry = $('#farmer-expiring-list');
    expiry.className = data.upcoming_expirations.length ? 'activity-list' : 'empty-state compact';
    expiry.innerHTML = data.upcoming_expirations.length ? data.upcoming_expirations.map(item => `<a class="activity-row" href="#farmer/animals/${encodeURIComponent(item.animal_id)}"><span><strong>${esc(item.provider)} · ${esc(item.policy_number)}</strong><small>Policy expires ${esc(item.end_date)}</small></span><b>View cattle →</b></a>`).join('') : '<p>No policy expirations in the next 30 days.</p>';
    renderFarmerHistory(history.slice(0, 5), $('#farmer-recent-history'));
  } catch (error) { $('#farmer-cattle-count').textContent = '—'; $('#farmer-insured-count').textContent = '—'; $('#farmer-expiring-list').textContent = error.message; }
}

function renderFarmerHistory(rows, target) {
  target.className = rows.length ? 'activity-list' : 'empty-state compact';
  target.innerHTML = rows.length ? rows.map(row => `<a class="activity-row" href="${row.animal_id ? `#farmer/animals/${encodeURIComponent(row.animal_id)}` : '#farmer/verify'}"><span><strong>${esc(row.decision)} identity result</strong><small>${esc(new Date(row.created_at).toLocaleString())}</small></span><b>Review →</b></a>`).join('') : '<p>No verification activity yet.</p>';
}

async function refreshFarmerAnimals() {
  const target = $('#farmer-animal-rows');
  const search = $('#farmer-animal-search').value;
  try {
    const result = await MuzzleApi.farmerAnimals(search);
    target.innerHTML = result.items.length ? result.items.map(animal => `<tr><td><strong>${esc(animal.name || 'Unnamed animal')}</strong></td><td><code>${esc(animal.muzzle_id || 'Unavailable')}</code></td><td>${esc(animal.ear_tag || '—')}</td><td>${esc(animal.breed || '—')}</td><td><span class="tag">${esc(animal.status.replaceAll('_', ' '))}</span></td><td><a class="row-action" href="#farmer/animals/${encodeURIComponent(animal.id)}">Details</a></td></tr>`).join('') : `<tr><td colspan="6" class="table-empty">${search ? 'No matching cattle found.' : 'No cattle registered yet. Use “Register cattle” to add your first animal.'}</td></tr>`;
  } catch (error) { target.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

async function showFarmerAnimal(id) {
  const host = $('#farmer-animal-detail');
  $('#farmer-animal-list').hidden = true; host.hidden = false; host.innerHTML = '<div class="empty-state compact">Loading your animal…</div>';
  try {
    const animal = await MuzzleApi.farmerAnimal(id);
    host.innerHTML = `<a class="back-link" href="#farmer/animals">← My cattle</a><div class="detail-heading"><div><p class="eyebrow">MY ANIMAL</p><h2>${esc(animal.name || 'Unnamed animal')}</h2><p class="public-muzzle-id">Muzzle ID <code>${esc(animal.muzzle_id || 'Unavailable')}</code></p></div><span class="tag">${esc(animal.status.replaceAll('_', ' '))}</span></div><div class="detail-grid"><div class="detail-cell"><small>EAR TAG</small><strong>${esc(animal.ear_tag || 'Not available')}</strong></div><div class="detail-cell"><small>BREED</small><strong>${esc(animal.breed || 'Not available')}</strong></div><div class="detail-cell"><small>SEX</small><strong>${esc(animal.sex || 'Not available')}</strong></div><div class="detail-cell"><small>DATE OF BIRTH</small><strong>${esc(animal.date_of_birth || 'Not available')}</strong></div><div class="detail-cell"><small>REGISTERED</small><strong>${esc(new Date(animal.created_at).toLocaleDateString())}</strong></div></div><h3>Reference images</h3>${animal.images.length ? `<div class="stored-image-grid">${animal.images.map((image, index) => `<figure><img loading="lazy" src="${MuzzleApi.farmerImageUrl(animal.id, image.id)}" alt="Reference ${index + 1} for ${esc(animal.name)}"><figcaption>Reference ${index + 1} · ${image.width} × ${image.height} · ${esc(image.format)}</figcaption></figure>`).join('')}</div>` : '<p>No reference images.</p>'}<h3>My verification history</h3>${animal.verifications.length ? `<div class="activity-list">${animal.verifications.map(item => `<div class="activity-row"><span><strong>${esc(item.decision)}</strong><small>${esc(new Date(item.created_at).toLocaleString())}</small></span></div>`).join('')}</div>` : '<p>No verification events are linked to this animal.</p>'}<h3>Insurance policies</h3>${animal.policies.length ? `<div class="table-wrap"><table><thead><tr><th>INSURER</th><th>POLICY</th><th>COVERAGE</th><th>END DATE</th><th>STATUS</th></tr></thead><tbody>${animal.policies.map(item => `<tr><td>${esc(item.provider)}</td><td>${esc(item.policy_number)}</td><td>${esc(Number(item.coverage_amount).toLocaleString())}</td><td>${esc(item.end_date)}</td><td>${esc(item.status)}</td></tr>`).join('')}</tbody></table></div>` : '<p>No policy is linked to this animal.</p>'}`;
  } catch (error) { host.innerHTML = `<a class="back-link" href="#farmer/animals">← My cattle</a><p class="form-result error">${esc(error.message)}</p>`; }
}

async function refreshFarmerInsurance() {
  const body = $('#farmer-policy-rows');
  try {
    const policies = await MuzzleApi.farmerInsurance();
    body.innerHTML = policies.length ? policies.map(item => `<tr><td><a href="#farmer/animals/${encodeURIComponent(item.animal_id)}">${esc(item.animal_name || 'Animal')}</a><br><small>${esc(item.ear_tag || '')}</small></td><td>${esc(item.provider)}</td><td>${esc(item.policy_number)}</td><td>${esc(Number(item.coverage_amount).toLocaleString())}</td><td>${esc(item.start_date)}–${esc(item.end_date)}</td><td><span class="tag">${esc(item.status.replaceAll('_', ' '))}</span></td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">No policy records are linked to your cattle.</td></tr>';
  } catch (error) { body.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

async function refreshFarmerRequests() {
  const body = $('#farmer-request-rows');
  try {
    const [items, animals] = await Promise.all([MuzzleApi.farmerRequests(), MuzzleApi.farmerAnimals('')]);
    const select = $('#farmer-request-animal'); const selected = select.value;
    select.innerHTML = '<option value="">Select an animal</option>' + animals.items.map(item => `<option value="${esc(item.id)}">${esc(item.name || item.ear_tag || item.muzzle_id)}</option>`).join('');
    if (selected) select.value = selected;
    body.innerHTML = items.length ? items.map(item => `<tr><td><code>${esc(item.reference)}</code></td><td>${esc(item.kind)}</td><td>${item.animal_id ? `<a href="#farmer/animals/${encodeURIComponent(item.animal_id)}">View animal</a>` : '—'}</td><td>${esc(item.description)}</td><td>${esc(new Date(item.created_at).toLocaleString())}</td><td>${esc(item.status.replaceAll('_', ' '))}</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">You have not submitted any claims or requests.</td></tr>';
  } catch (error) { body.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

async function refreshFarmerProfile() {
  try {
    const profile = await MuzzleApi.farmerProfile();
    const form = $('#farmer-profile-form');
    for (const key of ['full_name', 'mobile', 'email']) form.elements[key].value = profile[key] || '';
  } catch (error) { $('#farmer-profile-result').className = 'form-result error'; $('#farmer-profile-result').textContent = error.message; }
}

async function refreshAdminFarmerRequests() {
  const body = $('#admin-farmer-requests');
  body.innerHTML = '<tr><td colspan="6" class="table-empty">Loading farmer requests…</td></tr>';
  try {
    const items = await MuzzleApi.adminFarmerRequests();
    body.innerHTML = items.length ? items.map(item => `<tr><td><code>${esc(item.reference)}</code></td><td>${esc(item.farmer_name)}<br><small>${esc(item.farmer_mobile)}</small></td><td>${esc(item.animal_name || '—')}</td><td><strong>${esc(item.kind)}</strong><br>${esc(item.description)}</td><td>${esc(new Date(item.created_at).toLocaleString())}</td><td><select class="admin-request-status" data-id="${esc(item.id)}" aria-label="Request status ${esc(item.reference)}"><option value="submitted" ${item.status === 'submitted' ? 'selected' : ''}>Submitted</option><option value="under_review" ${item.status === 'under_review' ? 'selected' : ''}>Under review</option><option value="resolved" ${item.status === 'resolved' ? 'selected' : ''}>Resolved</option><option value="declined" ${item.status === 'declined' ? 'selected' : ''}>Declined</option></select></td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">No farmer claims or requests yet.</td></tr>';
    body.querySelectorAll('.admin-request-status').forEach(select => select.addEventListener('change', async () => {
      const previous = select.dataset.previous || select.querySelector('option[selected]')?.value || 'submitted';
      select.disabled = true;
      try { await MuzzleApi.updateAdminFarmerRequest(select.dataset.id, select.value); select.dataset.previous = select.value; }
      catch (error) { select.value = previous; alert(error.message); }
      finally { select.disabled = false; }
    }));
  } catch (error) { body.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

function farmerRoute() {
  if (!document.body.classList.contains('role-farmer')) return;
  const path = location.hash.slice(1).split('/');
  if (path[0] !== 'farmer') { location.hash = '#farmer/dashboard'; return; }
  const view = path[1] || 'dashboard';
  if (!farmerPageLabels[view]) { location.hash = '#farmer/dashboard'; return; }
  document.querySelectorAll('[data-farmer-view]').forEach(item => item.classList.toggle('route-hidden', item.dataset.farmerView !== view));
  document.querySelectorAll('[data-farmer-nav]').forEach(item => { const active = item.dataset.farmerNav === view; item.classList.toggle('active', active); if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current'); });
  $('#farmer-page-title').textContent = farmerPageLabels[view];
  document.title = `${farmerPageLabels[view]} · MuzzleID`;
  if (view === 'dashboard') refreshFarmerDashboard();
  if (view === 'animals') { $('#farmer-animal-detail').hidden = path[2] ? false : true; $('#farmer-animal-list').hidden = Boolean(path[2]); if (path[2]) showFarmerAnimal(decodeURIComponent(path[2])); else refreshFarmerAnimals(); }
  if (view === 'insurance') refreshFarmerInsurance();
  if (view === 'claims') refreshFarmerRequests();
  if (view === 'verify') MuzzleApi.farmerHistory().then(rows => renderFarmerHistory(rows, $('#farmer-history'))).catch(error => { $('#farmer-history').textContent = error.message; });
  if (view === 'profile') refreshFarmerProfile();
}

async function refreshHealth() {
  const system = $('#system-content');
  const banner = $('#dashboard-status-banner');
  try {
    const health = await MuzzleApi.health();
    const title = !health.database_available ? 'Database connection is unavailable' : health.verification_available ? 'Prototype biometric matching is available' : 'Biometric engine is not configured';
    const description = !health.database_available
      ? 'The API could not confirm its database connection. Registry and registration actions may fail until the database is available.'
      : health.verification_available
        ? 'Prototype full-image embeddings are enabled. No muzzle detector or unseen-animal validation is available; every match requires human confirmation.'
        : `Biometric matching is unavailable: ${health.biometric_engine_reason || 'engine startup checks have not completed.'}`;
    if (banner) {
      $('#status-title').textContent = title;
      $('#status-description').textContent = description;
      $('#status-tag').textContent = !health.database_available ? 'DATABASE ISSUE' : health.verification_available ? 'PROTOTYPE' : 'SETUP REQUIRED';
      banner.dataset.state = !health.database_available ? 'error' : health.verification_available ? 'ready' : 'warning';
    }
    const lock = document.querySelector('#verify-form .locked');
    if (lock) {
      lock.hidden = health.verification_available;
      lock.querySelector('strong').textContent = health.verification_available ? 'Prototype mode only' : 'Verification currently unavailable';
      lock.querySelector('small').textContent = health.verification_available ? 'No muzzle detector; matches are prototype suggestions and require in-person confirmation.' : health.biometric_engine_reason || (!health.model_available ? 'Training and matching calibration artifacts are not installed.' : 'FAISS gallery indexing is unavailable.');
    }
    if (system) {
      const facts = [
        ['API service', 'Online', true],
        ['Database connection', health.database_available ? 'Connected' : 'Unavailable', health.database_available],
        ['Trained embedding model', health.model_available ? `Loaded · ${health.model_version || 'version available'}` : 'Unavailable', health.model_available],
        ['Biometric verification', health.verification_available ? 'Available · prototype only' : health.biometric_engine_reason || 'Unavailable', health.verification_available],
        ['FAISS index', health.index_available ? (health.gallery_index_persisted ? 'Loaded and persisted' : 'Available · index rebuilt at startup') : 'Unavailable', health.index_available],
        ['Muzzle detector', health.detector_available ? 'Available' : 'Not configured', health.detector_available],
      ];
      system.className = 'detail-grid system-grid';
      system.innerHTML = facts.map(([label, value, good]) => `<div class="detail-cell"><small>${esc(label)}</small><strong class="system-state ${good ? 'is-ready' : 'is-limited'}">${esc(value)}</strong></div>`).join('') + `<p class="form-note">${esc((health.model_limitations || []).join(' '))} Identification output is a prototype suggestion and requires in-person confirmation.</p>`;
    }
  } catch (error) {
    if (banner) {
      $('#status-title').textContent = 'MuzzleID API is unavailable';
      $('#status-description').textContent = error.message;
      $('#status-tag').textContent = 'OFFLINE';
      banner.dataset.state = 'error';
    }
    if (system) { system.className = 'empty-state compact error-text'; system.textContent = `Health check failed: ${error.message}`; }
  }
}

async function refreshRegistry() {
  const rows = $('#animal-rows');
  if (!rows) return;
  rows.innerHTML = '<tr><td colspan="7" class="table-empty">Loading animal records…</td></tr>';
  try {
    const result = await MuzzleApi.animals({ q: registryQuery, offset: registryOffset, limit: pageSize, sort: registrySort, status: registryStatus });
    if ($('#animal-count')) $('#animal-count').textContent = result.total;
    $('#registry-summary').textContent = `${result.total} record${result.total === 1 ? '' : 's'} in this organization`;
    rows.innerHTML = result.items.length ? result.items.map(animal => `<tr class="animal-row" data-id="${esc(animal.id)}" tabindex="0"><td><strong>${esc(animal.name || 'Unnamed animal')}</strong></td><td><code class="muzzle-id">${esc(animal.muzzle_id || 'Unavailable')}</code></td><td>${esc(animal.ear_tag || '—')}</td><td>${esc(animal.breed || '—')}</td><td>${animal.image_count}</td><td><span class="tag ${animal.status === 'prototype_ready' ? 'tag-active' : animal.status === 'storage_error' ? 'tag-danger' : ''}">${esc(animal.status.replaceAll('_', ' '))}</span></td><td><a class="row-action" href="#animal/${encodeURIComponent(animal.id)}" aria-label="Open ${esc(animal.name || 'animal')}">View</a></td></tr>`).join('') : '<tr><td colspan="7" class="table-empty">No matching animal records. Adjust the filters or register an animal.</td></tr>';
    const first = result.total ? result.offset + 1 : 0;
    const last = Math.min(result.offset + result.items.length, result.total);
    $('#page-count').textContent = `${first}–${last} of ${result.total}`;
    $('#previous-page').disabled = registryOffset === 0;
    $('#next-page').disabled = registryOffset + result.items.length >= result.total;
    rows.querySelectorAll('.animal-row').forEach(row => {
      const open = () => { location.hash = `animal/${encodeURIComponent(row.dataset.id)}`; };
      row.addEventListener('click', open);
      row.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
    });
  } catch (error) {
    rows.innerHTML = `<tr><td colspan="7" class="table-empty error-text">${esc(error.message)} <button class="text-button" id="retry-registry">Retry</button></td></tr>`;
    $('#retry-registry')?.addEventListener('click', refreshRegistry);
  }
}

async function refreshHistory() {
  const target = $('#history-rows');
  if (!target) return;
  target.innerHTML = '<tr><td colspan="6" class="table-empty">Loading verification history…</td></tr>';
  try {
    const decisionFilter = $('#history-filter').value;
    const allRows = await MuzzleApi.history();
    const rows = decisionFilter === 'all' ? allRows : allRows.filter(item => item.decision === decisionFilter);
    target.innerHTML = rows.length ? rows.map(item => `<tr><td><span class="tag decision-${esc(item.decision.toLowerCase())}">${esc(item.decision)}</span></td><td>${item.animal_id ? `<a href="#animal/${encodeURIComponent(item.animal_id)}">${esc(item.animal_name || 'Animal record')}</a>` : 'No match'}<br><small>${esc(item.ear_tag || '')}</small></td><td><code class="muzzle-id">${esc(item.muzzle_id || '—')}</code></td><td>${item.similarity == null ? '—' : Number(item.similarity).toFixed(4)}</td><td>${esc(item.reason || '—')}</td><td>${esc(new Date(item.created_at).toLocaleString())}</td></tr>`).join('') : `<tr><td colspan="6" class="table-empty">${allRows.length ? 'No verification records match this decision.' : 'No verification events have been recorded yet.'}</td></tr>`;
  } catch (error) {
    target.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)} <button class="text-button" id="retry-history">Retry</button></td></tr>`;
    $('#retry-history')?.addEventListener('click', refreshHistory);
  }
}

async function refreshDashboard() {
  try {
    const [analytics, animals, history] = await Promise.all([MuzzleApi.analytics(), MuzzleApi.animals({ limit: 10 }), MuzzleApi.history()]);
    $('#animal-count').textContent = analytics.total_animals;
    $('#verification-count-today').textContent = analytics.verification_today;
    $('#open-alert-count').textContent = analytics.open_alerts;
    $('#active-insurance-kpi').textContent = analytics.policies.active || 0;
    $('#insurance-coverage-kpi').innerHTML = `Active cover <b>${esc(Number(analytics.total_active_coverage || 0).toLocaleString())}</b><br><small>Currency unspecified</small>`;
    const profiles = analytics.animal_profiles || {};
    const readyProfiles = profiles.prototype_ready || 0;
    const needsAttention = Object.entries(profiles).filter(([state]) => state !== 'prototype_ready').reduce((sum, [, count]) => sum + count, 0);
    $('#animal-profile-summary').innerHTML = `Biometric profiles <b>${readyProfiles} ready</b> · ${needsAttention} need attention`;
    const recentChecks = Object.values(analytics.verification_last_30_days || {}).reduce((sum, count) => sum + count, 0);
    $('#verification-summary').innerHTML = `30-day checks <b>${recentChecks}</b>`;
    const alertBadge = $('#nav-alert-badge');
    alertBadge.textContent = analytics.open_alerts;
    alertBadge.style.display = analytics.open_alerts ? 'inline-flex' : 'none';
    const activities = [
      ...animals.items.map(item => ({ time: item.created_at, label: `Registered ${item.name || 'unnamed animal'}`, detail: item.muzzle_id || item.ear_tag || 'Muzzle ID unavailable', href: `#animal/${encodeURIComponent(item.id)}` })),
      ...history.map(item => ({ time: item.created_at, label: `${item.decision} identity result`, detail: item.muzzle_id || (item.animal_id ? 'Matched animal' : 'No animal match'), href: '#history' })),
    ].sort((a, b) => new Date(b.time) - new Date(a.time)).slice(0, 5);
    const target = $('#recent');
    target.className = activities.length ? 'activity-list' : 'empty-state';
    target.innerHTML = activities.length ? activities.map(item => `<a class="activity-row" href="${item.href}"><span><strong>${esc(item.label)}</strong><small>${esc(item.detail)}</small></span><time>${esc(new Date(item.time).toLocaleString())}</time></a>`).join('') : '<span class="empty-mark">◷</span><strong>No activity yet</strong><p>Registrations and verification events will appear here.</p>';
  } catch (error) {
    $('#animal-count').textContent = '—';
    $('#verification-count-today').textContent = '—';
    $('#open-alert-count').textContent = '—';
    $('#active-insurance-kpi').textContent = '—';
    $('#animal-profile-summary').textContent = 'Metrics unavailable';
    $('#verification-summary').textContent = 'Metrics unavailable';
    $('#recent').innerHTML = `<div class="form-result error">${esc(error.message)}</div>`;
  }
}

async function refreshInsurance() {
  const list = $('#insurance-list');
  const tableRows = $('#insurance-rows');
  tableRows.innerHTML = '<tr><td colspan="7" class="table-empty">Loading policy records…</td></tr>';
  try {
    const [allPolicies, animals] = await Promise.all([MuzzleApi.policies('all'), MuzzleApi.animalOptions()]);
    const requestedStatus = $('#insurance-filter').value;
    const policies = requestedStatus === 'all' ? allPolicies : allPolicies.filter(policy => policy.status === requestedStatus);
    const select = $('#insurance-animal');
    const selected = select.value;
    select.innerHTML = '<option value="">Choose animal</option>' + animals.map(animal => `<option value="${esc(animal.id)}">${esc(animal.label)}</option>`).join('');
    if (selected) select.value = selected;
    tableRows.innerHTML = policies.length ? policies.map(policy => `<tr><td><a href="#animal/${encodeURIComponent(policy.animal_id)}">${esc(policy.animal_name || 'Animal record')}</a><br><small>${esc(policy.ear_tag || '')}</small></td><td><code class="muzzle-id">${esc(policy.muzzle_id || '—')}</code></td><td>${esc(policy.provider)}<br><small>${esc(policy.policy_number)}</small></td><td>${esc(Number(policy.coverage_amount).toLocaleString())}</td><td>${esc(policy.start_date)}–${esc(policy.end_date)}</td><td><select class="policy-status" data-id="${esc(policy.id)}" aria-label="Status for policy ${esc(policy.policy_number)}"><option ${policy.status === 'pending' ? 'selected' : ''}>pending</option><option ${policy.status === 'active' ? 'selected' : ''}>active</option><option ${policy.status === 'expired' ? 'selected' : ''}>expired</option><option ${policy.status === 'cancelled' ? 'selected' : ''}>cancelled</option><option ${policy.status === 'claim_reported' ? 'selected' : ''}>claim_reported</option></select></td><td><button type="button" class="text-button policy-delete" data-id="${esc(policy.id)}">Delete</button></td></tr>`).join('') : '<tr><td colspan="7" class="table-empty">No policy records match this filter.</td></tr>';
    $('#insurance-active-count').textContent = allPolicies.filter(policy => policy.status === 'active').length;
    $('#insurance-total-value').textContent = Number(allPolicies.filter(policy => policy.status === 'active').reduce((sum, policy) => sum + Number(policy.coverage_amount || 0), 0)).toLocaleString();
    $('#insurance-claims-count').textContent = allPolicies.filter(policy => policy.status === 'claim_reported').length;
    $('#insurance-inactive-count').textContent = allPolicies.filter(policy => ['expired', 'cancelled'].includes(policy.status)).length;
    list.querySelectorAll('.policy-status').forEach(control => control.addEventListener('change', async () => { try { await MuzzleApi.updatePolicy(control.dataset.id, { status: control.value }); } catch (error) { alert(error.message); await refreshInsurance(); } }));
    list.querySelectorAll('.policy-delete').forEach(button => button.addEventListener('click', async () => { if (!confirm('Delete this policy record?')) return; try { await MuzzleApi.deletePolicy(button.dataset.id); await refreshInsurance(); } catch (error) { alert(error.message); } }));
  } catch (error) { tableRows.innerHTML = `<tr><td colspan="7" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

async function refreshAlerts() {
  const target = $('#alerts-rows');
  target.innerHTML = '<tr><td colspan="6" class="table-empty">Loading alerts…</td></tr>';
  try {
    const rows = await MuzzleApi.alerts($('#alert-filter').value);
    target.innerHTML = rows.length ? rows.map(alert => `<tr><td><span class="tag severity-${esc(alert.severity)}">${esc(alert.severity)}</span></td><td>${esc(alert.message)}</td><td>${esc(alert.category.replaceAll('_', ' '))}</td><td>${alert.animal_id ? `<a href="#animal/${encodeURIComponent(alert.animal_id)}">${esc(alert.animal_name || 'Animal record')}</a><br><small>${esc(alert.ear_tag || '')}</small><br><code class="muzzle-id">${esc(alert.muzzle_id || '—')}</code>` : '—'}</td><td>${esc(new Date(alert.created_at).toLocaleString())}</td><td>${alert.status === 'open' ? `<button type="button" class="text-button resolve-alert" data-id="${esc(alert.id)}">Resolve</button>` : 'Resolved'}</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">No alerts match this filter.</td></tr>';
    target.querySelectorAll('.resolve-alert').forEach(button => button.addEventListener('click', async () => { try { await MuzzleApi.updateAlert(button.dataset.id, 'resolved'); await refreshAlerts(); } catch (error) { target.insertAdjacentHTML('afterbegin', `<p class="form-result error">${esc(error.message)}</p>`); } }));
  } catch (error) { target.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)}</td></tr>`; }
}

async function refreshAnalytics() {
  const target = $('#analytics-content');
  target.textContent = 'Loading analytics…';
  try {
    const data = await MuzzleApi.analytics();
    const profiles = Object.entries(data.animal_profiles).map(([label, value]) => `<div class="detail-cell"><small>${esc(label.replaceAll('_', ' '))}</small><strong>${value}</strong></div>`).join('');
    const decisions = Object.entries(data.verification_last_30_days).map(([label, value]) => `<div class="detail-cell"><small>${esc(label)}</small><strong>${value}</strong></div>`).join('');
    const registrations = data.registrations_last_30_days.map(row => `<tr><td>${esc(row.date)}</td><td>${row.count}</td></tr>`).join('');
    const makeBars = entries => {
      const max = Math.max(1, ...entries.map(([, value]) => Number(value) || 0));
      return entries.map(([label, value]) => `<div class="chart-row"><span>${esc(label.replaceAll('_', ' '))}</span><span class="chart-track"><i style="width:${Math.max(0, (Number(value) || 0) / max * 100)}%"></i></span><strong>${Number(value) || 0}</strong></div>`).join('') || '<p class="form-note">No events in this period.</p>';
    };
    target.className = 'analytics-grid';
    target.innerHTML = `<div class="detail-grid"><div class="detail-cell"><small>TOTAL REGISTERED ANIMALS</small><strong>${data.total_animals}</strong></div><div class="detail-cell"><small>OPEN OPERATIONAL ALERTS</small><strong>${data.open_alerts}</strong></div><div class="detail-cell"><small>POLICY RECORDS</small><strong>${Object.values(data.policies).reduce((sum, n) => sum + n, 0)}</strong></div></div><div class="analytics-charts"><section class="chart-card"><h3>Biometric profile status</h3><div role="img" aria-label="Counts of cattle by biometric profile status">${makeBars(Object.entries(data.animal_profiles))}</div></section><section class="chart-card"><h3>Identity decisions · last 30 days</h3><div role="img" aria-label="Counts of prototype identity decisions in the last 30 days">${makeBars(Object.entries(data.verification_last_30_days))}</div></section></div><h3>Profile totals</h3><div class="detail-grid">${profiles || '<p>No profiles yet.</p>'}</div><h3>Verification totals · last 30 days</h3><div class="detail-grid">${decisions}</div><h3>Daily registrations · last 30 days</h3><table><thead><tr><th>DATE</th><th>REGISTRATIONS</th></tr></thead><tbody>${registrations || '<tr><td colspan="2">No registrations in this period.</td></tr>'}</tbody></table>`;
  } catch (error) { target.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function refreshSettings() {
  const target = $('#settings-content');
  target.textContent = 'Loading security configuration…';
  try {
    const settings = await MuzzleApi.settings();
    target.className = 'detail-grid settings-grid';
    target.innerHTML = Object.entries({ 'Operator password': settings.operator_password_configured, 'Session signing key': settings.session_secret_configured, 'Encrypted image and embedding storage': settings.data_encryption_configured, 'Secure session cookie': settings.cookie_secure, 'Database': settings.database_type, 'Trained model': settings.model_available, 'FAISS runtime': settings.faiss_available, 'Model version': settings.model_version || 'Unavailable' }).map(([label, value]) => `<div class="detail-cell"><small>${esc(label)}</small><strong>${typeof value === 'boolean' ? value ? 'Configured' : 'Missing' : esc(value)}</strong></div>`).join('') + '<p class="form-note">This single-operator prototype has no user roles or organization isolation. Model matches are not valid evidence for ownership, insurance claims, or other consequential decisions.</p>';
  } catch (error) { target.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function showAnimalDetails(id) {
  const host = $('#animal-detail-content');
  host.innerHTML = '<div class="empty-state compact"><strong>Loading animal record…</strong></div>';
  try {
    const [animal, history, policies] = await Promise.all([
      MuzzleApi.animal(id),
      MuzzleApi.history(),
      MuzzleApi.policies('all'),
    ]);
    const animalHistory = history.filter(item => item.animal_id === id);
    const animalPolicies = policies.filter(item => item.animal_id === id);
    const prototypeReady = animal.status === 'prototype_ready';
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="detail-heading"><div><p class="eyebrow">ANIMAL PROFILE</p><h2>${esc(animal.name || 'Unnamed animal')}</h2><p class="subhead">Record ${esc(animal.id)}</p></div><span class="tag">${esc(animal.status.replaceAll('_', ' '))}</span></div><div class="detail-grid"><div><small>EAR TAG</small><strong>${esc(animal.ear_tag || 'Not available')}</strong></div><div><small>BREED</small><strong>${esc(animal.breed || 'Not available')}</strong></div><div><small>SEX</small><strong>${esc(animal.sex || 'Not available')}</strong></div><div><small>DATE OF BIRTH</small><strong>${esc(animal.date_of_birth || 'Not available')}</strong></div><div><small>REFERENCE IMAGES</small><strong>${animal.images.length} saved</strong></div><div><small>REGISTERED</small><strong>${esc(new Date(animal.created_at).toLocaleString())}</strong></div></div><div class="detail-images"><h3>Stored reference images</h3>${animal.images.length ? `<div class="stored-image-grid">${animal.images.map((image, index) => { const flags = image.quality?.warnings || []; return `<figure><img loading="lazy" src="/api/animals/${encodeURIComponent(animal.id)}/images/${encodeURIComponent(image.id)}" alt="Stored reference ${index + 1} for ${esc(animal.name || 'animal')}"><figcaption>Reference ${index + 1} · ${image.width} × ${image.height} · ${esc(image.format)}${flags.length ? ` · Capture flags: ${esc(flags.join(', '))}` : ''}</figcaption></figure>`; }).join('')}</div>` : '<p>No stored reference images.</p>'}<p class="form-note">Capture flags are simple image heuristics; they do not confirm muzzle visibility or image identity.</p></div><section class="panel"><h3>Verification history</h3>${animalHistory.length ? `<div class="table-wrap"><table><thead><tr><th>OUTCOME</th><th>TIME</th><th>SIMILARITY</th><th>REVIEW NOTE</th></tr></thead><tbody>${animalHistory.map(item => `<tr><td><span class="tag decision-${esc(item.decision.toLowerCase())}">${esc(item.decision)}</span></td><td>${esc(new Date(item.created_at).toLocaleString())}</td><td>${item.similarity == null ? '—' : Number(item.similarity).toFixed(4)}</td><td>${esc(item.reason || '—')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="form-note">No linked verification events are available.</p>'}</section><section class="panel"><h3>Insurance records</h3>${animalPolicies.length ? `<div class="table-wrap"><table><thead><tr><th>PROVIDER</th><th>POLICY</th><th>COVERAGE</th><th>DATES</th><th>STATUS</th></tr></thead><tbody>${animalPolicies.map(policy => `<tr><td>${esc(policy.provider)}</td><td>${esc(policy.policy_number)}</td><td>${esc(Number(policy.coverage_amount).toLocaleString())}</td><td>${esc(policy.start_date)}–${esc(policy.end_date)}</td><td>${esc(policy.status.replaceAll('_', ' '))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="form-note">No operator-entered policy records are linked to this animal.</p>'}</section><form id="animal-edit-form" class="panel"><h3>Edit animal details</h3><div class="form-row"><label>Name<input name="name" maxlength="120" value="${esc(animal.name)}"></label><label>Ear tag<input name="ear_tag" maxlength="120" value="${esc(animal.ear_tag)}"></label></div><div class="form-row"><label>Breed<input name="breed" maxlength="120" value="${esc(animal.breed)}"></label><label>Sex<select name="sex"><option value="">Select</option><option ${animal.sex === 'Female' ? 'selected' : ''}>Female</option><option ${animal.sex === 'Male' ? 'selected' : ''}>Male</option></select></label></div><label>Date of birth<input name="date_of_birth" type="date" value="${esc(animal.date_of_birth)}"></label><button class="button primary" type="submit">Save changes</button><button class="button secondary" id="delete-animal" type="button">Delete animal record</button><p class="form-result" id="animal-edit-result" role="status"></p></form><div class="status-banner detail-notice"><span class="status-icon">i</span><div><strong>Biometric profile: ${esc(animal.status.replaceAll('_', ' '))}</strong><p>${prototypeReady ? 'Prototype full-image embeddings are available. Muzzle detection and population-level validation are missing; require in-person confirmation.' : 'Biometric embeddings are pending, stale, or unavailable; reindex from the stored images when model artifacts are ready.'}</p>${['pending_model', 'stale_model', 'reindex_required'].includes(animal.status) && animal.images.length ? '<button class="button secondary" id="reindex-animal" type="button">Rebuild biometric profile</button>' : ''}</div></div>`;
    host.querySelector('.detail-heading .subhead').insertAdjacentHTML('afterend', `<p class="public-muzzle-id">Muzzle ID <code>${esc(animal.muzzle_id || 'Unavailable')}</code></p>`);
    $('#reindex-animal')?.addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; button.textContent = 'Rebuilding…'; try { await MuzzleApi.reindexAnimal(animal.id); await showAnimalDetails(animal.id); } catch (error) { button.disabled = false; button.textContent = 'Rebuild biometric profile'; $('#animal-edit-result').textContent = error.message; } });
    const editForm = $('#animal-edit-form');
    editForm.addEventListener('submit', async event => { event.preventDefault(); const result = $('#animal-edit-result'); try { const form = new FormData(editForm); const value = Object.fromEntries(form); value.date_of_birth = value.date_of_birth || null; await MuzzleApi.updateAnimal(animal.id, value); await showAnimalDetails(animal.id); $('#animal-edit-result').className = 'form-result success'; $('#animal-edit-result').textContent = 'Animal details saved.'; } catch (error) { result.className = 'form-result error'; result.textContent = error.message; } });
    $('#delete-animal').addEventListener('click', async () => { if (!confirm('Permanently delete this animal, its encrypted reference images, embeddings, and policy records? Verification history remains with the animal link removed.')) return; try { await MuzzleApi.deleteAnimal(animal.id); location.hash = '#animals'; await refreshRegistry(); } catch (error) { $('#animal-edit-result').className = 'form-result error'; $('#animal-edit-result').textContent = error.message; } });
  } catch (error) {
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="empty-state compact error-text">${esc(error.message)}</div>`;
  }
}

function syncSidebarToggle() {
  const button = $('#sidebar-toggle');
  const mobile = window.matchMedia('(max-width: 768px)').matches;
  const expanded = mobile ? document.body.classList.contains('mobile-nav-open') : !document.body.classList.contains('sidebar-collapsed');
  button.setAttribute('aria-expanded', String(expanded));
  const label = mobile ? (expanded ? 'Close navigation' : 'Open navigation') : (expanded ? 'Collapse navigation' : 'Expand navigation');
  button.setAttribute('aria-label', label);
  button.title = label;
}

function route() {
  if (document.body.classList.contains('role-farmer')) { farmerRoute(); return; }
  if (!document.body.classList.contains('role-admin')) return;
  let path = location.hash.slice(1) || 'overview';
  try { path = decodeURIComponent(path).replace(/^\//, ''); }
  catch { path = ''; }
  const [routeName, id] = path.split('/');
  const page = routeName === 'animal' && id ? 'details' : (PAGE_LABELS[routeName] ? routeName : 'not-found');
  const visiblePage = page;
  document.querySelectorAll('[data-view]').forEach(element => {
    const visibleViews = (element.dataset.view || '').split(/\s+/);
    element.classList.toggle('route-hidden', !visibleViews.includes(visiblePage));
  });
  const navKey = page === 'details' ? 'animals' : page;
  document.querySelectorAll('.nav-item').forEach(link => {
    const active = link.getAttribute('href') === `#${navKey}`;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  $('#page-label').textContent = page === 'details' ? 'Animal details' : (PAGE_LABELS[page] || 'Page not found');
  document.title = `${page === 'details' ? 'Animal details' : PAGE_LABELS[page] || 'Page not found'} · MuzzleID`;
  document.body.classList.remove('mobile-nav-open');
  syncSidebarToggle();
  if (page === 'animals') refreshRegistry();
  if (page === 'overview') refreshDashboard();
  if (page === 'history') refreshHistory();
  if (page === 'details' && id) showAnimalDetails(id);
  if (page === 'system') refreshHealth();
  if (page === 'insurance') refreshInsurance();
  if (page === 'alerts') refreshAlerts();
  if (page === 'analytics') refreshAnalytics();
  if (page === 'settings') refreshSettings();
  if (page === 'farmer-requests') refreshAdminFarmerRequests();
}

const registerForm = $('#register-form');
const uploadInput = $('#register-files');
const previewArea = $('#image-previews');
let idempotencyKey = crypto.randomUUID();
let submitInProgress = false;
let previewUrls = [];

$('#insurance-form').addEventListener('submit', async event => {
  event.preventDefault();
  const result = $('#insurance-result');
  try {
    const value = Object.fromEntries(new FormData(event.currentTarget));
    value.coverage_amount = Number(value.coverage_amount);
    await MuzzleApi.createPolicy(value);
    event.currentTarget.reset();
    result.className = 'form-result success'; result.textContent = 'Policy record saved.';
    await refreshInsurance();
  } catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
});
$('#alert-filter').addEventListener('change', refreshAlerts);

function setFiles(files) {
  const transfer = new DataTransfer();
  for (const file of files) transfer.items.add(file);
  uploadInput.files = transfer.files;
  renderPreviews();
}

function renderPreviews() {
  previewUrls.forEach(url => URL.revokeObjectURL(url));
  previewUrls = [];
  const files = [...uploadInput.files];
  previewArea.innerHTML = files.map((file, index) => `<div class="image-preview"><img alt="Preview of ${esc(file.name)}"><div><strong>${esc(file.name)}</strong><small>${(file.size / 1024 / 1024).toFixed(2)} MB</small></div><button type="button" class="remove-image" data-index="${index}" aria-label="Remove ${esc(file.name)}">×</button></div>`).join('');
  previewArea.querySelectorAll('img').forEach((image, index) => { const url = URL.createObjectURL(files[index]); previewUrls.push(url); image.src = url; });
  previewArea.querySelectorAll('.remove-image').forEach(button => button.addEventListener('click', () => setFiles(files.filter((_, index) => index !== Number(button.dataset.index)))));
}

uploadInput.addEventListener('change', renderPreviews);
const dropzone = $('#register-dropzone');
dropzone.addEventListener('dragover', event => { event.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', event => { event.preventDefault(); dropzone.classList.remove('dragover'); const dropped = [...event.dataTransfer.files]; if (dropped.length) setFiles(dropped); });

registerForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (submitInProgress) return;
  const result = $('#register-result');
  const files = [...uploadInput.files];
  if (!files.length || files.length > 8) { result.className = 'form-result error'; result.textContent = 'Choose between 1 and 8 reference images.'; return; }
  if (files.some(file => file.size > 12 * 1024 * 1024)) { result.className = 'form-result error'; result.textContent = 'Each image must be 12 MB or smaller.'; return; }
  if (files.some(file => !['image/jpeg', 'image/png', 'image/webp'].includes(file.type))) { result.className = 'form-result error'; result.textContent = 'Use JPEG, PNG or WebP images.'; return; }
  submitInProgress = true;
  const submit = registerForm.querySelector('[type=submit]');
  submit.disabled = true; submit.dataset.label = submit.textContent; submit.textContent = 'Saving record…';
  result.className = 'form-result'; result.textContent = 'Validating and saving animal record…';
  try {
    const response = await MuzzleApi.register(new FormData(registerForm), idempotencyKey);
    if (!response.persisted) throw new Error('The server did not confirm that the record was persisted.');
    result.className = 'form-result success';
    result.textContent = response.message;
    const confirmation = $('#register-confirmation');
    confirmation.style.display = 'block';
    confirmation.innerHTML = `<div class="registration-success"><span class="success-mark" aria-hidden="true">✓</span><div><p class="eyebrow">RECORD SAVED</p><h3>${esc(response.name || 'Animal registration complete')}</h3><p>Persistent record and reference image${response.image_count === 1 ? '' : 's'} are saved. Biometric profile: <strong>${esc(response.status.replaceAll('_', ' '))}</strong>.</p><div class="muzzle-id-copy"><span><small>PUBLIC MUZZLE ID · PSEUDONYMOUS</small><code>${esc(response.muzzle_id || 'Unavailable')}</code></span><button type="button" class="button secondary small" id="copy-muzzle-id" ${response.muzzle_id ? '' : 'disabled'}>Copy ID</button></div><a class="button secondary small" href="#animal/${encodeURIComponent(response.id)}">Open animal record</a></div></div>`;
    $('#copy-muzzle-id')?.addEventListener('click', async buttonEvent => {
      try { await navigator.clipboard.writeText(response.muzzle_id); buttonEvent.currentTarget.textContent = 'Copied'; }
      catch { buttonEvent.currentTarget.textContent = 'Copy unavailable'; }
    });
    registerForm.reset(); renderPreviews(); idempotencyKey = crypto.randomUUID();
    registryOffset = 0; await refreshRegistry();
  } catch (error) {
    result.className = 'form-result error'; result.textContent = error.message;
    // Keep both metadata and files so the operator can retry with the same idempotency key.
  } finally {
    submitInProgress = false; submit.disabled = false; submit.textContent = 'Save animal record →';
  }
});

$('#verify-form').addEventListener('submit', async event => {
  event.preventDefault(); const result = $('#verify-result'); const submit = event.currentTarget.querySelector('[type=submit]');
  submit.disabled = true; result.className = 'form-result'; result.textContent = 'Checking image and pipeline availability…';
  try {
    const response = await MuzzleApi.verify(new FormData(event.currentTarget));
    const matched = response.matched_animal;
    result.className = `form-result verification-result decision-${String(response.decision).toLowerCase()}`;
    result.innerHTML = `<div class="verification-outcome"><span class="tag decision-${String(response.decision).toLowerCase()}">${esc(response.decision)}</span><div><strong>${matched ? esc(matched.name || 'Matched animal') : 'No enrolled match'}</strong>${matched ? `<p>Ear tag ${esc(matched.ear_tag || 'not provided')} · Muzzle ID <code>${esc(matched.muzzle_id || 'Unavailable')}</code></p>` : ''}${response.similarity == null ? '' : `<p>Similarity score: <strong>${Number(response.similarity).toFixed(4)}</strong></p>`}<small>${esc(response.message || 'Prototype result only. Confirm identity in person; do not use for ownership or insurance decisions.')}</small></div></div>`;
  } catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { submit.disabled = false; }
});

const verifyInput = $('#verify-file');
const verifyDropzone = $('#verify-dropzone');
const verifyPreviewWrap = $('#verify-preview-wrap');
const verifyPreviewImage = $('#verify-preview-img');
let verifyPreviewUrl = null;
function previewVerificationFile(file) {
  if (verifyPreviewUrl) URL.revokeObjectURL(verifyPreviewUrl);
  verifyPreviewUrl = null;
  $('#verify-result').textContent = '';
  $('#verify-result').className = 'form-result';
  if (!file) { verifyPreviewWrap.hidden = true; verifyPreviewWrap.style.display = 'none'; return; }
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 12 * 1024 * 1024) {
    verifyInput.value = '';
    verifyPreviewWrap.hidden = true; verifyPreviewWrap.style.display = 'none';
    $('#verify-result').className = 'form-result error';
    $('#verify-result').textContent = file.size > 12 * 1024 * 1024 ? 'Choose an image that is 12 MB or smaller.' : 'Use a JPEG, PNG or WebP image.';
    return;
  }
  verifyPreviewUrl = URL.createObjectURL(file);
  verifyPreviewImage.src = verifyPreviewUrl;
  verifyPreviewImage.alt = `Preview of ${file.name}`;
  verifyPreviewWrap.hidden = false; verifyPreviewWrap.style.display = 'block';
}
verifyInput.addEventListener('change', () => previewVerificationFile(verifyInput.files[0]));
verifyDropzone.addEventListener('dragover', event => { event.preventDefault(); verifyDropzone.classList.add('dragover'); });
verifyDropzone.addEventListener('dragleave', () => verifyDropzone.classList.remove('dragover'));
verifyDropzone.addEventListener('drop', event => {
  event.preventDefault(); verifyDropzone.classList.remove('dragover');
  const file = [...event.dataTransfer.files].find(item => item.type.startsWith('image/'));
  if (!file) return;
  const transfer = new DataTransfer(); transfer.items.add(file); verifyInput.files = transfer.files;
  previewVerificationFile(file);
});

let filterTimer;
function setRegistryFilter(value) {
  registryQuery = value.trim(); registryOffset = 0;
  $('#animal-filter').value = value;
  refreshRegistry();
}
$('#global-search').addEventListener('input', event => {
  clearTimeout(filterTimer); const value = event.target.value;
  if (location.hash !== '#animals') location.hash = 'animals';
  filterTimer = setTimeout(() => setRegistryFilter(value), 250);
});
$('#animal-filter').addEventListener('input', event => { clearTimeout(filterTimer); filterTimer = setTimeout(() => setRegistryFilter(event.target.value), 250); });
$('#animal-sort').addEventListener('change', event => { registrySort = event.target.value; registryOffset = 0; refreshRegistry(); });
$('#animal-status-filter').addEventListener('change', event => { registryStatus = event.target.value; registryOffset = 0; refreshRegistry(); });
$('#previous-page').addEventListener('click', () => { registryOffset = Math.max(0, registryOffset - pageSize); refreshRegistry(); });
$('#next-page').addEventListener('click', () => { registryOffset += pageSize; refreshRegistry(); });
$('#history-filter').addEventListener('change', refreshHistory);
$('#insurance-filter').addEventListener('change', refreshInsurance);
$('#refresh-analytics-btn').addEventListener('click', refreshAnalytics);
$('#refresh-system-btn').addEventListener('click', refreshHealth);
$('#sidebar-toggle').addEventListener('click', () => {
  const mobile = window.matchMedia('(max-width: 768px)').matches;
  if (mobile) document.body.classList.toggle('mobile-nav-open');
  else document.body.classList.toggle('sidebar-collapsed');
  syncSidebarToggle();
});
$('#sidebar-close').addEventListener('click', () => {
  document.body.classList.remove('mobile-nav-open');
  syncSidebarToggle();
});
window.matchMedia('(max-width: 768px)').addEventListener('change', event => {
  if (!event.matches) document.body.classList.remove('mobile-nav-open');
  syncSidebarToggle();
});
syncSidebarToggle();
window.addEventListener('hashchange', route);
window.addEventListener('muzzleid:session-expired', () => {
  const farmerSession = document.body.classList.contains('role-farmer');
  document.body.classList.remove('authenticated', 'role-admin', 'role-farmer');
  $('#farmer-shell').hidden = true; farmerAccount = null; clearFarmerViews();
  showAuthScreen(farmerSession ? 'farmer-login-form' : 'login-form');
  const result = $(farmerSession ? '#farmer-login-result' : '#login-result');
  result.textContent = 'Your session expired. Sign in again.'; result.className = 'form-result error';
});

const authGate = $('#auth-gate');
$('#choose-admin').addEventListener('click', () => showAuthScreen('login-form'));
$('#choose-farmer').addEventListener('click', () => showAuthScreen('farmer-login-form'));
$('#back-role-from-admin').addEventListener('click', () => showAuthScreen('role-selection'));
$('#back-role-from-farmer-login').addEventListener('click', () => showAuthScreen('role-selection'));
$('#back-role-from-farmer-signup').addEventListener('click', () => showAuthScreen('role-selection'));
$('#show-farmer-signup').addEventListener('click', () => showAuthScreen('farmer-signup-form'));
$('#show-farmer-login').addEventListener('click', () => showAuthScreen('farmer-login-form'));
document.querySelectorAll('[data-toggle-password]').forEach(button => button.addEventListener('click', () => {
  const input = document.getElementById(button.dataset.togglePassword);
  const reveal = input.type === 'password'; input.type = reveal ? 'text' : 'password';
  button.textContent = reveal ? 'Hide' : 'Show'; button.setAttribute('aria-label', `${reveal ? 'Hide' : 'Show'} password`);
}));

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const result = $('#login-result');
  const button = event.currentTarget.querySelector('[type=submit]');
  button.disabled = true; result.textContent = 'Signing in…'; result.className = 'form-result';
  try {
    await MuzzleApi.login($('#operator-password').value);
    $('#operator-password').value = ''; result.textContent = '';
    activateRole('admin'); await refreshHealth(); route();
  } catch (error) { result.textContent = error.message; result.className = 'form-result error'; }
  finally { button.disabled = false; }
});

$('#farmer-login-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type=submit]'); const result = $('#farmer-login-result');
  button.disabled = true; button.textContent = 'Signing in…'; result.className = 'form-result'; result.textContent = '';
  try { const data = await MuzzleApi.farmerLogin({ identifier: form.elements.identifier.value, password: form.elements.password.value }); form.reset(); activateRole('farmer', data.farmer); }
  catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Sign in'; }
});

$('#farmer-signup-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const result = $('#farmer-signup-result'); const button = form.querySelector('[type=submit]');
  if (form.elements.password.value !== form.elements.password_confirm.value) { result.className = 'form-result error'; result.textContent = 'The passwords do not match.'; form.elements.password_confirm.focus(); return; }
  button.disabled = true; button.textContent = 'Creating account…'; result.className = 'form-result'; result.textContent = 'Creating your account…';
  try { const data = await MuzzleApi.farmerSignup({ full_name: form.elements.full_name.value, mobile: form.elements.mobile.value, email: form.elements.email.value || null, password: form.elements.password.value }); form.reset(); activateRole('farmer', data.farmer); }
  catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Create account'; }
});

let farmerSearchTimer;
$('#farmer-animal-search').addEventListener('input', () => { clearTimeout(farmerSearchTimer); farmerSearchTimer = setTimeout(refreshFarmerAnimals, 250); });
$('#refresh-farmer-requests').addEventListener('click', refreshFarmerRequests);
$('#refresh-admin-farmer-requests').addEventListener('click', refreshAdminFarmerRequests);

const farmerRegisterInput = $('#farmer-register-files');
const farmerPreviewArea = $('#farmer-image-previews');
let farmerPreviewUrls = [];
function setFarmerFiles(files) { const transfer = new DataTransfer(); for (const file of files) transfer.items.add(file); farmerRegisterInput.files = transfer.files; renderFarmerPreviews(); }
function renderFarmerPreviews() {
  farmerPreviewUrls.forEach(url => URL.revokeObjectURL(url)); farmerPreviewUrls = [];
  const files = [...farmerRegisterInput.files];
  farmerPreviewArea.innerHTML = files.map((file, index) => `<div class="image-preview"><img alt="Preview of ${esc(file.name)}"><div><strong>${esc(file.name)}</strong><small>${(file.size / 1024 / 1024).toFixed(2)} MB</small></div><button type="button" class="remove-image" data-index="${index}" aria-label="Remove ${esc(file.name)}">×</button></div>`).join('');
  farmerPreviewArea.querySelectorAll('img').forEach((image, index) => { const url = URL.createObjectURL(files[index]); farmerPreviewUrls.push(url); image.src = url; });
  farmerPreviewArea.querySelectorAll('.remove-image').forEach(button => button.addEventListener('click', () => setFarmerFiles(files.filter((_, index) => index !== Number(button.dataset.index)))));
}
farmerRegisterInput.addEventListener('change', renderFarmerPreviews);
const farmerRegisterDrop = $('#farmer-register-dropzone');
farmerRegisterDrop.addEventListener('dragover', event => { event.preventDefault(); farmerRegisterDrop.classList.add('dragover'); });
farmerRegisterDrop.addEventListener('dragleave', () => farmerRegisterDrop.classList.remove('dragover'));
farmerRegisterDrop.addEventListener('drop', event => { event.preventDefault(); farmerRegisterDrop.classList.remove('dragover'); if (event.dataTransfer.files.length) setFarmerFiles([...event.dataTransfer.files]); });
$('#farmer-register-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const result = $('#farmer-register-result'); const button = form.querySelector('[type=submit]'); const files = [...farmerRegisterInput.files];
  if (!files.length || files.length > 8) { result.className = 'form-result error'; result.textContent = 'Choose between 1 and 8 images.'; return; }
  if (files.some(file => file.size > 12 * 1024 * 1024 || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type))) { result.className = 'form-result error'; result.textContent = 'Use JPEG, PNG, or WebP images up to 12 MB each.'; return; }
  button.disabled = true; button.textContent = 'Saving cattle record…'; result.className = 'form-result'; result.textContent = 'Validating and securely saving your record…';
  try {
    const data = await MuzzleApi.registerFarmerAnimal(new FormData(form), farmerRegisterKey);
    if (!data.persisted) throw new Error('The server did not confirm that the animal was saved.');
    $('#farmer-register-confirmation').innerHTML = `<div class="registration-success"><span class="success-mark" aria-hidden="true">✓</span><div><p class="eyebrow">RECORD SAVED</p><h3>${esc(data.name || 'Animal registered')}</h3><p>Biometric profile: ${esc(data.status.replaceAll('_', ' '))}. A prototype profile is not proof of ownership.</p><div class="muzzle-id-copy"><span><small>PERSISTENT MUZZLE ID</small><code>${esc(data.muzzle_id || 'Unavailable')}</code></span><button type="button" class="button secondary small" id="farmer-copy-muzzle-id" ${data.muzzle_id ? '' : 'disabled'}>Copy ID</button></div><a class="button secondary small" href="#farmer/animals/${encodeURIComponent(data.id)}">View my cattle record</a></div></div>`;
    $('#farmer-copy-muzzle-id')?.addEventListener('click', async e => { try { await navigator.clipboard.writeText(data.muzzle_id); e.currentTarget.textContent = 'Copied'; } catch { e.currentTarget.textContent = 'Copy unavailable'; } });
    result.className = 'form-result success'; result.textContent = data.message; form.reset(); renderFarmerPreviews(); farmerRegisterKey = crypto.randomUUID();
  } catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Save my animal'; }
});

const farmerVerifyInput = $('#farmer-verify-file'); let farmerVerifyUrl = null;
function previewFarmerVerification(file) {
  if (farmerVerifyUrl) URL.revokeObjectURL(farmerVerifyUrl); farmerVerifyUrl = null; const wrap = $('#farmer-verify-preview-wrap');
  if (!file) { wrap.hidden = true; return; }
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 12 * 1024 * 1024) { farmerVerifyInput.value = ''; wrap.hidden = true; $('#farmer-verify-result').className = 'form-result error'; $('#farmer-verify-result').textContent = file.size > 12 * 1024 * 1024 ? 'Choose an image up to 12 MB.' : 'Use JPEG, PNG, or WebP.'; return; }
  farmerVerifyUrl = URL.createObjectURL(file); $('#farmer-verify-preview').src = farmerVerifyUrl; wrap.hidden = false;
}
farmerVerifyInput.addEventListener('change', () => previewFarmerVerification(farmerVerifyInput.files[0]));
const farmerVerifyDrop = $('#farmer-verify-dropzone');
farmerVerifyDrop.addEventListener('dragover', event => { event.preventDefault(); farmerVerifyDrop.classList.add('dragover'); });
farmerVerifyDrop.addEventListener('dragleave', () => farmerVerifyDrop.classList.remove('dragover'));
farmerVerifyDrop.addEventListener('drop', event => { event.preventDefault(); farmerVerifyDrop.classList.remove('dragover'); const file = [...event.dataTransfer.files].find(item => item.type.startsWith('image/')); if (file) { const transfer = new DataTransfer(); transfer.items.add(file); farmerVerifyInput.files = transfer.files; previewFarmerVerification(file); } });
$('#farmer-verify-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('[type=submit]'); const result = $('#farmer-verify-result'); button.disabled = true; button.textContent = 'Checking…'; result.className = 'form-result'; result.textContent = 'Checking against your enrolled cattle profiles…';
  try {
    const data = await MuzzleApi.verifyFarmerAnimal(new FormData(event.currentTarget)); const animal = data.matched_animal;
    result.className = `form-result verification-result decision-${data.decision.toLowerCase()}`;
    result.innerHTML = `<div class="verification-outcome"><span class="tag decision-${data.decision.toLowerCase()}">${esc(data.decision)}</span><div><strong>${animal ? esc(animal.name || 'Your matched animal') : data.decision === 'UNKNOWN' ? 'No enrolled match' : 'Manual review needed'}</strong>${animal ? `<p>Ear tag ${esc(animal.ear_tag || 'not provided')} · Muzzle ID <code>${esc(animal.muzzle_id || 'Unavailable')}</code></p><a href="#farmer/animals/${encodeURIComponent(animal.id)}">Open animal record →</a>` : ''}<small>${esc(data.message)}</small></div></div>`;
    renderFarmerHistory(await MuzzleApi.farmerHistory(), $('#farmer-history'));
  } catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Check identity'; }
});

$('#farmer-request-kind').addEventListener('change', event => { $('#farmer-request-animal').required = event.target.value === 'claim'; });
$('#farmer-request-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const result = $('#farmer-request-result'); const button = form.querySelector('[type=submit]'); button.disabled = true; button.textContent = 'Submitting…';
  try { await MuzzleApi.createFarmerRequest({ kind: form.elements.kind.value, animal_id: form.elements.animal_id.value || null, description: form.elements.description.value }); form.reset(); $('#farmer-request-animal').required = false; result.className = 'form-result success'; result.textContent = 'Your request was recorded for operator review. Submission is not an insurance claim decision.'; await refreshFarmerRequests(); }
  catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Submit request'; }
});

$('#farmer-profile-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const result = $('#farmer-profile-result'); const button = form.querySelector('[type=submit]'); button.disabled = true;
  try { await MuzzleApi.updateFarmerProfile({ full_name: form.elements.full_name.value, mobile: form.elements.mobile.value, email: form.elements.email.value || null }); result.className = 'form-result success'; result.textContent = 'Profile updated.'; farmerAccount.full_name = form.elements.full_name.value; $('#farmer-greeting').textContent = farmerAccount.full_name; $('#farmer-welcome-name').textContent = `Welcome, ${farmerAccount.full_name}`; }
  catch (error) { result.className = 'form-result error'; result.textContent = error.message; } finally { button.disabled = false; }
});
$('#farmer-password-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const result = $('#farmer-password-result'); const button = form.querySelector('[type=submit]'); button.disabled = true;
  try { await MuzzleApi.changeFarmerPassword({ current_password: form.elements.current_password.value, new_password: form.elements.new_password.value }); form.reset(); result.className = 'form-result success'; result.textContent = 'Password changed successfully.'; }
  catch (error) { result.className = 'form-result error'; result.textContent = error.message; } finally { button.disabled = false; }
});

async function signOut() {
  try { await MuzzleApi.logout(); } catch { /* Clear the local view even when the API is unavailable. */ }
  document.body.classList.remove('authenticated', 'role-admin', 'role-farmer');
  $('#farmer-shell').hidden = true; farmerAccount = null; clearFarmerViews();
  document.querySelectorAll('[data-view]').forEach(view => view.classList.add('route-hidden'));
  showAuthScreen('role-selection'); $('#operator-password').value = '';
  history.replaceState(null, '', location.pathname + location.search);
}
$('#logout-button').addEventListener('click', signOut);
$('#farmer-logout').addEventListener('click', signOut);
(async () => {
  try {
    const session = await MuzzleApi.session();
    if (session.authenticated) {
      if (session.role === 'farmer') activateRole('farmer', session.farmer);
      else { activateRole('admin'); await refreshHealth(); route(); }
    } else {
      showAuthScreen('role-selection');
      if (!session.configured) $('#login-result').textContent = 'Admin sign-in needs the operator password and session secret configured in .env. Farmer accounts may still be available.';
    }
  } catch { showAuthScreen('role-selection'); $('#login-result').textContent = 'MuzzleID API is unavailable.'; }
})();
