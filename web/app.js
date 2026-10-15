const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const PAGE_LABELS = { landing: 'Welcome', overview: 'Dashboard', animals: 'Animal database', register: 'Register animal', verify: 'Identify animal', history: 'Verification history', insurance: 'Insurance', alerts: 'Alerts and review', analytics: 'Analytics', reports: 'Reports', system: 'System status', settings: 'Settings' };
const PLANNED = {
  insurance: 'Insurance records and identity attestation workflows are planned. No insurance decisions or partner integrations are available in this MVP.',
  alerts: 'Duplicate and suspicious identity review is planned. There are no active fraud alerts in this MVP.',
  analytics: 'Operational analytics will be available after validated verification events exist.',
  reports: 'Exportable reports are planned. No performance or customer metrics are available.',
  settings: 'Additional user profiles, organization isolation and role controls are planned. The prototype currently uses one configured operator password.',
};
let registryOffset = 0;
let registryQuery = '';
let registrySort = 'created_at';
const pageSize = 25;

async function refreshHealth() {
  try {
    const health = await MuzzleApi.health();
    $('#system-status').textContent = health.database_available ? 'Online' : 'Degraded';
    $('#engine-status').textContent = health.calibrated_thresholds_available ? 'Prototype matching active' : 'Biometric engine unavailable';
    $('#status-title').textContent = !health.database_available ? 'Database connection is unavailable' : health.calibrated_thresholds_available ? 'Prototype biometric matching is available' : 'Biometric engine is not configured';
    $('#status-description').textContent = !health.database_available
      ? 'The API could not confirm its database connection. Registry and registration actions may fail until the database is available.'
      : health.calibrated_thresholds_available
      ? 'Prototype full-image embeddings are enabled. No muzzle detector or unseen-animal validation is available; every match requires human confirmation.'
      : 'Animal records and image uploads are available. Matching requires a trained checkpoint and its matching validation calibration.';
    $('#status-tag').textContent = !health.database_available ? 'DATABASE ISSUE' : health.calibrated_thresholds_available ? 'PROTOTYPE' : 'SETUP REQUIRED';
    const lock = document.querySelector('#verify-form .locked');
    if (lock) {
      lock.hidden = health.calibrated_thresholds_available;
      lock.querySelector('strong').textContent = health.calibrated_thresholds_available ? 'Prototype mode only' : 'Verification currently unavailable';
      lock.querySelector('small').textContent = health.calibrated_thresholds_available ? 'No muzzle detector or open-set validation. Confirm every result in person.' : 'Training and calibration artifacts are not installed.';
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

async function showAnimalDetails(id) {
  const host = $('#animal-detail-content');
  host.innerHTML = '<div class="empty-state compact"><strong>Loading animal record…</strong></div>';
  try {
    const animal = await MuzzleApi.animal(id);
    const prototypeReady = animal.status === 'prototype_ready';
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="detail-heading"><div><p class="eyebrow">ANIMAL PROFILE</p><h2>${esc(animal.name || 'Unnamed animal')}</h2><p class="subhead">Record ${esc(animal.id)}</p></div><span class="tag">${esc(animal.status.replaceAll('_', ' '))}</span></div><div class="detail-grid"><div><small>EAR TAG</small><strong>${esc(animal.ear_tag || 'Not available')}</strong></div><div><small>BREED</small><strong>${esc(animal.breed || 'Not available')}</strong></div><div><small>SEX</small><strong>${esc(animal.sex || 'Not available')}</strong></div><div><small>DATE OF BIRTH</small><strong>${esc(animal.date_of_birth || 'Not available')}</strong></div><div><small>REFERENCE IMAGES</small><strong>${animal.images.length} saved</strong></div><div><small>REGISTERED</small><strong>${esc(new Date(animal.created_at).toLocaleString())}</strong></div></div><div class="detail-images"><h3>Stored reference images</h3>${animal.images.length ? animal.images.map((image,index) => `<div class="stored-image"><span>Reference ${index + 1}</span><small>${image.width} × ${image.height}</small><code>${esc(image.content_sha256.slice(0, 16))}…</code></div>`).join('') : '<p>No stored reference images.</p>'}<p class="form-note">Stored files are not served from the public web path. Hashes are shown for record integrity.</p></div><div class="status-banner detail-notice"><span class="status-icon">i</span><div><strong>Biometric profile: ${esc(animal.status.replaceAll('_', ' '))}</strong><p>${prototypeReady ? 'Prototype full-image embeddings are indexed. The model has no muzzle detector or unseen-animal validation; confirm identity in person.' : 'Embedding and gallery indexing are unavailable until a trained model and matching calibration are installed.'}</p></div></div>`;
  } catch (error) {
    host.innerHTML = `<a class="back-link" href="#animals">← Animal database</a><div class="empty-state compact error-text">${esc(error.message)}</div>`;
  }
}

function route() {
  const path = decodeURIComponent(location.hash.slice(1) || 'overview').replace(/^\//, '');
  const [routeName, id] = path.split('/');
  const page = routeName.startsWith('animal') && id ? 'details' : (PAGE_LABELS[routeName] ? routeName : 'overview');
  const visiblePage = PLANNED[page] ? 'planned' : page;
  document.querySelectorAll('[data-view]').forEach(element => {
    const visibleViews = (element.dataset.view || '').split(/\s+/);
    element.classList.toggle('route-hidden', !visibleViews.includes(visiblePage));
  });
  const navKey = page === 'details' ? 'animals' : page;
  document.querySelectorAll('.nav-item').forEach(link => link.classList.toggle('active', link.getAttribute('href') === `#${navKey}`));
  $('#page-label').textContent = page === 'details' ? 'Animal details' : (PAGE_LABELS[page] || 'Dashboard');
  if (PLANNED[page]) {
    $('#planned-title').textContent = PAGE_LABELS[page];
    $('#planned-description').textContent = PLANNED[page];
  }
  if (page === 'animals' || page === 'overview') refreshRegistry();
  if (page === 'history') refreshHistory();
  if (page === 'details' && id) showAnimalDetails(id);
  if (page === 'system') refreshHealth();
}

const registerForm = $('#register-form');
const uploadInput = $('#register-files');
const previewArea = $('#image-previews');
let idempotencyKey = crypto.randomUUID();
let submitInProgress = false;
let previewUrls = [];

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
