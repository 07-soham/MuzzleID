/* Shared API client. JSDoc keeps the plain-JavaScript portal's payload contracts explicit. */
window.MuzzleApi = (() => {
  const errorMessage = async response => {
    let payload = {};
    try { payload = await response.json(); } catch { /* An empty response is handled below. */ }
    return payload.detail || payload.message || `Request failed (${response.status}).`;
  };
  async function request(path, options = {}) {
    const response = await fetch(path, options);
    if (response.status === 401 && path !== '/api/login') window.dispatchEvent(new Event('muzzleid:session-expired'));
    if (!response.ok) throw new Error(await errorMessage(response));
    return response.status === 204 ? null : response.json();
  }
  return {
    session: () => request('/api/session'),
    login: password => request('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) }),
    logout: () => request('/api/logout', { method: 'POST' }),
    /** @returns {Promise<{status:string,database_available:boolean,model_available:boolean,decision_thresholds:string}>} */
    health: () => request('/api/health'),
    /** @returns {Promise<{items:Array,total:number,offset:number,limit:number}>} */
    animals: ({ q = '', offset = 0, limit = 25, sort = 'created_at', status = 'all' } = {}) => request(`/api/animals?${new URLSearchParams({ q, offset, limit, sort, status })}`),
    animalOptions: () => request('/api/animal-options'),
    animal: id => request(`/api/animals/${encodeURIComponent(id)}`),
    updateAnimal: (id, fields) => request(`/api/animals/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields) }),
    reindexAnimal: id => request(`/api/animals/${encodeURIComponent(id)}/reindex`, { method: 'POST' }),
    deleteAnimal: id => request(`/api/animals/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    /** @param {FormData} form @param {string} idempotencyKey */
    register: (form, idempotencyKey) => request('/api/animals', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: form }),
    verify: form => request('/api/verify', { method: 'POST', body: form }),
    history: () => request('/api/history'),
    policies: (status = 'all') => request(`/api/insurance/policies?status=${encodeURIComponent(status)}`),
    createPolicy: payload => request('/api/insurance/policies', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
    updatePolicy: (id, fields) => request(`/api/insurance/policies/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields) }),
    deletePolicy: id => request(`/api/insurance/policies/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    alerts: status => request(`/api/alerts?status=${encodeURIComponent(status)}`),
    updateAlert: (id, status) => request(`/api/alerts/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }),
    analytics: () => request('/api/analytics'),
    settings: () => request('/api/settings'),
  };
})();
