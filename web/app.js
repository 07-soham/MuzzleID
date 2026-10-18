const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const PAGE_LABELS = { landing: 'Welcome', overview: 'Dashboard', animals: 'Animal database', register: 'Register animal', verify: 'Identify animal', history: 'Verification history', insurance: 'Insurance', alerts: 'Alerts and review', analytics: 'Analytics', reports: 'Reports', system: 'System status', settings: 'Settings' };
let registryOffset = 0;
let registryQuery = '';
let registrySort = 'created_at';
const pageSize = 25;

async function refreshHealth() {
  try {
    const health = await MuzzleApi.health();
    $('#system-status').textContent = health.database_available ? 'Online' : 'Degraded';
    $('#engine-status').textContent = health.verification_available ? 'Prototype matching active' : 'Biometric engine unavailable';
    $('#status-title').textContent = !health.database_available ? 'Database connection is unavailable' : health.verification_available ? 'Prototype biometric matching is available' : 'Biometric engine is not configured';
    $('#status-description').textContent = !health.database_available
      ? 'The API could not confirm its database connection. Registry and registration actions may fail until the database is available.'
      : health.verification_available
      ? 'Prototype full-image embeddings are enabled. No muzzle detector or unseen-animal validation is available; every match requires human confirmation.'
      : 'Animal records and image uploads are available. Matching requires a trained checkpoint and its matching validation calibration.';
    $('#status-tag').textContent = !health.database_available ? 'DATABASE ISSUE' : health.verification_available ? 'PROTOTYPE' : 'SETUP REQUIRED';
    const lock = document.querySelector('#verify-form .locked');
    if (lock) {
      lock.hidden = health.verification_available;
      lock.querySelector('strong').textContent = health.verification_available ? 'Prototype mode only' : 'Verification currently unavailable';
      lock.querySelector('small').textContent = health.verification_available ? 'No muzzle detector; matches are prototype suggestions and require in-person confirmation.' : !health.model_available ? 'Training and matching calibration artifacts are not installed.' : 'FAISS gallery indexing is unavailable.';
    }
  } catch (error) {
    $('#system-status').textContent = 'Offline';
    $('#engine-status').textContent = 'API unavailable';
    $('#status-title').textContent = 'MuzzleID API is unavailable';
    $('#status-description').textContent = error.message;
    $('#status-tag').textContent = 'OFFLINE';
  }
}

async function refreshRegistry() {
  const rows = $('#animal-rows');
  if (!rows) return;
  rows.innerHTML = '<tr><td colspan="6" class="table-empty">Loading animal records…</td></tr>';
  try {
    const result = await MuzzleApi.animals({ q: registryQuery, offset: registryOffset, limit: pageSize, sort: registrySort });
    $('#animal-count').textContent = result.total;
    $('#registry-summary').textContent = `${result.total} record${result.total === 1 ? '' : 's'} in this organization`;
    rows.innerHTML = result.items.length ? result.items.map(animal => `<tr class="animal-row" data-id="${esc(animal.id)}" tabindex="0"><td><strong>${esc(animal.name || 'Unnamed animal')}</strong><br><small>${esc(animal.id.slice(0, 8))}</small></td><td>${esc(animal.ear_tag || '—')}</td><td>${esc(animal.breed || '—')}</td><td>${animal.image_count}</td><td><span class="tag ${animal.status === 'active' ? 'tag-active' : ''}">${esc(animal.status.replaceAll('_', ' '))}</span></td><td aria-hidden="true">›</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">No matching animal records. Register an animal to get started.</td></tr>';
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
    rows.innerHTML = `<tr><td colspan="6" class="table-empty error-text">${esc(error.message)} <button class="text-button" id="retry-registry">Retry</button></td></tr>`;
    $('#retry-registry')?.addEventListener('click', refreshRegistry);
  }
}

async function refreshHistory() {
  const target = $('#history-list');
  if (!target) return;
  target.innerHTML = '<div class="empty-state compact"><strong>Loading verification history…</strong></div>';
  try {
    const rows = await MuzzleApi.history();
    target.innerHTML = rows.length ? `<div class="history-list">${rows.map(item => `<div class="history-row"><span class="history-decision">${esc(item.decision)}</span><span>${esc(item.animal_id || 'No animal match')}</span><span>${esc(item.reason || '')}</span><time>${esc(new Date(item.created_at).toLocaleString())}</time></div>`).join('')}</div>` : '<div class="empty-state compact"><span class="empty-mark">◷</span><strong>No verification history</strong><p>Verification events will appear here when the biometric engine is active.</p></div>';
  } catch (error) {
    target.innerHTML = `<div class="form-result error">${esc(error.message)} <button class="text-button" id="retry-history">Retry</button></div>`;
    $('#retry-history')?.addEventListener('click', refreshHistory);
  }
}

async function refreshDashboard() {
  try {
    const [analytics, animals, history] = await Promise.all([MuzzleApi.analytics(), MuzzleApi.animals({ limit: 10 }), MuzzleApi.history()]);
    $('#animal-count').textContent = analytics.total_animals;
    $('#verification-count-today').textContent = analytics.verification_today;
    $('#open-alert-count').textContent = analytics.open_alerts;
    const activities = [
      ...animals.items.map(item => ({ time: item.created_at, label: `Registered ${item.name || 'unnamed animal'}`, detail: item.ear_tag || item.id.slice(0, 8), href: `#animal/${encodeURIComponent(item.id)}` })),
      ...history.map(item => ({ time: item.created_at, label: `${item.decision} identity result`, detail: item.animal_id || 'No animal match', href: '#history' })),
    ].sort((a,b) => new Date(b.time) - new Date(a.time)).slice(0, 5);
    const target = $('#recent');
    target.className = activities.length ? 'activity-list' : 'empty-state';
    target.innerHTML = activities.length ? activities.map(item => `<a class="activity-row" href="${item.href}"><span><strong>${esc(item.label)}</strong><small>${esc(item.detail)}</small></span><time>${esc(new Date(item.time).toLocaleString())}</time></a>`).join('') : '<span class="empty-mark">◷</span><strong>No activity yet</strong><p>Registrations and verification events will appear here.</p>';
  } catch (error) {
    $('#recent').innerHTML = `<div class="form-result error">${esc(error.message)}</div>`;
  }
}

async function refreshInsurance() {
  const list = $('#insurance-list');
  try {
    const [policies, animals] = await Promise.all([MuzzleApi.policies(), MuzzleApi.animalOptions()]);
    const select = $('#insurance-animal');
    const selected = select.value;
    select.innerHTML = '<option value="">Choose animal</option>' + animals.map(animal => `<option value="${esc(animal.id)}">${esc(animal.label)}</option>`).join('');
    if (selected) select.value = selected;
    list.innerHTML = policies.length ? `<table><thead><tr><th>ANIMAL</th><th>PROVIDER / POLICY</th><th>COVERAGE</th><th>PERIOD</th><th>STATUS</th><th>ACTIONS</th></tr></thead><tbody>${policies.map(policy => `<tr><td><a href="#animal/${encodeURIComponent(policy.animal_id)}">${esc(animals.find(a => a.id === policy.animal_id)?.label || policy.animal_id)}</a></td><td>${esc(policy.provider)}<br><small>${esc(policy.policy_number)}</small></td><td>${esc(Number(policy.coverage_amount).toLocaleString())}</td><td>${esc(policy.start_date)}–${esc(policy.end_date)}</td><td><select class="policy-status" data-id="${esc(policy.id)}"><option ${policy.status === 'pending' ? 'selected' : ''}>pending</option><option ${policy.status === 'active' ? 'selected' : ''}>active</option><option ${policy.status === 'expired' ? 'selected' : ''}>expired</option><option ${policy.status === 'cancelled' ? 'selected' : ''}>cancelled</option><option ${policy.status === 'claim_reported' ? 'selected' : ''}>claim_reported</option></select></td><td><button type="button" class="text-button policy-delete" data-id="${esc(policy.id)}">Delete</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty-state compact"><strong>No policy records</strong><p>Add policy metadata for a registered animal.</p></div>';
    list.querySelectorAll('.policy-status').forEach(control => control.addEventListener('change', async () => { try { await MuzzleApi.updatePolicy(control.dataset.id, { status: control.value }); } catch (error) { alert(error.message); await refreshInsurance(); } }));
    list.querySelectorAll('.policy-delete').forEach(button => button.addEventListener('click', async () => { if (!confirm('Delete this policy record?')) return; try { await MuzzleApi.deletePolicy(button.dataset.id); await refreshInsurance(); } catch (error) { alert(error.message); } }));
  } catch (error) { list.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function refreshAlerts() {
  const target = $('#alerts-list');
  try {
    const rows = await MuzzleApi.alerts($('#alert-filter').value);
    target.className = rows.length ? 'table-wrap' : 'empty-state compact';
    target.innerHTML = rows.length ? `<table><thead><tr><th>SEVERITY</th><th>ALERT</th><th>ANIMAL</th><th>CREATED</th><th></th></tr></thead><tbody>${rows.map(alert => `<tr><td>${esc(alert.severity)}</td><td>${esc(alert.message)}<br><small>${esc(alert.category.replaceAll('_',' '))}</small></td><td>${alert.animal_id ? `<a href="#animal/${encodeURIComponent(alert.animal_id)}">${esc(alert.animal_id.slice(0,8))}</a>` : '—'}</td><td>${esc(new Date(alert.created_at).toLocaleString())}</td><td>${alert.status === 'open' ? `<button type="button" class="text-button resolve-alert" data-id="${esc(alert.id)}">Resolve</button>` : 'Resolved'}</td></tr>`).join('')}</tbody></table>` : '<strong>No alerts match this filter.</strong>';
    target.querySelectorAll('.resolve-alert').forEach(button => button.addEventListener('click', async () => { try { await MuzzleApi.updateAlert(button.dataset.id, 'resolved'); await refreshAlerts(); } catch (error) { target.insertAdjacentHTML('afterbegin', `<p class="form-result error">${esc(error.message)}</p>`); } }));
  } catch (error) { target.className = 'empty-state compact'; target.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function refreshAnalytics() {
  const target = $('#analytics-content');
  try {
    const data = await MuzzleApi.analytics();
    const profiles = Object.entries(data.animal_profiles).map(([label,value]) => `<div class="detail-cell"><small>${esc(label.replaceAll('_',' '))}</small><strong>${value}</strong></div>`).join('');
    const decisions = Object.entries(data.verification_last_30_days).map(([label,value]) => `<div class="detail-cell"><small>${esc(label)}</small><strong>${value}</strong></div>`).join('');
    const registrations = data.registrations_last_30_days.map(row => `<tr><td>${esc(row.date)}</td><td>${row.count}</td></tr>`).join('');
    target.className = 'analytics-grid';
    target.innerHTML = `<div class="detail-grid"><div class="detail-cell"><small>TOTAL REGISTERED ANIMALS</small><strong>${data.total_animals}</strong></div><div class="detail-cell"><small>OPEN OPERATIONAL ALERTS</small><strong>${data.open_alerts}</strong></div><div class="detail-cell"><small>POLICY RECORDS</small><strong>${Object.values(data.policies).reduce((sum,n) => sum+n,0)}</strong></div></div><h3>Profiles</h3><div class="detail-grid">${profiles || '<p>No profiles yet.</p>'}</div><h3>Verification events · last 30 days</h3><div class="detail-grid">${decisions}</div><h3>Daily registrations · last 30 days</h3><table><thead><tr><th>DATE</th><th>REGISTRATIONS</th></tr></thead><tbody>${registrations || '<tr><td colspan="2">No registrations in this period.</td></tr>'}</tbody></table>`;
  } catch (error) { target.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function refreshSettings() {
  const target = $('#settings-content');
  try {
    const settings = await MuzzleApi.settings();
    target.className = 'detail-grid settings-grid';
    target.innerHTML = Object.entries({ 'Operator password': settings.operator_password_configured, 'Session signing key': settings.session_secret_configured, 'Encrypted image and embedding storage': settings.data_encryption_configured, 'Secure session cookie': settings.cookie_secure, 'Database': settings.database_type, 'Trained model': settings.model_available, 'FAISS runtime': settings.faiss_available, 'Model version': settings.model_version || 'Unavailable' }).map(([label,value]) => `<div class="detail-cell"><small>${esc(label)}</small><strong>${typeof value === 'boolean' ? value ? 'Configured' : 'Missing' : esc(value)}</strong></div>`).join('') + '<p class="form-note">This single-operator prototype has no user roles or organization isolation. Model matches are not valid evidence for ownership, insurance claims, or other consequential decisions.</p>';
  } catch (error) { target.innerHTML = `<p class="form-result error">${esc(error.message)}</p>`; }
}

async function showAnimalDetails(id) {
  const host = $('#animal-detail-content');
  host.innerHTML = '<div class="empty-state compact"><strong>Loading animal record…</strong></div>';
  try {
    const animal = await MuzzleApi.animal(id);
    const prototypeReady = animal.status === 'prototype_ready';
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="detail-heading"><div><p class="eyebrow">ANIMAL PROFILE</p><h2>${esc(animal.name || 'Unnamed animal')}</h2><p class="subhead">Record ${esc(animal.id)}</p></div><span class="tag">${esc(animal.status.replaceAll('_', ' '))}</span></div><div class="detail-grid"><div><small>EAR TAG</small><strong>${esc(animal.ear_tag || 'Not available')}</strong></div><div><small>BREED</small><strong>${esc(animal.breed || 'Not available')}</strong></div><div><small>SEX</small><strong>${esc(animal.sex || 'Not available')}</strong></div><div><small>DATE OF BIRTH</small><strong>${esc(animal.date_of_birth || 'Not available')}</strong></div><div><small>REFERENCE IMAGES</small><strong>${animal.images.length} saved</strong></div><div><small>REGISTERED</small><strong>${esc(new Date(animal.created_at).toLocaleString())}</strong></div></div><div class="detail-images"><h3>Stored reference images</h3>${animal.images.length ? `<div class="stored-image-grid">${animal.images.map((image,index) => { const flags = image.quality?.warnings || []; return `<figure><img loading="lazy" src="/api/animals/${encodeURIComponent(animal.id)}/images/${encodeURIComponent(image.id)}" alt="Stored reference ${index + 1} for ${esc(animal.name || 'animal')}"><figcaption>Reference ${index + 1} · ${image.width} × ${image.height} · ${esc(image.format)}${flags.length ? ` · Capture flags: ${esc(flags.join(', '))}` : ''}</figcaption></figure>`; }).join('')}</div>` : '<p>No stored reference images.</p>'}<p class="form-note">Capture flags are simple image heuristics; they do not confirm muzzle visibility or image identity.</p></div><form id="animal-edit-form" class="panel"><h3>Edit animal details</h3><div class="form-row"><label>Name<input name="name" maxlength="120" value="${esc(animal.name)}"></label><label>Ear tag<input name="ear_tag" maxlength="120" value="${esc(animal.ear_tag)}"></label></div><div class="form-row"><label>Breed<input name="breed" maxlength="120" value="${esc(animal.breed)}"></label><label>Sex<select name="sex"><option value="">Select</option><option ${animal.sex === 'Female' ? 'selected' : ''}>Female</option><option ${animal.sex === 'Male' ? 'selected' : ''}>Male</option></select></label></div><label>Date of birth<input name="date_of_birth" type="date" value="${esc(animal.date_of_birth)}"></label><button class="button primary" type="submit">Save changes</button><button class="button secondary" id="delete-animal" type="button">Delete animal record</button><p class="form-result" id="animal-edit-result" role="status"></p></form><div class="status-banner detail-notice"><span class="status-icon">i</span><div><strong>Biometric profile: ${esc(animal.status.replaceAll('_', ' '))}</strong><p>${prototypeReady ? 'Prototype full-image embeddings are available. Muzzle detection and population-level validation are missing; require in-person confirmation.' : 'Biometric embeddings are pending, stale, or unavailable; reindex from the stored images when model artifacts are ready.'}</p>${['pending_model','stale_model','reindex_required'].includes(animal.status) && animal.images.length ? '<button class="button secondary" id="reindex-animal" type="button">Rebuild biometric profile</button>' : ''}</div></div>`;
    $('#reindex-animal')?.addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; button.textContent = 'Rebuilding…'; try { await MuzzleApi.reindexAnimal(animal.id); await showAnimalDetails(animal.id); } catch (error) { button.disabled = false; button.textContent = 'Rebuild biometric profile'; $('#animal-edit-result').textContent = error.message; } });
    const editForm = $('#animal-edit-form');
    editForm.addEventListener('submit', async event => { event.preventDefault(); const result = $('#animal-edit-result'); try { const form = new FormData(editForm); const value = Object.fromEntries(form); value.date_of_birth = value.date_of_birth || null; await MuzzleApi.updateAnimal(animal.id, value); await showAnimalDetails(animal.id); $('#animal-edit-result').className = 'form-result success'; $('#animal-edit-result').textContent = 'Animal details saved.'; } catch (error) { result.className = 'form-result error'; result.textContent = error.message; } });
    $('#delete-animal').addEventListener('click', async () => { if (!confirm('Permanently delete this animal, its encrypted reference images, embeddings, and policy records? Verification history remains with the animal link removed.')) return; try { await MuzzleApi.deleteAnimal(animal.id); location.hash = '#animals'; await refreshRegistry(); } catch (error) { $('#animal-edit-result').className = 'form-result error'; $('#animal-edit-result').textContent = error.message; } });
  } catch (error) {
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="empty-state compact error-text">${esc(error.message)}</div>`;
  }
}

function route() {
  const path = decodeURIComponent(location.hash.slice(1) || 'overview').replace(/^\//, '');
  const [routeName, id] = path.split('/');
  const page = routeName.startsWith('animal') && id ? 'details' : (PAGE_LABELS[routeName] ? routeName : 'overview');
  const visiblePage = page;
  document.querySelectorAll('[data-view]').forEach(element => {
    const visibleViews = (element.dataset.view || '').split(/\s+/);
    element.classList.toggle('route-hidden', !visibleViews.includes(visiblePage));
  });
  const navKey = page === 'details' ? 'animals' : page;
  document.querySelectorAll('.nav-item').forEach(link => link.classList.toggle('active', link.getAttribute('href') === `#${navKey}`));
  $('#page-label').textContent = page === 'details' ? 'Animal details' : (PAGE_LABELS[page] || 'Dashboard');
  if (page === 'animals' || page === 'overview') refreshRegistry();
  if (page === 'overview') refreshDashboard();
  if (page === 'history') refreshHistory();
  if (page === 'details' && id) showAnimalDetails(id);
  if (page === 'system') refreshHealth();
  if (page === 'insurance') refreshInsurance();
  if (page === 'alerts') refreshAlerts();
  if (page === 'analytics') refreshAnalytics();
  if (page === 'settings') refreshSettings();
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
  previewArea.innerHTML = files.map((file,index) => `<div class="image-preview"><img alt="Preview of ${esc(file.name)}"><div><strong>${esc(file.name)}</strong><small>${(file.size / 1024 / 1024).toFixed(2)} MB</small></div><button type="button" class="remove-image" data-index="${index}" aria-label="Remove ${esc(file.name)}">×</button></div>`).join('');
  previewArea.querySelectorAll('img').forEach((image,index) => { const url = URL.createObjectURL(files[index]); previewUrls.push(url); image.src = url; });
  previewArea.querySelectorAll('.remove-image').forEach(button => button.addEventListener('click', () => setFiles(files.filter((_,index) => index !== Number(button.dataset.index)))));
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
  if (files.some(file => !['image/jpeg','image/png','image/webp'].includes(file.type))) { result.className = 'form-result error'; result.textContent = 'Use JPEG, PNG or WebP images.'; return; }
  submitInProgress = true;
  const submit = registerForm.querySelector('[type=submit]');
  submit.disabled = true; submit.dataset.label = submit.textContent; submit.textContent = 'Saving record…';
  result.className = 'form-result'; result.textContent = 'Validating and saving animal record…';
  try {
    const response = await MuzzleApi.register(new FormData(registerForm), idempotencyKey);
    if (!response.persisted) throw new Error('The server did not confirm that the record was persisted.');
    result.className = 'form-result success';
    result.textContent = `${response.message} Record ID: ${response.id}`;
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
    result.className = 'form-result success'; result.textContent = `PROTOTYPE ONLY — ${response.decision}${response.animal_id ? `: ${response.animal_id}` : ''}. Human confirmation required; do not use as ownership or insurance evidence.`;
  } catch (error) { result.className = 'form-result error'; result.textContent = error.message; }
  finally { submit.disabled = false; }
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
$('#previous-page').addEventListener('click', () => { registryOffset = Math.max(0, registryOffset - pageSize); refreshRegistry(); });
$('#next-page').addEventListener('click', () => { registryOffset += pageSize; refreshRegistry(); });
window.addEventListener('hashchange', route);
window.addEventListener('muzzleid:session-expired', () => {
  document.body.classList.remove('authenticated'); authGate.hidden = false;
  $('#login-result').textContent = 'Your operator session expired. Sign in again.';
});

const authGate = $('#auth-gate');
$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const result = $('#login-result');
  const button = event.currentTarget.querySelector('[type=submit]');
  button.disabled = true; result.textContent = 'Signing in…'; result.className = 'form-result';
  try {
    await MuzzleApi.login($('#operator-password').value);
    document.body.classList.add('authenticated'); authGate.hidden = true;
    $('#operator-password').value = ''; result.textContent = '';
    await refreshHealth(); route();
  } catch (error) { result.textContent = error.message; result.className = 'form-result error'; }
  finally { button.disabled = false; }
});
$('#logout-button').addEventListener('click', async () => {
  try { await MuzzleApi.logout(); } catch { /* Clear the browser gate if the API is unavailable. */ }
  document.body.classList.remove('authenticated'); authGate.hidden = false;
  $('#login-result').textContent = 'Signed out.'; $('#operator-password').focus();
});
(async () => {
  try {
    const session = await MuzzleApi.session();
    if (!session.configured) $('#login-result').textContent = 'Server setup required: configure the operator password and session secret in .env.';
    if (session.authenticated) {
      document.body.classList.add('authenticated'); authGate.hidden = true;
      await refreshHealth(); route();
    } else authGate.hidden = false;
  } catch { authGate.hidden = false; $('#login-result').textContent = 'MuzzleID API is unavailable.'; }
})();
