'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, folders: [], documents: [], employees: [], events: [], view: 'home', folderId: null, health: null, epoch: 0, chatBusy: false, chatGeneration: 0 };
  const viewNames = { home: 'Home', ask: 'Ask knowledge', library: 'Document library', approvals: 'Approvals', ingestion: 'Ingestion monitor', pdfdebug: 'PDF object inspector', access: 'Folders & access', audit: 'Activity log', settings: 'Processing settings', graph: 'Knowledge graph' };
  let toastTimer;
  let dialogBusy = false;
  let loginLoaded = false;
  let folderRequest = 0;
  let ingestionTimer = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }
  function button(text, className = 'button secondary', iconName) {
    const node = el('button', className);
    node.type = 'button';
    if (iconName) node.append(icon(iconName));
    node.append(document.createTextNode(text));
    return node;
  }
  function text(value, fallback = '—') {
    if (value === null || value === undefined || value === '') return fallback;
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
  }
  function initials(name) { return String(name || 'Employee').trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase(); }
  function formatDate(value, withTime = false) {
    if (!value) return '—';
    const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? `${value}T12:00:00` : value);
    if (Number.isNaN(d.getTime())) return text(value);
    return new Intl.DateTimeFormat(undefined, withTime ? { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' } : { day: 'numeric', month: 'short', year: 'numeric' }).format(d);
  }
  function today() {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }
  function sameId(a, b) { return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b); }
  function folderById(id) { return state.folders.find((folder) => sameId(folder.id, id)); }
  function roles(folder) { return (Array.isArray(folder?.roles) ? folder.roles : []).map((role) => String(typeof role === 'object' ? role.role : role).toLowerCase()); }
  function canManage(folder) { return sameId(folder?.owner_id, state.user?.id) || roles(folder).some((role) => ['owner', 'delegate'].includes(role)); }
  function canMake(folder) { return canManage(folder) || roles(folder).includes('maker'); }
  function canCheck(folder) { return canManage(folder) || roles(folder).includes('checker'); }
  function statusKey(doc) { if (doc.processing_request?.kind === 'initial' && doc.processing_request.status === 'cancelled' && doc.status !== 'superseded') return 'cancelled'; return String(doc.status || 'unknown').toLowerCase().replace(/[\s-]+/g, '_'); }
  function isPending(doc) { return doc.processing_request ? doc.processing_request.status === 'pending' : ['pending', 'pending_approval', 'staged', 'awaiting_approval'].includes(statusKey(doc)); }
  function canCancelRequest(doc) { const request = doc.processing_request; return request && ['pending', 'approved'].includes(request.status) && sameId(request.maker_id, state.user?.id) && canMake(folderById(doc.folder_id)); }
  function cancelRequestButton(requestId) {
    const control = button('Cancel request', 'mini-button');
    control.addEventListener('click', () => busy(control, async () => {
      await api(`/api/processing-requests/${encodeURIComponent(requestId)}/cancel`, { method: 'POST', body: {} });
      notify('Request cancelled. It is no longer awaiting approval or queued for processing.');
      await refreshDocuments();
      if (state.view === 'ingestion') await refreshIngestion();
    }).catch(alertError));
    return control;
  }
  function isFailed(doc) { return ['failed', 'error', 'processing_failed'].includes(statusKey(doc)); }
  function statusBadge(doc) {
    const key = statusKey(doc);
    const labels = { pending: 'Pending approval', pending_approval: 'Pending approval', staged: 'Pending approval', awaiting_approval: 'Pending approval', indexed: 'Indexed', approved: 'Approved', ready: 'Ready', processing: 'Processing', ingesting: 'Processing', rejected: 'Rejected', failed: 'Failed', error: 'Failed', archived: 'Archived', cancelled: 'Cancelled' };
    const badge = el('span', 'status', labels[key] || key.replaceAll('_', ' '));
    if (/^[a-z_]+$/.test(key)) badge.classList.add(key);
    return badge;
  }
  function safeJSON(value) {
    const output = JSON.stringify(value, (key, item) => /password|secret|api.?key|authorization|access_token|refresh_token/i.test(key) ? '[REDACTED]' : item, 2);
    if (output === undefined) return 'No result returned.';
    return output.length > 14000 ? `${output.slice(0, 14000)}\n… (display shortened)` : output;
  }
  function errorMessage(detail) {
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map((item) => typeof item === 'object' ? `${(item.loc || []).filter((x) => x !== 'body').join(' · ')}: ${item.msg || 'Invalid value'}` : String(item)).join('\n');
    if (detail && typeof detail === 'object') return text(detail.message || detail.detail || detail);
    return 'The request could not be completed. Please try again.';
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), path === '/chat' ? 1080000 : 60000);
    const headers = new Headers(options.headers || {});
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, { ...options, body, headers, credentials: 'include', signal: controller.signal });
      const type = response.headers.get('content-type') || '';
      const data = type.includes('application/json') ? await response.json() : null;
      if (!response.ok) {
        const error = new Error(errorMessage(data?.detail || data?.error || (response.status === 401 ? 'Your session has ended. Please sign in again.' : `Request failed (${response.status}).`)));
        error.status = response.status;
        error.detail = data?.detail;
        if (response.status === 401 && state.user && path !== '/api/login') showLogin(error.message);
        throw error;
      }
      return data || {};
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request took too long. Please try again.');
      if (error instanceof TypeError) throw new Error('Unable to connect to the workspace. Check your connection and try again.');
      throw error;
    } finally { window.clearTimeout(timer); }
  }
  function notify(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { $('toast').hidden = true; }, 5500);
  }
  function alertError(error) {
    if (!state.user) return;
    $('global-alert-text').textContent = error.message || text(error);
    $('global-alert').hidden = false;
  }
  async function busy(target, work) {
    const controls = target.matches('button') ? [target] : [...target.querySelectorAll('button, input, select, textarea')];
    const previous = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    target.setAttribute('aria-busy', 'true');
    try { return await work(); }
    finally {
      controls.forEach((control, i) => { control.disabled = previous[i]; });
      target.removeAttribute('aria-busy');
    }
  }
  function loading(node, label = 'Loading…') {
    const status = el('div', 'skeleton-text');
    status.setAttribute('role', 'status');
    status.append(el('span', 'loading-spinner'), document.createTextNode(label));
    node.replaceChildren(status);
  }
  function empty(node, title, description, iconName = 'book') {
    const content = el('div', 'empty-state');
    content.append(icon(iconName), el('h3', '', title), el('p', '', description));
    node.replaceChildren(content);
    return content;
  }
  function field(label, type = 'text', value = '', options = {}) {
    const wrap = el('div', 'field');
    const id = options.id || `field-${++field.counter}`;
    const input = type === 'textarea' ? el('textarea') : type === 'select' ? el('select') : el('input');
    input.id = id;
    if (input.tagName === 'INPUT') input.type = type;
    if (type !== 'select') input.value = value;
    if (options.required) input.required = true;
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.maxLength) input.maxLength = options.maxLength;
    const labelNode = el('label', '', label);
    labelNode.htmlFor = id;
    wrap.append(labelNode, input);
    if (options.help) {
      const help = el('p', 'field-help', options.help);
      help.id = `${id}-help`;
      input.setAttribute('aria-describedby', help.id);
      wrap.append(help);
    }
    return { wrap, input };
  }
  field.counter = 0;
  function option(select, label, value) {
    const node = el('option', '', label);
    node.value = value;
    select.append(node);
    return node;
  }
  function inlineError() { const node = el('div', 'inline-error'); node.setAttribute('role', 'alert'); node.hidden = true; return node; }
  function showInline(node, error) { node.textContent = error.message || text(error); node.hidden = false; }
  function openDialog(title, eyebrow = 'WORKSPACE') {
    if ($('form-dialog').open) $('form-dialog').close();
    $('dialog-title').textContent = title;
    $('dialog-eyebrow').textContent = eyebrow;
    $('dialog-body').replaceChildren();
    dialogBusy = false;
    $('close-dialog').disabled = false;
    $('form-dialog').showModal();
    return $('dialog-body');
  }
  function dialogActions(form, submitLabel, errorNode, onSubmit) {
    const actions = el('div', 'dialog-actions');
    const cancel = button('Cancel', 'button secondary');
    cancel.addEventListener('click', () => { if (!dialogBusy) $('form-dialog').close(); });
    const submit = button(submitLabel, 'button primary');
    submit.type = 'submit';
    actions.append(cancel, submit);
    form.append(errorNode, actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (dialogBusy) return;
      errorNode.hidden = true;
      dialogBusy = true;
      $('close-dialog').disabled = true;
      try { await busy(form, onSubmit); }
      catch (error) { showInline(errorNode, error); }
      finally { dialogBusy = false; $('close-dialog').disabled = false; }
    });
  }
  const metadataKeys = new Set(['description', 'classification', 'domain', 'owner_note', 'licence_reference']);
  function metadataObject(value) {
    if (!value.trim()) return {};
    let parsed;
    try { parsed = JSON.parse(value); } catch { throw new Error('Additional metadata must be valid JSON, for example: {"description": "Policy update"}.'); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Metadata must be a JSON object with named fields.');
    if (Object.keys(parsed).some((key) => !metadataKeys.has(key))) throw new Error('Use the documented metadata fields only. Business context is inherited and cannot be edited.');
    if (Object.values(parsed).some((value) => typeof value !== 'string' && typeof value !== 'boolean')) throw new Error('Metadata values must be text or true / false.');
    return parsed;
  }
  function contextText(folder) {
    return ['business_unit', 'division', 'department', 'region', 'organisation'].map((key) => folder?.[key]).filter(Boolean).join(' · ');
  }
  function contextBox(select) {
    const box = el('div', 'business-context');
    const update = () => {
      const folder = folderById(select.value);
      box.replaceChildren(el('strong', '', 'Business context is inherited from the folder'), el('p', '', contextText(folder) || 'Select a business folder to see its predefined context.'));
    };
    select.addEventListener('change', update);
    update();
    return box;
  }

  function showLogin(message) {
    state.epoch++;
    state.chatGeneration++;
    state.chatBusy = false;
    state.user = null;
    $('pdfdebug-content').replaceChildren();
    delete $('pdfdebug-content').dataset.loaded;
    state.folders = [];
    state.documents = [];
    $('review-scope').value = '';
    state.employees = [];
    state.events = [];
    state.folderId = null;
    state.health = null;
    $('workspace').hidden = true;
    $('boot-screen').hidden = true;
    $('login-view').hidden = false;
    $('password').value = '';
    $('global-alert').hidden = true;
    $('toast').hidden = true;
    closeNavigation();
    if ($('form-dialog').open) $('form-dialog').close();
    if (message) showInline($('login-error'), new Error(message));
    else $('login-error').hidden = true;
    if (!loginLoaded) loadDemoUsers();
  }
  async function loadDemoUsers() {
    try {
      const data = await api('/api/demo-users');
      $('demo-user').replaceChildren();
      option($('demo-user'), 'Choose a sample employee', '');
      for (const user of data.users || []) {
        const node = option($('demo-user'), `${user.name} · ${user.business_unit || user.id}`, String(user.id));
        node.dataset.employeeId = String(user.id);
      }
      $('demo-user').closest('.field').hidden = !(data.users || []).length;
      if (data.demo_password) {
        $('demo-password-hint').textContent = `Demo password: ${data.demo_password}. Enter it above to authenticate the selected account.`;
        $('demo-password-hint').hidden = false;
      } else $('demo-password-hint').hidden = true;
      loginLoaded = true;
    } catch (error) { showInline($('login-error'), error); }
  }
  async function enterWorkspace(user) {
    state.epoch++;
    state.user = user;
    $('login-view').hidden = true;
    $('boot-screen').hidden = true;
    $('workspace').hidden = false;
    $('password').value = '';
    $('user-name').textContent = user.name || user.id || 'Employee';
    $('user-unit').textContent = user.business_unit || 'Enterprise workspace';
    $('user-avatar').textContent = initials(user.name);
    $('rail-avatar').textContent = initials(user.name);
    resetConversation();
    state.folderId = null;
    empty($('folder-detail'), 'Select a folder', 'Its details and access grants appear here.', 'folder');
    $('mode-badge').textContent = 'Checking service mode';
    $('mode-badge').className = 'mode-badge';
    $('mode-title').textContent = 'Service mode';
    $('mode-description').textContent = 'Checking service status.';
    const epoch = state.epoch;
    const [folders, documents] = await Promise.allSettled([api('/api/folders'), api('/api/documents')]);
    if (epoch !== state.epoch || !state.user) return;
    if (folders.status === 'fulfilled') state.folders = folders.value.folders || [];
    else alertError(folders.reason);
    if (documents.status === 'fulfilled') state.documents = documents.value.documents || [];
    else alertError(documents.reason);
    updateCounts();
    renderDocuments();
    renderApprovals();
    renderFolders();
    route();
    loadHealth();
  }
  function closeNavigation() {
    $('sidebar').classList.remove('open');
    $('sidebar-overlay').hidden = true;
    $('menu-button').setAttribute('aria-expanded', 'false');
    if (window.matchMedia('(max-width: 760px)').matches) $('sidebar').inert = true;
  }
  function openNavigation() {
    $('sidebar').inert = false;
    $('sidebar').classList.add('open');
    $('sidebar-overlay').hidden = false;
    $('menu-button').setAttribute('aria-expanded', 'true');
    $('sidebar').querySelector('.nav-item.active')?.focus();
  }
  function route() {
    if (!state.user) return;
    const requested = location.hash.slice(1);
    const next = Object.hasOwn(viewNames, requested) ? requested : 'home';
    const changed = state.view !== next;
    state.view = next;
    for (const [key, title] of Object.entries(viewNames)) {
      $(`view-${key}`).hidden = key !== next;
      if (key === next) $('breadcrumb-current').textContent = title;
    }
    // The PDF inspector has no sidebar link: it opens from the Ingestion monitor, which stays highlighted.
    const section = next === 'pdfdebug' ? 'ingestion' : next;
    for (const item of $('primary-nav').querySelectorAll('[data-view]')) {
      const active = item.dataset.view === section;
      item.classList.toggle('active', active);
      if (active) item.setAttribute('aria-current', next === section ? 'page' : 'location');
      else item.removeAttribute('aria-current');
    }
    document.title = `${viewNames[next]} | Knowledge`;
    document.querySelectorAll('[data-rail]').forEach((node) => node.classList.toggle('active', node.dataset.rail === next));
    requestAnimationFrame(moveIndicators);
    document.body.dataset.currentView = next;
    closeNavigation();
    if (changed) {
      $('main-content').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    if (next === 'home') renderHome();
    if (next === 'library') renderDocuments();
    if (next === 'approvals') renderApprovals();
    if (next === 'pdfdebug' && !$('pdfdebug-content').dataset.loaded) loadPdfChoices().catch(alertError);
    window.clearInterval(ingestionTimer); ingestionTimer = null;
    if (next === 'ingestion') { refreshIngestion().catch(alertError); ingestionTimer = window.setInterval(() => { if (state.user && state.view === 'ingestion' && !document.hidden) refreshIngestion().catch(() => {}); }, 5000); }
    if (next === 'access') { renderFolders(); if (state.folderId) selectFolder(state.folderId); }
    if (next === 'audit') refreshAudit().catch(alertError);
    if (next === 'settings') loadSettings().catch(alertError);
    if (next === 'graph') loadGraph().catch(alertError);
  }

  // ---------------------------------------------------------------- knowledge graph
  // One graph per folder. Explorer draws entities coloured by ontology type; the review queue lists
  // proposed links with their evidence. Owners and checkers build and review; only approved links
  // reach answers.
  const graphTypeColors = { Document: '#64748b', Policy: '#7c3aed', Rule: '#c2416c', Product: '#d97706', Organisation: '#2563eb',
    Person: '#0d9488', Process: '#15803d', Metric: '#b45309', Event: '#a21caf', Location: '#0891b2', Term: '#94a3b8' };
  const graphState = { folderId: null, data: null, ontology: null, tab: 'explore', status: 'all', search: '', selected: null, poll: null,
    hiddenTypes: new Set(), layout: null, view: null, observer: null, mode: '2d', layout3d: null, camera3d: null };
  async function loadGraph() {
    const host = $('graph-content');
    const epoch = state.epoch;
    if (!state.folders.length) { empty(host, 'No folders', 'You need a folder grant to see its knowledge graph.', 'folder'); return; }
    if (!graphState.folderId || !folderById(graphState.folderId)) graphState.folderId = state.folders[0].id;
    if (!graphState.ontology) graphState.ontology = await api('/api/graph/ontology');
    const data = await api(`/api/graph?folder_id=${encodeURIComponent(graphState.folderId)}`);
    if (epoch !== state.epoch || state.view !== 'graph') return;
    graphState.data = data;
    renderGraph();
    const running = data.builds.find((b) => ['queued', 'running'].includes(b.status));
    window.clearTimeout(graphState.poll);
    if (running) graphState.poll = window.setTimeout(() => { if (state.view === 'graph') loadGraph().catch(() => {}); }, 4000);
  }
  function graphNodesById() { return new Map(graphState.data.nodes.map((n) => [n.id, n])); }
  function visibleLinks() {
    const { links } = graphState.data;
    if (graphState.status === 'all') return links.filter((l) => l.status !== 'rejected');
    return links.filter((l) => l.status === graphState.status);
  }
  function renderGraph() {
    const host = $('graph-content');
    const data = graphState.data;
    const toolbar = el('div', 'graph-toolbar');
    const folderSelect = el('select'); folderSelect.setAttribute('aria-label', 'Folder');
    state.folders.forEach((f) => option(folderSelect, f.name || f.path, f.id));
    folderSelect.value = graphState.folderId;
    folderSelect.addEventListener('change', () => { graphState.folderId = folderSelect.value; graphState.selected = null; loadGraph().catch(alertError); });
    const status = el('select'); status.setAttribute('aria-label', 'Links shown');
    [['all', 'Approved and proposed'], ['approved', 'Approved only'], ['proposed', 'Proposed only'], ['rejected', 'Rejected']].forEach(([v, t]) => option(status, t, v));
    status.value = graphState.status;
    status.addEventListener('change', () => { graphState.status = status.value; renderGraph(); });
    const search = el('input'); search.type = 'search'; search.placeholder = 'Find an entity'; search.value = graphState.search;
    search.setAttribute('aria-label', 'Find an entity');
    search.addEventListener('change', () => { graphState.search = search.value.trim(); renderGraph(); });
    const spacer = el('span', 'graph-spacer');
    toolbar.append(folderSelect, status, search, spacer);
    if (data.can_build) {
      const build = button('Build graph', 'button primary', 'refresh');
      build.disabled = data.builds.some((b) => ['queued', 'running'].includes(b.status));
      build.addEventListener('click', openGraphBuild);
      toolbar.append(build);
    }
    const nodes = [toolbar, graphBuildLine()];
    const proposed = data.links.filter((l) => l.status === 'proposed').length;
    const tabs = el('div', 'graph-tabs'); tabs.setAttribute('role', 'tablist');
    for (const [key, label] of [['explore', 'Explorer'], ['review', `Review queue (${proposed})`], ['entities', 'Main entities']]) {
      const tab = button(label, 'graph-tab'); tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(graphState.tab === key));
      tab.addEventListener('click', () => { graphState.tab = key; renderGraph(); });
      tabs.append(tab);
    }
    nodes.push(tabs);
    if (graphState.tab === 'entities') nodes.push(entityPanel());
    else if (!data.nodes.length) {
      const box = el('div'); empty(box, 'No graph yet', data.can_build ? 'Choose Build graph to read this folder\'s approved documents.' : 'A folder owner or checker can build it.', 'library');
      nodes.push(box);
    } else if (graphState.tab === 'review') nodes.push(graphReviewQueue());
    else nodes.push(graphExplorer());
    host.replaceChildren(...nodes);
  }
  function graphBuildLine() {
    const build = graphState.data.builds[0];
    const line = el('p', 'graph-build-line');
    if (!build) { line.textContent = `Ontology ${graphState.ontology.version}. No build yet for this folder.`; return line; }
    const when = formatDate(build.finished_at || build.created_at, true);
    if (['queued', 'running'].includes(build.status)) line.append(el('span', 'graph-spinner'), document.createTextNode(` Building: ${build.stage}`));
    else if (build.status === 'failed') { line.classList.add('failed'); line.textContent = `Last build failed (${when}): ${build.error}`; }
    else {
      const c = build.counts || {};
      const reopened = c.reviews_reopened ? ` · ${c.reviews_reopened} approvals back to review (claim changed)` : '';
      line.textContent = `Last build ${when} · ${c.documents} documents · ${c.nodes} entities · ${c.links} links · ${c.reviews_carried || 0} review decisions kept${reopened} · ontology ${build.ontology_version} · ${build.backend}`;
      if ((c.notes || []).length) line.title = c.notes.join('\n');
    }
    return line;
  }
  function openGraphBuild() {
    const body = openDialog('Build knowledge graph', 'KNOWLEDGE GRAPH');
    const form = el('form', 'graph-build-form');
    form.append(el('p', 'muted', 'Choose the approved documents to read. graphify extracts entities and links, then each is typed against the ontology. Every link starts as proposed until reviewed.'));
    form.append(el('p', 'graph-warning', 'Demo setting: document text is sent to Claude through the Claude CLI. In production this step uses the internal model.'));
    const list = el('div', 'graph-doc-list');
    for (const doc of graphState.data.documents) {
      const label = el('label', 'graph-doc');
      const box = el('input'); box.type = 'checkbox'; box.value = doc.id; box.checked = !doc.large;
      label.append(box, el('span', '', doc.title), el('small', '', doc.large ? `${doc.chunks} chunks, large: can take 30 minutes or more` : `${doc.chunks} chunks`));
      list.append(label);
    }
    if (!graphState.data.documents.length) list.append(el('p', 'muted', 'This folder has no indexed documents yet.'));
    form.append(list);
    const error = el('p', 'inline-error'); error.hidden = true;
    dialogActions(form, 'Start build', error, async () => {
      const ids = [...list.querySelectorAll('input:checked')].map((b) => b.value);
      if (!ids.length) throw new Error('Choose at least one document.');
      await api('/api/graph/builds', { method: 'POST', body: { folder_id: graphState.folderId, document_ids: ids } });
      $('form-dialog').close();
      notify('Graph build started. Progress shows above the graph.');
      await loadGraph();
    });
    body.append(form);
  }
  function seededRandom(text) {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
    return () => { h = Math.imul(h ^ (h >>> 15), 2246822507); h = Math.imul(h ^ (h >>> 13), 3266489909); h ^= h >>> 16; return (h >>> 0) / 4294967296; };
  }
  const graphShortLabel = (label) => (label.length > 34 ? `${label.slice(0, 32)}…` : label);
  const graphLabelWidth = (label) => graphShortLabel(label).length * 6.6 + 10;
  function graphLayout(nodes, links, frameW, frameH, widthOf) {
    // One force simulation in screen pixels: repulsion, springs along links, a pull to the middle
    // (weaker sideways, since the frame is wide), and collision between label boxes, all inside
    // the frame. A graph too big for the frame gets a larger virtual frame and its boxes are sized
    // for the zoom it will be shown at, so labels still do not overlap. Seeded: same graph, same picture.
    const need = nodes.reduce((sum, n) => sum + (widthOf(n.id) + 24) * 62, 0) * 2.4;
    const grow = Math.max(1, Math.sqrt(need / (frameW * frameH)));
    const W = frameW * grow, H = frameH * grow, k = 1 / grow;
    const ids = new Set(nodes.map((n) => n.id));
    const edges = links.filter((l) => ids.has(l.source) && ids.has(l.target) && l.source !== l.target);
    const rand = seededRandom(nodes.map((n) => n.id).sort().join('|'));
    const pts = nodes.map((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2;
      return { id: n.id, x: W / 2 + Math.cos(a) * W * 0.3 + rand() * 20, y: H / 2 + Math.sin(a) * H * 0.3 + rand() * 20, vx: 0, vy: 0, w: widthOf(n.id) / k, h: 48 / k };
    });
    const at = new Map(pts.map((p) => [p.id, p]));
    const springLength = 150 / k, steps = 500;
    for (let step = 0; step < steps; step++) {
      const cool = 1 - step / steps;
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i];
        for (let j = i + 1; j < pts.length; j++) {
          const b = pts[j];
          let dx = a.x - b.x, dy = a.y - b.y; const d2 = dx * dx + dy * dy + 0.01;
          if (d2 < 360000 / (k * k)) { const f = 700 / (k * d2); dx *= f; dy *= f; a.vx += dx; a.vy += dy; b.vx -= dx; b.vy -= dy; }
        }
      }
      for (const e of edges) {
        const a = at.get(e.source), b = at.get(e.target);
        const dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 1; const f = (d - springLength) * 0.06;
        a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
      for (const p of pts) {
        p.vx += (W / 2 - p.x) * 0.012; p.vy += (H / 2 - p.y) * 0.024;
        const v = Math.hypot(p.vx, p.vy); const cap = (30 * cool + 0.6) / k;
        if (v > cap) { p.vx *= cap / v; p.vy *= cap / v; }
        p.x += p.vx; p.y += p.vy; p.vx *= 0.5; p.vy *= 0.5;
      }
      if (step > steps * 0.25) {
        for (let i = 0; i < pts.length; i++) {
          const a = pts[i];
          for (let j = i + 1; j < pts.length; j++) {
            const b = pts[j];
            const ox = (a.w + b.w) / 2 + 6 / k - Math.abs(a.x - b.x), oy = (a.h + b.h) / 2 - Math.abs(a.y - b.y);
            if (ox <= 0 || oy <= 0) continue;
            if (oy < ox) { const s = (a.y < b.y ? -1 : 1) * oy * 0.5; a.y += s; b.y -= s; }
            else { const s = (a.x < b.x ? -1 : 1) * ox * 0.5; a.x += s; b.x -= s; }
          }
        }
      }
      for (const p of pts) {
        p.x = Math.min(W - p.w / 2 - 12 / k, Math.max(p.w / 2 + 12 / k, p.x));
        p.y = Math.min(H - 44 / k, Math.max(30 / k, p.y));
      }
    }
    return new Map(pts.map((p) => [p.id, { x: p.x, y: p.y }]));
  }
  // 3D view: a seeded 3D force layout drawn with perspective on a canvas. No library, so it runs
  // under the app's script policy. Drag rotates, scroll zooms, a press without movement selects.
  function graph3DLayout(nodes, links) {
    const rand = seededRandom(`3d|${nodes.map((n) => n.id).sort().join('|')}`);
    const pts = nodes.map((n) => ({ id: n.id, x: (rand() - 0.5) * 300, y: (rand() - 0.5) * 300, z: (rand() - 0.5) * 300, vx: 0, vy: 0, vz: 0 }));
    const at = new Map(pts.map((p) => [p.id, p]));
    const edges = links.filter((l) => at.has(l.source) && at.has(l.target) && l.source !== l.target);
    const steps = 420;
    for (let step = 0; step < steps; step++) {
      const cool = 1 - step / steps;
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i];
        for (let j = i + 1; j < pts.length; j++) {
          const b = pts[j];
          let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z; const d2 = dx * dx + dy * dy + dz * dz + 0.01;
          if (d2 > 300000) continue;
          const f = 2400 / d2; dx *= f; dy *= f; dz *= f;
          a.vx += dx; a.vy += dy; a.vz += dz; b.vx -= dx; b.vy -= dy; b.vz -= dz;
        }
      }
      for (const e of edges) {
        const a = at.get(e.source), b = at.get(e.target);
        const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z; const d = Math.hypot(dx, dy, dz) || 1; const f = (d - 90) * 0.05;
        a.vx += (dx / d) * f; a.vy += (dy / d) * f; a.vz += (dz / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f; b.vz -= (dz / d) * f;
      }
      for (const p of pts) {
        p.vx -= p.x * 0.01; p.vy -= p.y * 0.01; p.vz -= p.z * 0.01;
        const v = Math.hypot(p.vx, p.vy, p.vz); const cap = 20 * cool + 0.4;
        if (v > cap) { p.vx *= cap / v; p.vy *= cap / v; p.vz *= cap / v; }
        p.x += p.vx; p.y += p.vy; p.z += p.vz; p.vx *= 0.6; p.vy *= 0.6; p.vz *= 0.6;
      }
    }
    const mx = pts.reduce((s, p) => s + p.x, 0) / pts.length, my = pts.reduce((s, p) => s + p.y, 0) / pts.length, mz = pts.reduce((s, p) => s + p.z, 0) / pts.length;
    return new Map(pts.map((p) => [p.id, { x: p.x - mx, y: p.y - my, z: p.z - mz }]));
  }
  function graph3D(host, nodes, links, degree) {
    host.classList.add('is-3d');
    const cv = document.createElement('canvas'); cv.className = 'graph-3d';
    cv.setAttribute('role', 'img'); cv.setAttribute('aria-label', `Knowledge graph in 3D with ${nodes.length} entities and ${links.length} links. Drag to rotate.`);
    const ctx = cv.getContext('2d');
    const key = `${nodes.map((n) => n.id).sort().join(',')}#${links.map((l) => l.id).sort().join(',')}`;
    if (graphState.layout3d?.key !== key) graphState.layout3d = { key, pos: graph3DLayout(nodes, links) };
    let pos = graphState.layout3d.pos;
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (graphState.camera3d?.key !== key) graphState.camera3d = { key, yaw: 0.5, pitch: -0.3, zoom: 1, spin: !still };
    const cam = graphState.camera3d;
    const radiusOf = (p) => Math.hypot(p.x, p.y, p.z);
    let R = Math.max(60, ...[...pos.values()].map(radiusOf));
    const neighbours = new Map(nodes.map((n) => [n.id, new Set([n.id])]));
    links.forEach((l) => { neighbours.get(l.source)?.add(l.target); neighbours.get(l.target)?.add(l.source); });
    const sel = graphState.selected;
    const selectedNode = sel?.kind === 'node' && neighbours.has(sel.id) ? sel.id : null;
    const selectedLink = sel?.kind === 'link' ? links.find((l) => l.id === sel.id) : null;
    const font = getComputedStyle(document.body).fontFamily;
    const edgeColour = { approved: [111, 102, 105], proposed: [160, 151, 155], rejected: [227, 169, 174] };
    let size = { w: 0, h: 0 }, hover = null, drag = null, raf = 0, last = 0, projected = [];

    // Per-frame work is kept small: positions are projected into typed arrays, links are stroked in
    // a few batches (one path per status and depth band), and each label is painted once into a
    // small bitmap that is then stamped, since outlined text is the slowest thing a canvas draws.
    const N = nodes.length;
    const index = new Map(nodes.map((n, i) => [n.id, i]));
    const edges = links.filter((l) => index.has(l.source) && index.has(l.target)).map((l) => ({ l, a: index.get(l.source), b: index.get(l.target) }));
    const deg = nodes.map((n) => degree.get(n.id) || 0);
    const baseR = deg.map((d) => 6 + Math.min(10, Math.sqrt(d) * 2.6));
    const sx = new Float32Array(N), sy = new Float32Array(N), sp = new Float32Array(N), sn = new Float32Array(N);
    const order = nodes.map((_, i) => i);
    const byDegree = nodes.map((_, i) => i).sort((u, v) => deg[v] - deg[u] || u - v);
    const shortLabel = nodes.map((n) => graphShortLabel(n.label));
    const LEVELS = 6, STATUS = { approved: 0, proposed: 1, rejected: 2 }, STATUS_NAMES = ['approved', 'proposed', 'rejected'];
    const buckets = Array.from({ length: 3 * LEVELS }, () => []);
    const SPRITE_PX = 13, sprites = new Map(); let spriteDpr = 0;

    function projectAll() {
      const cy = Math.cos(cam.yaw), syaw = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), spitch = Math.sin(cam.pitch);
      const s = (Math.min(size.w, size.h) * 0.45 * cam.zoom) / R, hw = size.w / 2, hh = size.h / 2;
      for (let i = 0; i < N; i++) {
        const p = pos.get(nodes[i].id);
        const x = p.x * cy - p.z * syaw; let z = p.x * syaw + p.z * cy;
        const y = p.y * cp - z * spitch; z = p.y * spitch + z * cp;
        const persp = 760 / (760 + z * s);
        sx[i] = hw + x * s * persp; sy[i] = hh + y * s * persp; sp[i] = persp; sn[i] = 1 - (z / R + 1) / 2;
      }
    }
    function litSets() {
      const focus = hover || selectedNode;
      const nodesLit = new Set(), edgesLit = new Set();
      if (focus) { neighbours.get(focus).forEach((m) => nodesLit.add(m)); links.forEach((l) => { if (l.source === focus || l.target === focus) edgesLit.add(l.id); }); }
      else if (selectedLink) { nodesLit.add(selectedLink.source); nodesLit.add(selectedLink.target); edgesLit.add(selectedLink.id); }
      return { nodesLit, edgesLit };
    }
    function label(text, x, y, sizePx, colour, alpha) {
      ctx.globalAlpha = alpha; ctx.font = `600 ${sizePx}px ${font}`; ctx.textAlign = 'center';
      ctx.lineJoin = 'round'; ctx.lineWidth = 4; ctx.strokeStyle = '#fcfbfb'; ctx.strokeText(text, x, y);
      ctx.fillStyle = colour; ctx.fillText(text, x, y);
    }
    function sprite(text) {
      const dpr = window.devicePixelRatio || 1;
      if (dpr !== spriteDpr) { sprites.clear(); spriteDpr = dpr; }
      let s = sprites.get(text);
      if (!s) {
        ctx.font = `600 ${SPRITE_PX}px ${font}`;
        const w = Math.ceil(ctx.measureText(text).width) + 10, h = SPRITE_PX + 10;
        const c = document.createElement('canvas'); c.width = Math.ceil(w * dpr); c.height = Math.ceil(h * dpr);
        const g = c.getContext('2d'); g.scale(dpr, dpr);
        g.font = `600 ${SPRITE_PX}px ${font}`; g.textAlign = 'center'; g.lineJoin = 'round'; g.lineWidth = 4;
        g.strokeStyle = '#fcfbfb'; g.strokeText(text, w / 2, SPRITE_PX + 2); g.fillStyle = '#2b2527'; g.fillText(text, w / 2, SPRITE_PX + 2);
        s = { c, w, h }; sprites.set(text, s);
      }
      return s;
    }
    function nodeLabel(text, x, y, sizePx, alpha) {
      const s = sprite(text), k = sizePx / SPRITE_PX;
      ctx.globalAlpha = alpha; ctx.drawImage(s.c, x - (s.w * k) / 2, y - (SPRITE_PX + 2) * k, s.w * k, s.h * k);
    }
    function draw() {
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, size.w, size.h);
      projectAll();
      const { nodesLit, edgesLit } = litSets(); const focusing = nodesLit.size > 0;
      const rOf = (i) => baseR[i] * sp[i];
      const lit = [];
      for (const b of buckets) b.length = 0;
      for (const e of edges) {
        if (edgesLit.has(e.l.id) || selectedLink?.id === e.l.id) { lit.push(e); continue; }
        const level = Math.min(LEVELS - 1, Math.round(((sn[e.a] + sn[e.b]) / 2) * (LEVELS - 1)));
        buckets[(STATUS[e.l.status] ?? 1) * LEVELS + level].push(e);
      }
      ctx.globalAlpha = 1; ctx.lineWidth = 1.2;
      for (let bi = 0; bi < buckets.length; bi++) {
        const list = buckets[bi]; if (!list.length) continue;
        const status = Math.floor(bi / LEVELS), level = bi % LEVELS;
        let alpha = 0.22 + 0.55 * (level / (LEVELS - 1)); if (focusing) alpha *= 0.15;
        const [r, g, bl] = edgeColour[STATUS_NAMES[status]];
        ctx.strokeStyle = `rgba(${r},${g},${bl},${alpha})`; ctx.setLineDash(status === 0 ? [] : [5, 4]);
        ctx.beginPath();
        for (const e of list) { ctx.moveTo(sx[e.a], sy[e.a]); ctx.lineTo(sx[e.b], sy[e.b]); }
        ctx.stroke();
      }
      for (const e of lit) {  // lit or selected links: drawn one by one, with arrowheads and names
        const on = edgesLit.has(e.l.id);
        const [r, g, bl] = selectedLink?.id === e.l.id ? [201, 37, 50] : [60, 54, 57];
        let alpha = 0.22 + 0.55 * ((sn[e.a] + sn[e.b]) / 2); if (focusing && !on) alpha *= 0.15;
        ctx.strokeStyle = `rgba(${r},${g},${bl},${on ? 1 : alpha})`; ctx.lineWidth = on ? 2 : 1.2;
        ctx.setLineDash(e.l.status === 'approved' ? [] : [5, 4]);
        ctx.beginPath(); ctx.moveTo(sx[e.a], sy[e.a]); ctx.lineTo(sx[e.b], sy[e.b]); ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const e of lit) {
        if (!edgesLit.has(e.l.id)) continue;
        const colour = selectedLink?.id === e.l.id ? 'rgb(201, 37, 50)' : 'rgb(60,54,57)';
        const ax = sx[e.a], ay = sy[e.a], bx = sx[e.b], by = sy[e.b];
        const dx = bx - ax, dy = by - ay, d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d;
        const tipX = bx - ux * (rOf(e.b) + 3), tipY = by - uy * (rOf(e.b) + 3);
        ctx.globalAlpha = 1; ctx.fillStyle = colour; ctx.beginPath();
        ctx.moveTo(tipX, tipY); ctx.lineTo(tipX - ux * 9 - uy * 4.5, tipY - uy * 9 + ux * 4.5); ctx.lineTo(tipX - ux * 9 + uy * 4.5, tipY - uy * 9 - ux * 4.5); ctx.closePath(); ctx.fill();
        if (d > 70) label(e.l.predicate.replaceAll('_', ' '), (ax + bx) / 2, (ay + by) / 2 + 3.5, 10.5, '#5b5357', 1);
      }
      order.sort((u, v) => sn[u] - sn[v]);
      projected = [];
      ctx.lineWidth = 2;
      for (const i of order) {
        const n = nodes[i], x = sx[i], y = sy[i], r = rOf(i), on = nodesLit.has(n.id);
        let alpha = 0.5 + 0.5 * sn[i]; if (focusing && !on) alpha *= 0.18;
        ctx.globalAlpha = alpha; ctx.fillStyle = graphTypeColors[n.type] || '#94a3b8';
        ctx.beginPath();
        if (n.type === 'Document') ctx.rect(x - r, y - r, r * 2, r * 2); else ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill(); ctx.strokeStyle = '#ffffff'; ctx.stroke();
        if (n.id === selectedNode || n.id === hover) {
          ctx.globalAlpha = 1; ctx.strokeStyle = graphTypeColors[n.type] || '#94a3b8';
          ctx.beginPath(); ctx.arc(x, y, r + 5, 0, Math.PI * 2); ctx.stroke();
        }
        projected.push({ id: n.id, x, y, r });
      }
      // Labels go on last and never overlap: the hovered or selected entity first, then its lit
      // neighbours, then the most connected entities in the front half, each only where there is room.
      const taken = [];
      const place = (i, alpha, force) => {
        const s = sprite(shortLabel[i]), k = (11 + 2 * Math.min(1, Math.max(0, sp[i] - 0.6))) / SPRITE_PX;
        const w = s.w * k, h = s.h * k, y = sy[i] + rOf(i) + 14, x0 = sx[i] - w / 2, y0 = y - (SPRITE_PX + 2) * k;
        if (!force) for (const t of taken) if (x0 < t.x1 && x0 + w > t.x0 && y0 < t.y1 && y0 + h > t.y0) return;
        taken.push({ x0, y0, x1: x0 + w, y1: y0 + h });
        nodeLabel(shortLabel[i], sx[i], y, k * SPRITE_PX, alpha);
      };
      const focusIndex = index.get(hover || selectedNode);
      if (focusIndex !== undefined) place(focusIndex, 1, true);
      for (const i of byDegree) {
        if (i === focusIndex) continue;
        if (focusing) { if (nodesLit.has(nodes[i].id)) place(i, 1, false); }
        else if (sn[i] > 0.45) place(i, Math.min(1, 0.35 + sn[i]), false);
      }
      ctx.globalAlpha = 1;
    }
    function frame(t) {
      raf = 0;
      if (!cv.isConnected) return;
      const dt = last ? Math.min(50, t - last) : 16; last = t;
      if (cam.spin && !drag && !hover) cam.yaw += dt * 0.00015;
      draw();
      if (cam.spin) raf = requestAnimationFrame(frame); else last = 0;
    }
    const kick = () => { if (!raf) raf = requestAnimationFrame(frame); };
    const pick = (x, y) => { for (let i = projected.length - 1; i >= 0; i--) { const q = projected[i]; if (Math.hypot(q.x - x, q.y - y) <= q.r + 4) return q.id; } return null; };
    const local = (event) => { const r = cv.getBoundingClientRect(); return [event.clientX - r.left, event.clientY - r.top]; };
    cv.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      drag = { x: event.clientX, y: event.clientY, moved: 0 }; try { cv.setPointerCapture(event.pointerId); } catch { /* pointer already gone */ } cv.classList.add('dragging');
    });
    cv.addEventListener('pointermove', (event) => {
      if (drag) {
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        drag.x = event.clientX; drag.y = event.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
        if (drag.moved > 3) { cam.yaw += dx * 0.008; cam.pitch = Math.max(-1.45, Math.min(1.45, cam.pitch + dy * 0.008)); }
        kick(); return;
      }
      const id = pick(...local(event));
      if (id !== hover) { hover = id; cv.style.cursor = id ? 'pointer' : ''; kick(); }
    });
    cv.addEventListener('pointerleave', () => { if (!drag && hover) { hover = null; cv.style.cursor = ''; kick(); } });
    cv.addEventListener('pointerup', (event) => {
      if (!drag) return;
      const moved = drag.moved; drag = null; cv.classList.remove('dragging');
      if (moved >= 4) { kick(); return; }
      const id = pick(...local(event));
      if (id) { graphState.selected = { kind: 'node', id }; renderGraph(); }
      else if (graphState.selected) { graphState.selected = null; renderGraph(); }
    });
    cv.addEventListener('pointercancel', () => { drag = null; cv.classList.remove('dragging'); });
    cv.addEventListener('wheel', (event) => {
      event.preventDefault();
      const lines = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;  // step follows the wheel distance
      const step = Math.max(-0.4, Math.min(0.4, -lines * (event.ctrlKey ? 0.01 : 0.0011)));
      cam.zoom = Math.max(0.3, Math.min(5, cam.zoom * Math.exp(step))); kick();
    }, { passive: false });

    const controls = el('div', 'graph-zoom');
    for (const [text, aria, act] of [
      ['+', 'Zoom in', () => { cam.zoom = Math.min(5, cam.zoom * 1.25); kick(); }],
      ['−', 'Zoom out', () => { cam.zoom = Math.max(0.3, cam.zoom * 0.8); kick(); }],
      ['Fit', 'Fit the graph to the frame', () => { cam.zoom = 1; kick(); }],
      ['Reset', 'Lay the graph out again', () => {
        graphState.layout3d = { key, pos: graph3DLayout(nodes, links) }; pos = graphState.layout3d.pos;
        R = Math.max(60, ...[...pos.values()].map(radiusOf)); cam.zoom = 1; kick();
      }],
    ]) {
      const b = el('button', 'graph-zoom-button', text); b.type = 'button'; b.title = aria; b.setAttribute('aria-label', aria);
      b.addEventListener('click', act); controls.append(b);
    }
    const spin = el('button', 'graph-zoom-button', cam.spin ? 'Stop' : 'Spin');
    spin.type = 'button'; spin.title = 'Turn the slow rotation on or off'; spin.setAttribute('aria-pressed', String(cam.spin));
    spin.addEventListener('click', () => { cam.spin = !cam.spin; spin.textContent = cam.spin ? 'Stop' : 'Spin'; spin.setAttribute('aria-pressed', String(cam.spin)); kick(); });
    controls.append(spin);
    const keyBar = el('div', 'graph-key');
    keyBar.append(el('span', 'approved', 'Approved'), el('span', 'proposed', 'Proposed'), el('span', 'graph-key-hint', 'Drag to rotate · scroll to zoom · nearer is darker'));
    host.append(cv, controls, keyBar);

    const observer = new ResizeObserver(() => {
      const w = host.clientWidth, h = host.clientHeight; if (!w || !h) return;
      const dpr = window.devicePixelRatio || 1;
      size = { w, h }; cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.width = `${w}px`; cv.style.height = `${h}px`;
      kick();
    });
    observer.observe(host);
    graphState.observer = observer;
  }
  function graphModeSwitch() {
    const box = el('div', 'graph-mode'); box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'View');
    for (const [mode, text] of [['2d', '2D'], ['3d', '3D']]) {
      const b = el('button', 'graph-mode-button', text); b.type = 'button';
      b.setAttribute('aria-pressed', String(graphState.mode === mode));
      b.addEventListener('click', () => { if (graphState.mode !== mode) { graphState.mode = mode; renderGraph(); } });
      box.append(b);
    }
    return box;
  }
  function graphExplorer() {
    const byId = graphNodesById();
    const hidden = graphState.hiddenTypes;
    let links = visibleLinks().filter((l) => !hidden.has(byId.get(l.source)?.type) && !hidden.has(byId.get(l.target)?.type));
    const degree = new Map();
    links.forEach((l) => { degree.set(l.source, (degree.get(l.source) || 0) + 1); degree.set(l.target, (degree.get(l.target) || 0) + 1); });
    let ids = [...degree.keys()];
    const query = graphState.search.toLowerCase();
    let note = '';
    if (query) {
      const hits = new Set(graphState.data.nodes.filter((n) => n.label.toLowerCase().includes(query)).map((n) => n.id));
      links = links.filter((l) => hits.has(l.source) || hits.has(l.target));
      ids = [...new Set(links.flatMap((l) => [l.source, l.target]).concat([...hits].filter((id) => degree.has(id))))];
      note = `${hits.size} matching entities and their links`;
    }
    if (ids.length > 220) {
      ids = ids.sort((a, b) => (degree.get(b) || 0) - (degree.get(a) || 0)).slice(0, 220);
      const keep = new Set(ids); links = links.filter((l) => keep.has(l.source) && keep.has(l.target));
      note = 'Showing the 220 most connected entities. Search to focus on others.';
    }
    const nodes = ids.map((id) => byId.get(id)).filter(Boolean);
    const wrap = el('div', 'graph-split');
    const canvas = el('div', 'graph-canvas');
    const side = el('aside', 'graph-side');
    wrap.append(canvas, side);
    graphState.observer?.disconnect();
    canvas.append(graphModeSwitch());
    if (!nodes.length) { canvas.append(el('p', 'graph-empty', 'No links match this filter.')); graphSide(side); return wrap; }
    if (graphState.mode === '3d') {
      graph3D(canvas, nodes, links, degree);
      if (note) canvas.append(el('p', 'graph-note', note));
      graphSide(side);
      return wrap;
    }

    const ns = 'http://www.w3.org/2000/svg';
    const svgEl = (tag, attrs = {}, cls = '') => {
      const node = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
      if (cls) node.setAttribute('class', cls);
      return node;
    };
    const svg = svgEl('svg', { role: 'img', 'aria-label': `Knowledge graph with ${nodes.length} entities and ${links.length} links` }, 'graph-svg');
    const defs = svgEl('defs');
    for (const name of ['approved', 'proposed', 'rejected', 'lit']) {
      const marker = svgEl('marker', { id: `graph-arrow-${name}`, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 8, markerHeight: 8, markerUnits: 'userSpaceOnUse', orient: 'auto' }, `graph-arrow ${name}`);
      marker.append(svgEl('path', { d: 'M0,1.5 L9,5 L0,8.5 z' }));
      defs.append(marker);
    }
    // Everything sits in one `world` group, so panning moves a single transform. Fading is done on
    // whole layers and the lit items are copied into `focusLayer`, so hover never restyles every element.
    const world = svgEl('g', {}, 'graph-world');
    const edgeLayer = svgEl('g', {}, 'graph-dim'), labelLayer = svgEl('g', {}, 'graph-dim-labels'), nodeLayer = svgEl('g', {}, 'graph-dim'), focusLayer = svgEl('g', {}, 'graph-focus');
    // The world is its own compositor layer, so pan and zoom move a finished picture and repaint
    // nothing. Hover puts a veil over it and draws the lit copies above, leaving the world untouched.
    const veil = svgEl('rect', { x: 0, y: 0, width: '100%', height: '100%' }, 'graph-veil');
    world.append(edgeLayer, labelLayer, nodeLayer);
    svg.append(defs, world, veil, focusLayer);

    const radius = (id) => 7 + Math.min(12, Math.sqrt(degree.get(id) || 0) * 3);
    const neighbours = new Map(nodes.map((n) => [n.id, new Set([n.id])]));
    links.forEach((l) => { neighbours.get(l.source)?.add(l.target); neighbours.get(l.target)?.add(l.source); });
    const sel = graphState.selected;
    const selectedNode = sel?.kind === 'node' && neighbours.has(sel.id) ? sel.id : null;
    const selectedLink = sel?.kind === 'link' && links.some((l) => l.id === sel.id) ? sel.id : null;
    const select = (kind, id) => { graphState.selected = id ? { kind, id } : null; renderGraph(); };

    const edgeViews = links.map((l) => {
      const g = svgEl('g', {}, `graph-edge ${l.status}${l.id === selectedLink ? ' selected' : ''}`);
      const line = svgEl('path', {}, 'graph-edge-line'), hit = svgEl('path', {}, 'graph-edge-hit');
      const title = svgEl('title'); title.textContent = `${l.source_label} ${l.predicate.replaceAll('_', ' ')} ${l.target_label} (${l.status})`;
      g.append(title, line, hit);
      const label = svgEl('text', {}, `graph-edge-label${l.id === selectedLink ? ' selected' : ''}`);
      label.textContent = l.predicate.replaceAll('_', ' ');
      g.addEventListener('click', () => select('link', l.id));
      g.addEventListener('pointerenter', () => { if (!drag) focus(null, l.id); });
      g.addEventListener('pointerleave', () => { if (!drag) rest(); });
      edgeLayer.append(g); labelLayer.append(label);
      return { l, g, line, hit, label };
    });
    const nodeViews = nodes.map((n) => {
      const r = radius(n.id);
      const g = svgEl('g', {}, `graph-node${n.id === selectedNode ? ' selected' : ''}${(degree.get(n.id) || 0) < 2 && nodes.length > 60 ? ' minor' : ''}`);
      g.style.setProperty('--type', graphTypeColors[n.type] || '#94a3b8');
      const title = svgEl('title'); title.textContent = `${n.label} (${n.type})`;
      const halo = svgEl('circle', { r: r + 6 }, 'graph-node-halo');
      const shape = n.type === 'Document' ? svgEl('rect', { x: -r, y: -r, width: r * 2, height: r * 2, rx: 4 }, 'graph-node-shape') : svgEl('circle', { r }, 'graph-node-shape');
      const text = svgEl('text', { y: r + 16 }, 'graph-node-label'); text.textContent = graphShortLabel(n.label);
      g.append(title, halo, shape, text);
      g.addEventListener('pointerenter', () => { if (!drag) focus(n.id); });
      g.addEventListener('pointerleave', () => { if (!drag) rest(); });
      nodeLayer.append(g);
      return { n, g, r };
    });
    const nodeView = new Map(nodeViews.map((v) => [v.n.id, v]));
    const edgeView = new Map(edgeViews.map((v) => [v.l.id, v]));
    const edgesOf = new Map(nodes.map((n) => [n.id, []]));
    edgeViews.forEach((v) => {
      edgesOf.get(v.l.source)?.push(v); edgesOf.get(v.l.target)?.push(v);
      v.line.setAttribute('marker-end', `url(#graph-arrow-${v.l.id === selectedLink ? 'lit' : v.l.status})`);
    });

    // Hovering (or selecting) an entity keeps it and its neighbours lit and fades the rest: the two
    // base layers fade as a whole and copies of the lit items are drawn on top at full strength.
    let focused = null, wanted = null, focusFrame = 0;
    function applyFocus(id, linkId) {
      const litEdges = id ? [...(edgesOf.get(id) || [])] : [];
      const one = linkId ? edgeView.get(linkId) : null;
      if (one && !litEdges.includes(one)) litEdges.push(one);
      const lit = new Set(id && nodeView.has(id) ? [id] : []);
      litEdges.forEach((v) => { lit.add(v.l.source); lit.add(v.l.target); });
      focused = lit.size ? { id, linkId } : null;
      svg.classList.toggle('focusing', lit.size > 0);
      const copy = (node) => { const c = node.cloneNode(true); c.classList.add('lit'); return c; };
      focusLayer.replaceChildren(...litEdges.map((v) => copy(v.g)), ...litEdges.map((v) => copy(v.label)), ...[...lit].map((m) => copy(nodeView.get(m).g)));
    }
    function focus(id, linkId) {  // at most one change per frame, however many hover events arrive
      wanted = [id || null, linkId || null];
      if (!focusFrame) focusFrame = requestAnimationFrame(() => { focusFrame = 0; applyFocus(...wanted); });
    }
    const rest = () => focus(selectedNode, selectedLink);

    // Positions are in layout units; the view maps them to screen pixels, so nodes and labels keep
    // a readable size at any zoom. Layout and view are kept per graph, so selecting does not move it.
    const key = `${[...ids].sort().join(',')}#${links.map((l) => l.id).sort().join(',')}`;
    let pos = graphState.layout?.key === key ? graphState.layout.pos : null;
    let view = graphState.view?.key === key ? graphState.view : null;
    let size = { w: 0, h: 0 };
    let drag = null;
    function fit() {
      // Labels and nodes keep their pixel size, so find the largest zoom at which the centres plus
      // those fixed margins still fit the frame (binary search: the span grows with the zoom).
      const items = nodes.map((n) => ({ p: pos.get(n.id), w: (labelled(n) ? graphLabelWidth(n.label) : 34) / 2 }));
      const span = (k) => {
        let l = Infinity, r = -Infinity, t = Infinity, b = -Infinity;
        for (const { p, w } of items) { l = Math.min(l, p.x * k - w); r = Math.max(r, p.x * k + w); t = Math.min(t, p.y * k - 26); b = Math.max(b, p.y * k + 40); }
        return { l, r, t, b };
      };
      const pad = 28;
      let lo = 0.02, hi = 1.3;
      for (let i = 0; i < 32; i++) {
        const k = (lo + hi) / 2, e = span(k);
        if (e.r - e.l <= size.w - pad * 2 && e.b - e.t <= size.h - pad * 2) lo = k; else hi = k;
      }
      const e = span(lo);
      view.k = lo; view.tx = (size.w - (e.r - e.l)) / 2 - e.l; view.ty = (size.h - (e.b - e.t)) / 2 - e.t;
    }
    const labelled = (n) => !((degree.get(n.id) || 0) < 2 && nodes.length > 60);
    const widthOf = (id) => { const n = byId.get(id); return labelled(n) ? graphLabelWidth(n.label) : 34; };
    const relayout = () => { pos = graphLayout(nodes, links, size.w, size.h, widthOf); graphState.layout = { key, pos }; fit(); };
    // Geometry is in zoomed units inside `world`; the pan is the world's own transform. A pan
    // therefore touches one attribute, and moving one entity touches it and its own links only.
    let showLabels = false, nodeLabels = null;
    const place = (v) => {
      const p = pos.get(v.n.id); v.x = p.x * view.k; v.y = p.y * view.k;
      v.g.setAttribute('transform', `translate(${v.x.toFixed(1)},${v.y.toFixed(1)})`);
    };
    const route = (v) => {
      const a = nodeView.get(v.l.source), b = nodeView.get(v.l.target);
      const x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
      const bend = Math.min(36, len * 0.1);
      const cx = (x1 + x2) / 2 - (dy / len) * bend, cy = (y1 + y2) / 2 + (dx / len) * bend;
      const rs = a.r + 2, rt = b.r + 3;
      const t0 = Math.hypot(cx - x1, cy - y1) || 1, t1 = Math.hypot(x2 - cx, y2 - cy) || 1;
      const sx = x1 + ((cx - x1) / t0) * rs, sy = y1 + ((cy - y1) / t0) * rs;
      const ex = x2 - ((x2 - cx) / t1) * rt, ey = y2 - ((y2 - cy) / t1) * rt;
      const d = `M${sx.toFixed(1)},${sy.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
      v.line.setAttribute('d', d); v.hit.setAttribute('d', d);
      v.label.setAttribute('x', (0.25 * x1 + 0.5 * cx + 0.25 * x2).toFixed(1));
      v.label.setAttribute('y', (0.25 * y1 + 0.5 * cy + 0.25 * y2 + 3.5).toFixed(1));
      const shown = showLabels && len > 90;
      if (v.shown !== shown) { v.shown = shown; v.label.classList.toggle('shown', shown); }
    };
    // Pan moves the world layer. While a zoom gesture is running, the finished picture of the whole
    // graph is scaled as one layer (no repaint); the geometry is laid out again at the new zoom once
    // the gesture pauses, which puts nodes and labels back to pixel size and makes them sharp.
    let drawnK = 1, settle = 0, shownX = 0, shownY = 0;
    const pan = () => {
      shownX = view.tx; shownY = view.ty;
      const t = `translate(${view.tx.toFixed(1)}px,${view.ty.toFixed(1)}px)`;
      world.style.transform = t; focusLayer.style.transform = t;
    };
    const scaleView = () => {
      const s = view.k / drawnK;
      svg.style.transform = `translate(${(view.tx - s * shownX).toFixed(1)}px,${(view.ty - s * shownY).toFixed(1)}px) scale(${s.toFixed(4)})`;
    };
    function draw() {
      drawnK = view.k; window.clearTimeout(settle); svg.style.transform = '';
      pan();
      showLabels = links.length <= 80 || view.k >= 1.2;
      const labels = view.k >= 1.3;
      if (labels !== nodeLabels) { nodeLabels = labels; nodeViews.forEach((v) => v.g.classList.toggle('show-label', labels)); }
      nodeViews.forEach(place); edgeViews.forEach(route);
      if (focused) applyFocus(focused.id, focused.linkId);  // the lit copies follow the new geometry
    }
    // Pointer and wheel events can arrive several times per frame; do the work once per frame.
    let frame = 0, need = null;
    function schedule(what) {
      need = need === null || need === what ? what : 'all';
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0; const job = need; need = null;
        if (job === 'zoom') scaleView();
        else if (job === 'pan' && view.k === drawnK) pan();
        else if (job === 'all' || view.k !== drawnK) draw();
        else { place(job); edgesOf.get(job.n.id).forEach(route); if (focused) applyFocus(focused.id, focused.linkId); }
      });
    }
    function zoomAt(mx, my, factor) {
      if (!view) return;  // not laid out yet
      const k = Math.max(0.15, Math.min(4, view.k * factor));
      view.tx = mx - ((mx - view.tx) * k) / view.k; view.ty = my - ((my - view.ty) * k) / view.k; view.k = k;
      schedule('zoom');
      window.clearTimeout(settle); settle = window.setTimeout(() => schedule('all'), 140);
    }
    // The zoom step follows how far the wheel moved, so a mouse notch is a clear step and a trackpad,
    // which sends many small events, zooms smoothly. The frame position is read once per gesture.
    let wheelBox = null, wheelBoxTimer = 0;
    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      if (!wheelBox) { const box = canvas.getBoundingClientRect(); wheelBox = { left: box.left + canvas.clientLeft, top: box.top + canvas.clientTop }; }
      window.clearTimeout(wheelBoxTimer); wheelBoxTimer = window.setTimeout(() => { wheelBox = null; }, 300);
      const lines = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      const step = Math.max(-0.4, Math.min(0.4, -lines * (event.ctrlKey ? 0.01 : 0.0014)));
      zoomAt(event.clientX - wheelBox.left, event.clientY - wheelBox.top, Math.exp(step));
    }, { passive: false });
    // Drag an entity to move it, drag the background to pan. A press without movement selects.
    svg.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.target.closest('.graph-edge')) return;
      const g = event.target.closest('.graph-node');
      drag = { x: event.clientX, y: event.clientY, moved: 0, node: g ? nodeViews.find((v) => v.g === g) : null };
      try { svg.setPointerCapture(event.pointerId); } catch { /* pointer already gone */ }
      svg.classList.add('dragging');
    });
    svg.addEventListener('pointermove', (event) => {
      if (!drag || !view) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      drag.x = event.clientX; drag.y = event.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
      if (drag.node) { const p = pos.get(drag.node.n.id); p.x += dx / view.k; p.y += dy / view.k; schedule(drag.node); }
      else { view.tx += dx; view.ty += dy; schedule('pan'); }
    });
    const endDrag = () => {
      if (!drag) return;
      const { node, moved } = drag; drag = null; svg.classList.remove('dragging');
      if (moved < 4) {
        if (node) select('node', node.n.id);
        else if (graphState.selected) select(null, null);
      }
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', () => { drag = null; svg.classList.remove('dragging'); });

    const controls = el('div', 'graph-zoom');
    for (const [label, aria, act] of [
      ['+', 'Zoom in', () => zoomAt(size.w / 2, size.h / 2, 1.25)],
      ['−', 'Zoom out', () => zoomAt(size.w / 2, size.h / 2, 0.8)],
      ['Fit', 'Fit the graph to the frame', () => { fit(); draw(); }],
      ['Reset', 'Lay the graph out again', () => { relayout(); draw(); }],
    ]) {
      const b = el('button', 'graph-zoom-button', label); b.type = 'button'; b.title = aria; b.setAttribute('aria-label', aria);
      b.addEventListener('click', act); controls.append(b);
    }
    const keyBar = el('div', 'graph-key');
    keyBar.append(el('span', 'approved', 'Approved'), el('span', 'proposed', 'Proposed'));
    if (graphState.status === 'rejected') keyBar.append(el('span', 'rejected', 'Rejected'));
    keyBar.append(el('span', 'graph-key-hint', 'Scroll to zoom · drag to move'));
    canvas.append(svg, controls, keyBar);
    if (note) canvas.append(el('p', 'graph-note', note));

    const observer = new ResizeObserver(() => {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (!w || !h) return;
      const first = !size.w;
      size = { w, h }; svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
      let fresh = false;
      if (!view) { view = { key, k: 1, tx: 0, ty: 0 }; graphState.view = view; fresh = true; }
      if (!pos) relayout();
      else if (fresh) fit();
      draw();
      if (first) applyFocus(selectedNode, selectedLink);
    });
    observer.observe(canvas);
    graphState.observer = observer;
    graphSide(side);
    return wrap;
  }
  function graphSide(side) {
    const data = graphState.data, byId = graphNodesById(), sel = graphState.selected;
    if (sel?.kind === 'node' && byId.has(sel.id)) {
      const node = byId.get(sel.id);
      side.append(el('h3', '', node.label));
      const typeRow = el('div', 'graph-type-row');
      typeRow.append(el('span', 'graph-swatch'), el('span', '', node.type), el('small', '', node.type_source === 'human' ? 'set by a reviewer' : node.type_source === 'rule' ? 'source document' : 'set by the model'));
      typeRow.firstChild.style.setProperty('--type', graphTypeColors[node.type] || '#94a3b8');
      side.append(typeRow);
      if (data.can_review && node.type !== 'Document') {
        const select = el('select'); select.setAttribute('aria-label', 'Change entity type');
        Object.keys(graphState.ontology.types).forEach((t) => option(select, t, t)); select.value = node.type;
        select.addEventListener('change', async () => {
          await api(`/api/graph/nodes/${encodeURIComponent(node.id)}`, { method: 'PATCH', body: { node_type: select.value } }).catch(alertError);
          await loadGraph();
        });
        const lbl = el('label', 'graph-retype', 'Correct the type '); lbl.append(select); side.append(lbl);
      }
      const links = data.links.filter((l) => l.source === node.id || l.target === node.id);
      side.append(el('h4', '', `${links.length} links`));
      const list = el('ul', 'graph-link-list');
      for (const l of links) {
        const item = el('li'); const b = button('', 'graph-link-row');
        b.append(el('span', `graph-status ${l.status}`, l.status), el('span', '', l.source === node.id ? `${l.predicate.replaceAll('_', ' ')} → ${l.target_label}` : `${l.source_label} → ${l.predicate.replaceAll('_', ' ')}`));
        b.addEventListener('click', () => { graphState.selected = { kind: 'link', id: l.id }; renderGraph(); });
        item.append(b); list.append(item);
      }
      side.append(list);
      return;
    }
    if (sel?.kind === 'link') {
      const link = data.links.find((l) => l.id === sel.id);
      if (link) { side.append(graphLinkCard(link, true)); return; }
    }
    side.append(el('h3', '', 'Ontology'), el('p', 'muted small-text', `Version ${graphState.ontology.version}. Click an entity or a link for details, or a type to hide or show it.`));
    const counts = new Map(); data.nodes.forEach((n) => counts.set(n.type, (counts.get(n.type) || 0) + 1));
    const legend = el('ul', 'graph-legend');
    for (const [type, description] of Object.entries({ Document: 'A source document', ...graphState.ontology.types })) {
      const count = counts.get(type) || 0;
      const li = el('li');
      const item = button('', `graph-legend-item${count ? '' : ' none'}`); item.title = description;
      item.setAttribute('aria-pressed', String(!graphState.hiddenTypes.has(type)));
      const sw = el('span', 'graph-swatch'); sw.style.setProperty('--type', graphTypeColors[type] || '#94a3b8');
      item.append(sw, el('span', '', type), el('small', '', String(count)));
      item.addEventListener('click', () => {
        if (graphState.hiddenTypes.has(type)) graphState.hiddenTypes.delete(type); else graphState.hiddenTypes.add(type);
        renderGraph();
      });
      li.append(item); legend.append(li);
    }
    side.append(legend);
  }
  function graphLinkCard(link, detailed) {
    const card = el('section', `graph-link-card ${link.status}`);
    const head = el('div', 'graph-link-head');
    head.append(el('strong', '', link.source_label), el('span', 'graph-predicate', link.predicate.replaceAll('_', ' ')), el('strong', '', link.target_label));
    card.append(head);
    const meta = el('p', 'graph-link-meta');
    meta.append(el('span', `graph-status ${link.status}`, link.status), document.createTextNode(` ${link.confidence.toLowerCase() || 'unrated'} · score ${link.score} · found as "${link.relation.replaceAll('_', ' ')}"`));
    if (link.reviewed_by) meta.append(document.createTextNode(` · reviewed by ${link.reviewed_by}`));
    card.append(meta);
    const evidence = el('div', 'graph-evidence'); evidence.append(el('p', 'muted small-text', 'Loading evidence…'));
    card.append(evidence);
    api(`/api/graph/links/${encodeURIComponent(link.id)}`).then((data) => {
      evidence.replaceChildren(...(data.evidence.length ? data.evidence.map((e) => {
        const q = el('blockquote', ''); q.append(el('p', '', e.quote), el('cite', '', `${e.title}${e.page ? `, page ${e.page}` : ''}`)); return q;
      }) : [el('p', 'muted small-text', 'No evidence stored.')]));
    }).catch((error) => evidence.replaceChildren(el('p', 'inline-error', error.message)));
    if (graphState.data.can_review) {
      const actions = el('div', 'graph-review-actions');
      const choices = link.status === 'proposed' ? [['approve', 'Approve', 'button primary'], ['reject', 'Reject', 'button secondary']]
        : [['reopen', 'Back to proposed', 'button secondary']];
      for (const [decision, label, cls] of choices) {
        const b = button(label, cls);
        b.addEventListener('click', async () => {
          b.disabled = true;
          try { await api(`/api/graph/links/${encodeURIComponent(link.id)}/review`, { method: 'POST', body: { decision } }); await loadGraph(); }
          catch (error) { alertError(error); b.disabled = false; }
        });
        actions.append(b);
      }
      card.append(actions);
    }
    return card;
  }
  function graphReviewQueue() {
    const links = graphState.data.links.filter((l) => l.status === 'proposed').sort((a, b) => b.score - a.score);
    const box = el('div', 'graph-review');
    if (!links.length) { empty(box, 'Nothing to review', 'Every link in this folder has been reviewed.', 'check'); return box; }
    if (graphState.data.can_review) {
      const strong = links.filter((l) => l.confidence === 'EXTRACTED' && l.score >= 0.9);
      const bar = el('div', 'graph-review-bar');
      bar.append(el('span', 'muted small-text', `${links.length} proposed links, highest score first.`));
      if (strong.length) {
        const bulk = button(`Approve ${strong.length} stated links (score 0.9 or more)`, 'button secondary');
        bulk.addEventListener('click', async () => {
          bulk.disabled = true;
          try {
            await api('/api/graph/review', { method: 'POST', body: { folder_id: graphState.folderId, link_ids: strong.map((l) => l.id), decision: 'approve', note: 'bulk: extracted, score >= 0.9' } });
            notify(`Approved ${strong.length} links.`); await loadGraph();
          } catch (error) { alertError(error); bulk.disabled = false; }
        });
        bar.append(bulk);
      }
      box.append(bar);
    }
    links.slice(0, 60).forEach((l) => box.append(graphLinkCard(l, false)));
    if (links.length > 60) box.append(el('p', 'muted small-text', `Showing the first 60 of ${links.length}. Review these to see more.`));
    return box;
  }
  // Main entities: what each document is mainly about, from the entity scan (local GLiNER2.5). A second
  // pass of the same model checks each entity's type; only confirmed entities can be main entities. A
  // main entity whose type the document's kind is not expected to be about is flagged, never dropped.
  const entityState = { folderId: null, data: null, poll: null, flaggedOnly: false };
  const percent = (p) => `${Math.round((p || 0) * 100)}%`;
  async function loadEntities() {
    const epoch = state.epoch, folderId = graphState.folderId;
    const data = await api(`/api/entities?folder_id=${encodeURIComponent(folderId)}`);
    if (epoch !== state.epoch || state.view !== 'graph' || graphState.tab !== 'entities' || graphState.folderId !== folderId) return;
    entityState.folderId = folderId; entityState.data = data;
    renderGraph();
    window.clearTimeout(entityState.poll);
    if (data.scans.some((s) => ['queued', 'running'].includes(s.status))) {
      entityState.poll = window.setTimeout(() => { if (state.view === 'graph' && graphState.tab === 'entities') loadEntities().catch(() => {}); }, 4000);
    }
  }
  function entityScanLine(data) {
    const scan = data.scans[0];
    const line = el('p', 'graph-build-line');
    if (!scan) { line.textContent = 'No entity scan yet. GLiNER2.5 runs on this machine; no document text is sent out.'; return line; }
    const when = formatDate(scan.finished_at || scan.created_at, true);
    if (['queued', 'running'].includes(scan.status)) line.append(el('span', 'graph-spinner'), document.createTextNode(` Scanning: ${scan.stage}`));
    else if (scan.status === 'failed') { line.classList.add('failed'); line.textContent = `Last scan failed (${when}): ${scan.error}`; }
    else {
      const c = scan.counts || {};
      line.textContent = `Last scan ${when} · ${c.documents} documents · ${c.entities} entities, ${c.confirmed} confirmed and ${c.disputed} disputed by the classifier · ${c.main_entities} main entities, ${c.unexpected} flagged · ${scan.model} · ontology ${scan.ontology_version}`;
      if ((c.notes || []).length) line.title = c.notes.join('\n');
    }
    return line;
  }
  function entityChip(m, kindLabel) {
    const chip = el('span', `entity-chip${m.fit === 'unexpected' ? ' unexpected' : ''}`);
    const dot = el('i'); dot.style.setProperty('--type', graphTypeColors[m.type] || '#94a3b8');
    chip.append(dot, document.createTextNode(m.label));
    chip.title = `${m.type} · in ${m.chunks} chunks · classifier agrees ${percent(m.agreement)}`
      + (m.fit === 'unexpected' ? ` · a ${kindLabel || 'document of this kind'} is not expected to be about a ${m.type}` : '');
    return chip;
  }
  function entityPanel() {
    const box = el('div', 'entity-panel');
    const data = entityState.folderId === graphState.folderId ? entityState.data : null;
    if (!data) { box.append(el('p', 'muted', 'Loading main entities…')); loadEntities().catch(alertError); return box; }
    const running = data.scans.some((s) => ['queued', 'running'].includes(s.status));
    const bar = el('div', 'graph-review-bar');
    bar.append(entityScanLine(data));
    if (data.can_scan) {
      const scan = button(data.scans.length ? 'Scan again' : 'Scan entities', 'button secondary', 'refresh');
      scan.disabled = running;
      scan.addEventListener('click', async () => {
        scan.disabled = true;
        try {
          await api('/api/entities/scans', { method: 'POST', body: { folder_id: graphState.folderId } });
          notify('Entity scan started. Only new or changed chunks are read again.'); await loadEntities();
        } catch (error) { alertError(error); scan.disabled = false; }
      });
      bar.append(scan);
    }
    box.append(bar);
    const scanned = data.documents.filter((d) => d.scanned);
    if (!scanned.length) {
      const hint = el('div');
      empty(hint, 'No entity scan yet', data.can_scan ? 'Choose Scan entities to find what each document is mainly about.' : 'A folder owner or checker can start a scan.', 'library');
      box.append(hint); return box;
    }
    const flagged = scanned.filter((d) => d.unexpected).length;
    const filter = el('label', 'entity-filter');
    const only = el('input'); only.type = 'checkbox'; only.checked = entityState.flaggedOnly;
    only.addEventListener('change', () => { entityState.flaggedOnly = only.checked; renderGraph(); });
    filter.append(only, document.createTextNode(` Only documents with a flagged main entity (${flagged})`));
    box.append(filter);
    const table = el('table', 'entity-table');
    const head = el('tr');
    ['Document', 'Kind', 'Main entities', 'Type check'].forEach((t) => head.append(el('th', '', t)));
    const thead = el('thead'); thead.append(head);
    const body = el('tbody');
    for (const doc of scanned.filter((d) => !entityState.flaggedOnly || d.unexpected)) {
      const row = el('tr');
      const title = button(doc.title, 'entity-title');
      title.addEventListener('click', () => openEntityDocument(doc.id).catch(alertError));
      const titleCell = el('td'); titleCell.append(title);
      const kind = el('td', 'entity-kind', doc.kind_label || 'unknown');
      kind.append(el('small', '', ` ${percent(doc.kind_confidence)}`));
      const chips = el('td'); const list = el('div', 'entity-chips');
      doc.main.forEach((m) => list.append(entityChip(m, doc.kind_label)));
      if (!doc.main.length) list.append(el('span', 'muted small-text', 'No confirmed entity in two or more chunks'));
      chips.append(list);
      const counts = el('td', 'entity-counts', `${doc.confirmed} confirmed`);
      if (doc.disputed) counts.append(el('span', 'entity-disputed', ` · ${doc.disputed} disputed`));
      row.append(titleCell, kind, chips, counts);
      body.append(row);
    }
    table.append(thead, body);
    const scroll = el('div', 'entity-scroll'); scroll.append(table);
    box.append(scroll, el('p', 'muted small-text', 'Dashed red: a main entity of a type this kind of document is not expected to be about. The kinds and their expected types are a draft in the ontology file. Choose a document to see every entity and the classifier\'s view of it.'));
    return box;
  }
  async function openEntityDocument(documentId) {
    const data = await api(`/api/entities/documents/${encodeURIComponent(documentId)}`);
    const body = openDialog(data.title, 'MAIN ENTITIES');
    const kinds = data.kind_scores.map((k) => `${k.kind} ${percent(k.p)}`).join(' · ');
    body.append(el('p', 'muted', `Kind: ${data.kind_label || 'unknown'} (${kinds}). ${data.total} entities; showing the ${data.entities.length} in the most chunks.`));
    const table = el('table', 'entity-table compact');
    const head = el('tr');
    ['Entity', 'Type', 'Chunks', 'Classifier', 'Main'].forEach((t) => head.append(el('th', '', t)));
    const thead = el('thead'); thead.append(head);
    const rows = el('tbody');
    for (const e of data.entities) {
      const row = el('tr');
      const name = el('td'); name.append(entityChip({ ...e, fit: e.fit }, data.kind_label));
      const check = el('td', e.check === 'confirmed' ? 'entity-ok' : 'entity-disputed',
        e.check === 'confirmed' ? `agrees ${percent(e.agreement)}` : `disputed: reads it as ${e.hint === 'not an entity' ? 'not an entity' : `a ${e.hint}`} (${percent(e.agreement)} for ${e.type})`);
      row.append(name, el('td', '', e.type), el('td', 'num', String(e.chunks)), check,
        el('td', '', e.main ? (e.fit === 'unexpected' ? 'yes, flagged' : 'yes') : ''));
      rows.append(row);
    }
    table.append(thead, rows);
    const scroll = el('div', 'entity-scroll tall'); scroll.append(table);
    body.append(scroll);
  }
  // Processing settings: the time limits parsing runs under. Everyone can read them; only
  // developers (KB_DEVELOPERS on the server) can change them. Saved values apply from the next run.
  async function loadSettings() {
    const host = $('settings-content');
    const epoch = state.epoch;
    host.replaceChildren(el('p', 'muted', 'Loading settings…'));
    let data;
    try { data = await api('/api/settings/processing-limits'); }
    catch (error) { if (epoch === state.epoch) host.replaceChildren(el('p', 'inline-error', error.message)); return; }
    if (epoch === state.epoch && state.view === 'settings') renderSettings(data);
  }
  function renderSettings(data) {
    const host = $('settings-content');
    const note = el('p', 'settings-note', data.can_edit
      ? 'Changes apply from the next document that starts processing. Approvals stay valid, and each run records the limits it used.'
      : 'Only developers can change these limits. These are the values documents are processed with now.');
    const form = el('form', 'content-card settings-card');
    const head = el('div', 'card-heading');
    head.append(el('h2', '', 'Time limits'), el('span', 'small-text muted', `${data.limits.length} settings`));
    form.append(head);
    const entries = new Map();
    for (const limit of data.limits) {
      const id = `limit-${limit.key}`;
      const row = el('div', 'settings-row');
      const copy = el('div', 'settings-copy');
      const label = el('label', '', limit.label); label.htmlFor = id;
      const help = el('p', 'field-help', limit.help); help.id = `${id}-help`;
      const fallback = limit.environment ?? limit.default;
      const source = el('p', 'settings-source', limit.source === 'saved'
        ? `Saved by ${limit.updated_by} on ${formatDate(limit.updated_at, true)}`
        : limit.source === 'environment' ? `From the server environment (${limit.key})` : 'Default value');
      copy.append(label, help, source);
      const side = el('div', 'settings-input');
      const control = el('div', 'settings-control');
      const input = el('input');
      Object.assign(input, { type: 'number', id, min: limit.minimum, max: limit.maximum, step: 1, required: true, value: limit.value, disabled: !data.can_edit });
      input.inputMode = 'numeric';
      const range = el('span', 'settings-range', `${limit.minimum} to ${limit.maximum} s · default ${limit.default}`);
      range.id = `${id}-range`;
      input.setAttribute('aria-describedby', `${id}-help ${id}-range`);
      control.append(input, el('span', 'settings-unit', 'seconds'));
      side.append(control, range);
      const entry = { input, limit, reset: false };
      if (data.can_edit && limit.source === 'saved') {
        const reset = button(limit.environment ? 'Use environment value' : 'Use default', 'mini-button');
        reset.addEventListener('click', () => { entry.reset = true; input.value = fallback; status.textContent = `${limit.label} will go back to ${fallback} s when you save.`; });
        side.append(reset);
      }
      input.addEventListener('input', () => { entry.reset = false; });
      row.append(copy, side);
      form.append(row);
      entries.set(limit.key, entry);
    }
    const status = el('span', 'small-text muted'); status.setAttribute('role', 'status');
    if (data.can_edit) {
      const actions = el('div', 'settings-actions');
      const save = button('Save changes', 'button primary'); save.type = 'submit';
      actions.append(status, save);
      form.append(actions);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        const values = {};
        for (const [key, { input, limit, reset }] of entries) {
          if (reset) values[key] = null;
          else if (Number(input.value) !== limit.value) values[key] = Number(input.value);
        }
        if (!Object.keys(values).length) { status.textContent = 'Nothing has changed.'; return; }
        save.disabled = true;
        status.textContent = 'Saving…';
        try {
          const result = await api('/api/settings/processing-limits', { method: 'PUT', body: { values } });
          renderSettings(result);
          notify(result.changed === 1 ? 'Saved 1 change.' : `Saved ${result.changed} changes.`);
        } catch (error) {
          status.textContent = error.message;
          save.disabled = false;
        }
      });
    }
    host.replaceChildren(note, form);
  }
  async function loadHealth() {
    const epoch = state.epoch;
    try {
      const health = await api('/health');
      if (epoch !== state.epoch || !state.user) return;
      state.health = health;
      if (health.maintenance) {
        $('mode-badge').textContent = 'Maintenance';
        $('mode-title').textContent = 'Ingestion maintenance';
        $('mode-description').textContent = 'Search is paused. Open Ingestion monitor to follow processing steps and errors.';
        return;
      }
      setMode(health.model === 'openrouter' && health.external_models_enabled ? 'external-inference' : /local|extractive/.test(String(health.model)) ? 'local-extractive-demo' : health.mode);
      if (health.status && !['ok', 'healthy', 'ready', 'degraded'].includes(health.status)) $('mode-badge').title = `Service status: ${health.status}`;
    } catch {
      if (epoch !== state.epoch) return;
      $('mode-badge').textContent = 'Service status unavailable';
      $('mode-title').textContent = 'Service status unavailable';
      $('mode-description').textContent = 'We could not confirm the inference mode. Check with your workspace administrator.';
    }
  }
  function modeInfo(mode) {
    const value = String(mode || '').toLowerCase();
    if (/local|extractive|demo|offline|fallback/.test(value)) return { label: 'Local extractive demo', className: 'demo', description: 'Local retrieval with extractive answers. No external inference is used for this response mode.' };
    if (/external|openrouter|remote|llm|inference/.test(value)) return { label: 'External inference', className: 'external', description: 'Authorised retrieved context is processed by the configured external inference provider. Review citations before use.' };
    return { label: 'Inference mode unconfirmed', className: '', description: 'The service has not confirmed whether answers use local extraction or external inference.' };
  }
  function setMode(mode) {
    const info = modeInfo(mode);
    $('mode-badge').textContent = info.label;
    $('mode-badge').className = `mode-badge ${info.className}`;
    $('mode-title').textContent = info.label;
    $('mode-description').textContent = info.description;
    $('mode-badge').title = info.description;
  }

  // Recent questions are kept in this browser only, per employee, and shown on the empty Ask screen.
  function recentKey() { return `kb.recent.${state.user?.id || 'anon'}`; }
  function readRecent() {
    try { const list = JSON.parse(localStorage.getItem(recentKey()) || '[]'); return Array.isArray(list) ? list.filter((q) => typeof q === 'string').slice(0, 5) : []; } catch { return []; }
  }
  function rememberQuestion(message) {
    const list = [message, ...readRecent().filter((q) => q !== message)].slice(0, 5);
    try { localStorage.setItem(recentKey(), JSON.stringify(list)); } catch { /* storage may be blocked */ }
  }
  function renderRecent() {
    const list = readRecent();
    $('recent-questions').hidden = !list.length;
    $('recent-list').replaceChildren(...list.map((question) => {
      const item = el('button', 'recent-item', question);
      item.type = 'button';
      item.addEventListener('click', () => { if (!state.chatBusy) { $('chat-input').value = question; $('chat-input').focus(); } });
      return item;
    }));
  }
  function resetConversation() {
    if (state.chatBusy) return;
    renderRecent();
    state.chatGeneration++;
    const welcome = $('chat-welcome');
    $('chat-messages').replaceChildren(welcome);
    welcome.hidden = false;
    $('chat-input').value = '';
    $('chat-input').style.height = '';
    $('chat-input').disabled = false;
    $('send-button').disabled = false;
    $('new-conversation').disabled = false;
  }
  function addMessage(kind, content) {
    const article = el('article', `chat-message ${kind}`);
    const header = el('div', 'message-header');
    const avatar = el('span', `message-avatar ${kind === 'user' ? 'employee-avatar' : ''}`);
    avatar.append(kind === 'user' ? document.createTextNode(initials(state.user?.name)) : icon('chat'));
    const time = el('time', '', new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date()));
    time.dateTime = new Date().toISOString();
    header.append(avatar, document.createTextNode(kind === 'user' ? 'You' : 'KBase'), time);
    article.append(header);
    if (content !== undefined) article.append(el('div', 'message-text', content));
    $('chat-messages').append(article);
    return article;
  }
  function scrollChat() { const box = $('chat-messages'); box.scrollTop = box.scrollHeight; }
  function scrollChatTo(node) {
    const box = $('chat-messages');
    box.scrollTop += node.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
  }
  function appendAnswer(node, answer, citations, prefix) {
    const content = el('div', 'message-text');
    // Citation links are built from trusted DOM nodes; model text is never HTML.
    const source = text(answer, 'No answer was returned. Please try a more specific question.').replace(/\n{3,}/g, '\n\n');
    const matcher = /\[([^\]\n]{1,160})\]/g;
    let start = 0;
    let match;
    while ((match = matcher.exec(source)) !== null) {
      content.append(document.createTextNode(source.slice(start, match.index)));
      const index = /^\d+$/.test(match[1]) ? Number(match[1]) - 1 : citations.findIndex((citation) => sameId(citation.id, match[1]) || sameId(citation.document_id, match[1]));
      if (index >= 0 && index < citations.length) {
        const link = el('a', '', `[${index + 1}]`);
        link.href = `#${prefix}-citation-${index}`;
        link.setAttribute('aria-label', `Source ${index + 1}: ${citations[index].title || 'Document'}`);
        link.addEventListener('click', (event) => {
          event.preventDefault();
          const card = $(`${prefix}-citation-${index}`);
          if (card) scrollChatTo(card);
          card?.focus({ preventScroll: true });
        });
        content.append(link);
      } else content.append(document.createTextNode(match[0]));
      start = matcher.lastIndex;
    }
    content.append(document.createTextNode(source.slice(start)));
    node.append(content);
  }
  function appendCitations(node, citations, prefix) {
    if (!citations.length) return;
    const section = el('section', 'citations');
    section.setAttribute('aria-label', 'Supporting sources');
    const heading = el('h3', 'citations-heading');
    const documents = new Set(citations.map(citation => citation.document_id).filter(Boolean));
    const coverage = documents.size ? ` across ${documents.size} document${documents.size === 1 ? '' : 's'}` : '';
    heading.append(icon('book'), document.createTextNode(`${citations.length} cited passage${citations.length === 1 ? '' : 's'}${coverage}`));
    section.append(heading);
    citations.forEach((citation, index) => {
      const card = el('article', 'citation-card');
      card.id = `${prefix}-citation-${index}`;
      card.tabIndex = -1;
      const title = el('div', 'citation-title');
      title.append(el('span', 'citation-index', index + 1), el('span', '', citation.title || citation.document_id || 'Supporting document'));
      const meta = [citation.section, citation.page !== null && citation.page !== undefined ? `Page ${citation.page}` : null].filter(Boolean).join(' · ');
      card.append(title);
      if (meta) card.append(el('div', 'citation-meta', meta));
      card.append(el('p', 'citation-snippet', citation.snippet || 'No supporting snippet was returned for this source.'));
      section.append(card);
    });
    node.append(section);
  }
  function appendTimings(node, result) {
    if (!Array.isArray(result.timings)) return;
    const samples = [...result.timings];
    for (const step of result.trace || []) {
      if (step.tool === 'searchdocuments') samples.push(...(step.result?.timings || []));
      if (step.tool === 'plan_questions') samples.push({stage: 'planning', duration_ms: step.duration_ms});
    }
    const labels = {
      chat_total: 'Total server response',
      planning: 'Question planning',
      retrieval_total: 'Retrieval total',
      embedding_api: 'Embedding API',
      opensearch_mapping: 'OpenSearch mapping check',
      opensearch_bm25: 'OpenSearch keyword search',
      opensearch_knn: 'OpenSearch vector search',
      generation_api: 'Answer generation API',
      grounding_verification: 'Grounding verification'
    };
    const details = el('details', 'trace-details');
    details.append(el('summary', '', 'Performance breakdown'));
    details.append(el('p', 'muted', 'Times include retries. Stage totals sum all questions; concurrent and nested stages overlap, so they do not add up to response time.'));
    const table = el('table');
    const header = table.createTHead().insertRow();
    for (const label of ['Stage', 'Calls', 'Time (s)']) {
      const cell = el('th', '', label); cell.scope = 'col'; header.append(cell);
    }
    const body = table.createTBody();
    for (const [stage, label] of Object.entries(labels)) {
      const matching = samples.filter(sample => sample.stage === stage);
      if (!matching.length) continue;
      const row = body.insertRow();
      row.insertCell().textContent = label;
      row.insertCell().textContent = matching.length;
      row.insertCell().textContent = (matching.reduce((sum, sample) => sum + sample.duration_ms, 0) / 1000).toFixed(2);
    }
    details.append(table); node.append(details);
  }
  function appendTrace(node, trace) {
    if (!Array.isArray(trace) || !trace.length) return;
    const details = el('details', 'trace-details');
    details.append(el('summary', '', `Operational trace · ${trace.length} steps`));
    const list = el('div', 'trace-list');
    trace.forEach((step, index) => {
      const item = el('div', 'trace-step');
      const heading = el('div', 'trace-step-title');
      heading.append(el('strong', '', `${String(index + 1).padStart(2, '0')}  ${text(step.tool, 'Tool call')}`));
      if (Number.isFinite(Number(step.duration_ms)) && step.duration_ms !== null) heading.append(el('span', '', `${Math.round(Number(step.duration_ms))} ms`));
      item.append(heading);
      if (step.tool === 'plan_questions' && Array.isArray(step.result?.parts)) {
        item.append(el('p', '', `${step.result.method} · ${step.result.parts.length} planned searches`));
        const questions = el('ol');
        for (const query of step.result.parts) questions.append(el('li', '', query));
        item.append(questions);
      } else if (step.tool === 'searchdocuments') {
        item.append(el('p', '', step.arguments?.query),
          el('small', 'muted', `Requested ${step.arguments?.top_k} passages · retrieved ${step.result?.evidence?.length || 0}`));
      }
      const payload = el('details');
      payload.append(el('summary', '', 'View arguments & result'), el('pre', '', `Arguments\n${safeJSON(step.arguments || {})}\n\nResult\n${safeJSON(step.result)}`));
      item.append(payload);
      list.append(item);
    });
    details.append(list);
    node.append(details);
  }
  async function submitQuestion() {
    const message = $('chat-input').value.trim();
    if (!message || state.chatBusy || !state.user) return;
    state.chatBusy = true;
    const epoch = state.epoch;
    const generation = state.chatGeneration;
    $('chat-welcome').hidden = true;
    rememberQuestion(message);
    addMessage('user', message);
    $('chat-input').value = '';
    $('chat-input').style.height = '';
    $('chat-input').disabled = true;
    $('send-button').disabled = true;
    $('new-conversation').disabled = true;
    const thinking = el('div', 'thinking', 'Searching your documents…');
    thinking.setAttribute('role', 'status');
    $('chat-messages').append(thinking);
    scrollChat();
    try {
      const result = await api('/chat', { method: 'POST', body: { message, top_k: 5 } });
      if (epoch !== state.epoch || generation !== state.chatGeneration) return;
      thinking.remove();
      const response = addMessage('assistant');
      const citations = Array.isArray(result.citations) ? result.citations : [];
      const prefix = `answer-${Date.now()}-${generation}`;
      appendAnswer(response, result.answer, citations, prefix);
      const info = modeInfo(result.mode);
      const mode = el('div', 'message-mode');
      mode.append(el('span', `mode-badge ${info.className}`, info.label));
      response.append(mode);
      if (result.mode) setMode(result.mode);
      if (result.escalation) response.append(el('p', 'escalation-note', `Follow-up / escalation: ${typeof result.escalation === 'object' ? safeJSON(result.escalation) : result.escalation}`));
      appendCitations(response, citations, prefix);
      appendTimings(response, result);
      appendTrace(response, result.trace);
      if (!citations.length) response.append(el('p', 'escalation-note', 'No document citations were returned. Treat this as an operational response or request for clarification, not a verified policy answer.'));
      scrollChatTo(response);
    } catch (error) {
      if (epoch !== state.epoch || generation !== state.chatGeneration) return;
      thinking.remove();
      const response = addMessage('assistant');
      response.append(el('p', 'message-text', error.message));
      const retry = button('Try again', 'button secondary small', 'refresh');
      retry.style.marginTop = '12px';
      retry.addEventListener('click', () => { if (!state.chatBusy) { $('chat-input').value = message; submitQuestion(); } });
      response.append(retry);
      scrollChat();
    } finally {
      thinking.remove();
      if (epoch === state.epoch && generation === state.chatGeneration) {
        state.chatBusy = false;
        $('chat-input').disabled = false;
        $('send-button').disabled = false;
        $('new-conversation').disabled = false;
        $('chat-input').focus({ preventScroll: true });
      }
    }
  }

  function reviewable(doc) {
    const request = doc.processing_request;
    return request?.status === 'pending' && canCheck(folderById(doc.folder_id))
      && !sameId(request.maker_id, state.user?.id) && !sameId(doc.maker_id, state.user?.id);
  }
  function updateCounts() {
    const count = state.documents.filter(isPending).length;
    const pending = state.documents.filter(isPending);
    const reviewCount = pending.filter(reviewable).length;
    $('review-scope').options[0].textContent = `All pending requests (${pending.length})`;
    $('review-scope').options[1].textContent = `I can review (${reviewCount})`;
    const links = $('workspace-links');
    links.replaceChildren();
    for (const [label, total, view, filter] of [
      ['Documents in your library', state.documents.length, 'library', ''],
      ['Requests you can review', reviewCount, 'approvals', 'mine'],
      ['Failed documents', state.documents.filter(isFailed).length, 'library', 'failed'],
      ['Folders you can access', state.folders.length, 'access', '']
    ]) {
      const link = el('a', 'workspace-stat');
      link.href = `#${view}`;
      link.append(el('span', '', label), el('strong', '', total), icon('chevron'));
      link.addEventListener('click', () => {
        if (view === 'library') {
          $('document-search').value = '';
          $('document-status').value = filter;
        }
        if (view === 'approvals') $('review-scope').value = filter;
      });
      links.append(link);
    }
    $('approval-count').textContent = String(count);
    $('approval-count').hidden = count === 0;
    $('upload-button').disabled = !state.folders.some(canMake);
    $('upload-button').title = $('upload-button').disabled ? 'Maker, owner, or delegated-owner access is required for uploads.' : 'Upload a document for approval';
    const manageable = state.folders.filter(canManage);
    $('create-folder-button').disabled = manageable.length === 0;
    $('create-folder-button').title = manageable.length ? 'Create a subfolder with inherited business context' : 'Folder-owner or delegated-owner access is required to create a subfolder';
    $('home-upload').disabled = $('upload-button').disabled;
    $('home-upload').title = $('upload-button').title;
    renderHome();
    renderStats();
  }
  // Summary tiles at the top of each page, computed from data already loaded.
  function fileExt(title) { return (String(title || '').split('.').pop() || '').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 5); }
  function statTile(label, value, sub, className = '') {
    const tile = el('div', `stat-tile ${className}`.trim());
    tile.append(el('span', 'stat-label', label), el('strong', 'stat-value', value), el('span', 'stat-sub', sub || ''));
    return tile;
  }
  function fillStrip(id, tiles) { const node = $(id); if (node) node.replaceChildren(...tiles); }
  function renderStats() {
    if (!state.user) return;
    const docs = state.documents;
    const indexed = docs.filter((d) => ['indexed', 'approved', 'ready'].includes(statusKey(d))).length;
    const pending = docs.filter(isPending);
    const failed = docs.filter(isFailed).length;
    const processing = docs.filter((d) => ['processing', 'ingesting'].includes(statusKey(d))).length;
    fillStrip('stats-library', [
      statTile('Documents', docs.length, `${indexed} searchable`, 'hero'),
      statTile('Pending approval', pending.length, pending.length ? 'Waiting for a checker' : 'Nothing waiting'),
      statTile('Processing', processing, processing ? 'Being indexed now' : 'Idle'),
      statTile('Failed', failed, failed ? 'Retry from the Ingestion monitor' : 'No failures', failed ? 'alert' : '')
    ]);
    const reviewCount = pending.filter(reviewable).length;
    const ownPending = pending.filter((d) => sameId(d.maker_id, state.user.id)).length;
    fillStrip('stats-approvals', [
      statTile('To review', reviewCount, reviewCount ? 'You can approve these' : 'Nothing for you', 'hero'),
      statTile('All pending', pending.length, 'In your folders'),
      statTile('Your uploads waiting', ownPending, 'A different checker must approve')
    ]);
    fillStrip('stats-access', [
      statTile('Folders', state.folders.length, 'You can access', 'hero'),
      statTile('You manage', state.folders.filter(canManage).length, 'Owner or delegate'),
      statTile('You can upload to', state.folders.filter(canMake).length, 'Maker access'),
      statTile('You can review', state.folders.filter(canCheck).length, 'Checker access')
    ]);
    const events = state.events || [];
    const todayText = new Date().toDateString();
    fillStrip('stats-audit', [
      statTile('Events loaded', events.length, 'Newest first', 'hero'),
      statTile('Today', events.filter((e) => e.created_at && new Date(e.created_at).toDateString() === todayText).length, 'Since midnight'),
      statTile('Your actions', events.filter((e) => sameId(e.actor_id, state.user.id)).length, 'In this list')
    ]);
  }
  const HOME_ICONS = ['book', 'shield', 'library', 'folder', 'check', 'audit'];
  const HOME_TONES = ['#c92532', '#ab1c29', '#f0624f', '#c2185b', '#8f1520', '#d97706'];
  function renderHome() {
    if (!state.user) return;
    const cards = $('home-cards');
    cards.replaceChildren();
    const counts = new Map();
    for (const doc of state.documents) counts.set(String(doc.folder_id), (counts.get(String(doc.folder_id)) || 0) + 1);
    const folders = [...state.folders].sort((a, b) => (counts.get(String(b.id)) || 0) - (counts.get(String(a.id)) || 0)).slice(0, 6);
    if (!folders.length) empty(cards, 'No folders yet', 'Ask a folder owner for access to see your knowledge here.', 'folder');
    folders.forEach((folder, index) => {
      const card = el('a', 'home-card');
      card.href = '#library';
      const symbol = icon(HOME_ICONS[index % HOME_ICONS.length]);
      symbol.style.color = HOME_TONES[index % HOME_TONES.length];
      const total = counts.get(String(folder.id)) || 0;
      card.style.setProperty('--i', index);
      const count = el('span', 'home-card-count');
      card.append(symbol, el('h3', '', folder.name || folder.path || 'Folder'), count);
      countUp(count, total, (n) => `${n} ${total === 1 ? 'File' : 'Files'}`);
      card.addEventListener('click', () => { $('document-search').value = folder.name || ''; $('document-status').value = ''; });
      cards.append(card);
    });
    const reviewCount = state.documents.filter(isPending).filter(reviewable).length;
    $('home-hero-title').textContent = reviewCount ? `${reviewCount} ${reviewCount === 1 ? 'document' : 'documents'} to review` : 'Nothing to review';
    $('home-hero-text').textContent = reviewCount ? 'Waiting for a checker decision' : 'Documents you can review will appear here';
    const list = $('home-feed-list');
    list.replaceChildren();
    const recent = [...state.documents].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 4);
    if (!recent.length) empty(list, 'No documents yet', 'Uploaded documents appear here.', 'book');
    for (const [index, doc] of recent.entries()) {
      const folder = folderById(doc.folder_id);
      const post = el('article', 'feed-post');
      post.style.setProperty('--i', index);
      const top = el('div', 'feed-post-top');
      const who = el('div', 'feed-post-who');
      who.append(el('strong', '', folder?.name || 'Business folder'), el('span', '', formatDate(doc.latest_date || doc.created_at)));
      top.append(el('span', 'feed-avatar', initials(folder?.name || 'Folder')), who, statusBadge(doc));
      const foot = el('div', 'feed-post-foot');
      const meta = el('span', 'feed-meta');
      meta.append(icon('file'), document.createTextNode(`v${doc.version || 1}`));
      const open = button('Open', 'feed-open');
      open.addEventListener('click', () => openDocumentDetails(doc));
      foot.append(meta, open);
      post.append(top, el('p', '', doc.title || 'Untitled document'), foot);
      list.append(post);
    }
  }
  async function refreshDocuments() {
    const epoch = state.epoch;
    const data = await api('/api/documents');
    if (epoch !== state.epoch) return;
    state.documents = data.documents || [];
    updateCounts();
    renderDocuments();
    renderApprovals();
  }
  function renderDocuments() {
    const search = $('document-search').value.toLowerCase().trim();
    const status = $('document-status').value;
    const docs = state.documents.filter((doc) => {
      const searchable = `${doc.title || ''} ${doc.folder_path || folderById(doc.folder_id)?.path || ''}`.toLowerCase();
      const key = statusKey(doc);
      const matches = !status || (status === 'pending' ? isPending(doc) : status === 'failed' ? isFailed(doc) : status === 'indexed' ? ['indexed', 'approved', 'ready'].includes(key) : status === 'processing' ? ['processing', 'ingesting'].includes(key) : key === status);
      return searchable.includes(search) && matches;
    }).sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    $('document-count').textContent = `${docs.length} ${docs.length === 1 ? 'document' : 'documents'}`;
    if (!docs.length) {
      empty($('document-list'), search || status ? 'No matching documents' : 'No documents yet', search || status ? 'Try a different search or status filter.' : 'Approved documents appear here. Makers can upload one to start a review.', 'book');
      return;
    }
    const scroll = el('div', 'table-scroll');
    scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', 'Documents. Scroll horizontally to view version, date, and actions.');
    const table = el('table');
    const caption = el('caption', 'sr-only', 'Documents with status, version, latest date, and available actions');
    const head = el('thead');
    const tr = el('tr');
    for (const label of ['Document', 'Status', 'Version', 'Latest date', 'Actions']) { const th = el('th', '', label); th.scope = 'col'; tr.append(th); }
    head.append(tr);
    const body = el('tbody');
    for (const doc of docs) {
      const row = el('tr');
      const titleCell = el('td');
      const title = el('div', 'document-title-cell');
      const file = el('span', 'file-icon'); file.append(icon('file')); file.dataset.ext = fileExt(doc.title);
      const copy = el('div');
      copy.append(el('div', 'document-name', doc.title || 'Untitled document'), el('div', 'document-path', doc.folder_path || folderById(doc.folder_id)?.path || 'Business folder'));
      title.append(file, copy); titleCell.append(title);
      const statusCell = el('td'); statusCell.append(statusBadge(doc));
      const version = el('td'); version.append(el('span', 'document-version', `v${doc.version || 1}`));
      const date = el('td', '', formatDate(doc.latest_date || doc.created_at));
      const actionsCell = el('td');
      const actions = el('div', 'table-actions');
      const detail = button('Details', 'mini-button');
      detail.setAttribute('aria-label', `Details for ${doc.title || 'document'}`);
      detail.addEventListener('click', () => openDocumentDetails(doc));
      actions.append(detail);
      if (canCancelRequest(doc)) actions.append(cancelRequestButton(doc.processing_request.id));
      if (canMake(folderById(doc.folder_id))) {
        const edit = button('Edit metadata', 'mini-button');
        edit.setAttribute('aria-label', `Edit metadata for ${doc.title || 'document'}`);
        edit.addEventListener('click', () => editMetadata(doc));
        actions.append(edit);
        if (['indexed', 'archived'].includes(statusKey(doc)) && !['pending', 'approved', 'processing'].includes(doc.processing_request?.status)) {
          const reprocess = button('Request enrichment', 'mini-button');
          reprocess.addEventListener('click', () => requestEnrichment(doc));
          actions.append(reprocess);
        }
        if (isFailed(doc)) {
          const retry = button('Retry', 'mini-button');
          retry.addEventListener('click', () => busy(retry, async () => {
            await api(`/api/documents/${encodeURIComponent(doc.id)}/retry`, { method: 'POST', body: {} });
            notify('Processing retry requested. Refresh to check the latest status.');
            await refreshDocuments();
          }).catch(alertError));
          actions.append(retry);
        }
      }
      actionsCell.append(actions);
      row.append(titleCell, statusCell, version, date, actionsCell);
      body.append(row);
    }
    table.append(caption, head, body); scroll.append(table);
    $('document-list').replaceChildren(scroll);
  }
  function openDocumentDetails(doc) {
    const body = openDialog('Document details', 'SOURCE RECORD');
    body.append(el('h3', '', doc.title || 'Untitled document'));
    const info = el('div', 'business-context'); info.style.marginTop = '16px';
    const properties = [ ['Folder', doc.folder_path || folderById(doc.folder_id)?.path], ['Status', statusKey(doc).replaceAll('_', ' ')], ['Version', `v${doc.version || 1}`], ['Latest date', formatDate(doc.latest_date)], ['Uploaded', formatDate(doc.created_at, true)], ['Maker', doc.maker_id], ['Checker', doc.checker_id] ];
    for (const [label, value] of properties) { const row = el('p'); row.append(el('strong', '', `${label}: `), document.createTextNode(text(value))); info.append(row); }
    body.append(info);
    if (doc.processing_request) {
      body.append(el('h3', '', 'Processing approval'), el('pre', 'audit-detail', safeJSON(doc.processing_request)));
    }
    if (doc.error) body.append(el('div', 'inline-error', text(doc.error)));
    if (doc.metadata && Object.keys(doc.metadata).length) { body.append(el('h3', '', 'Metadata'), el('pre', 'audit-detail', safeJSON(doc.metadata))); }
    const siblings = state.documents.filter((other) => sameId(other.folder_id, doc.folder_id) && other.title === doc.title).sort((a, b) => Number(b.version || 0) - Number(a.version || 0));
    if (siblings.length > 1) {
      const versions = el('div', 'metadata-preview');
      versions.append(el('h3', '', 'Version history'));
      for (const record of siblings) versions.append(el('p', '', `v${record.version || 1} · ${formatDate(record.latest_date || record.created_at)} · ${statusKey(record).replaceAll('_', ' ')}`));
      body.append(versions);
    }
    body.append(el('p', 'dialog-notice', 'Cited snippets are available in Ask knowledge. Raw source downloads are not exposed to protect sensitive information.'));
  }
  function openUpload() {
    const allowed = state.folders.filter(canMake);
    if (!allowed.length) return notify('You need maker or owner access to upload a document.');
    const body = openDialog('Upload a document', 'MAKER–CHECKER ONBOARDING');
    const form = el('form');
    const zone = el('div', 'upload-zone');
    const file = el('input'); file.type = 'file'; file.id = 'upload-file'; file.required = true;
    file.accept = '.pdf,.doc,.docx,.txt,.md,.markdown,.csv,.tsv,.log,.parquet,.xlsx,.xls,.xlsm,.xml,.eml,.msg,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp';
    const label = el('label', '', 'Choose a source document'); label.htmlFor = file.id;
    zone.append(icon('upload'), label, el('p', 'field-help', 'PDF, Office documents, email, text, or images. Parser support is determined by the server.'), file);
    const folder = field('Destination folder', 'select', '', { required: true });
    for (const entry of allowed) option(folder.input, entry.path || entry.name, String(entry.id));
    const date = field('Latest document date', 'date', today(), { help: 'Defaults to today. A new upload preserves the document’s version history.' });
    date.input.max = today();
    const metadata = field('Additional metadata (optional JSON)', 'textarea', '', { placeholder: '{"description": "Policy update"}', help: 'Fields: description, classification, domain, owner_note, licence_reference. Business context is inherited from the folder.' });
    const error = inlineError();
    form.append(zone, folder.wrap, contextBox(folder.input), date.wrap, metadata.wrap, el('p', 'dialog-notice', 'Your upload enters a staging area for checker review. It is not available for answers until approved and successfully indexed.'));
    dialogActions(form, 'Submit for approval', error, async () => {
      if (!file.files?.[0]) throw new Error('Choose a document to upload.');
      const payload = new FormData();
      payload.append('file', file.files[0]);
      payload.append('folder_id', folder.input.value);
      if (date.input.value) payload.append('latest_date', date.input.value);
      payload.append('metadata', JSON.stringify(metadataObject(metadata.input.value)));
      let result;
      try {
        result = await api('/api/documents', { method: 'POST', body: payload });
      } catch (error) {
        if (error.status !== 409 || error.detail?.code !== 'duplicate_filename') throw error;
        const matches = error.detail.documents;
        if (!matches.length) throw new Error('The existing files changed. Please submit again.');
        const choice = await new Promise((resolve) => {
          const panel = el('div', 'content-card');
          panel.setAttribute('role', 'group');
          panel.setAttribute('aria-label', 'Existing filename');
          panel.append(el('h3', '', 'An indexed file with this name already exists'),
            el('p', '', `“${file.files[0].name}” has ${matches.length} indexed version(s) in this folder.`),
            el('p', '', 'Replace removes the matching indexed versions, their stored files and search entries, and cancels their queued requests. Keep as new version preserves them.'));
          const actions = el('div', 'dialog-actions');
          for (const [label, value] of [['Replace', 'replace'], ['Keep as new version', 'version'], ['Cancel upload', null]]) {
            const control = button(label);
            control.addEventListener('click', () => { panel.remove(); resolve(value); }, { once: true });
            actions.append(control);
          }
          panel.append(actions); form.append(panel);
          actions.firstElementChild.focus();
        });
        if (!choice) return;
        payload.append('replace_ids', JSON.stringify(matches.map((doc) => doc.id)));
        payload.append('duplicate_action', choice);
        result = await api('/api/documents', { method: 'POST', body: payload });
      }
      $('form-dialog').close();
      notify(result.cleanup_pending ? 'Replacement uploaded. Previous files and index entries are being cleaned up; checker approval is required.' : 'Document uploaded and submitted for checker approval.');
      await refreshDocuments().catch(alertError);
    });
    body.append(form);
    file.focus();
  }
  function editMetadata(doc) {
    const body = openDialog('Edit document metadata', 'DOCUMENT GOVERNANCE');
    const form = el('form');
    const date = field('Latest document date', 'date', String(doc.latest_date || today()).slice(0, 10), { required: true });
    const editable = Object.fromEntries(Object.entries(doc.metadata || {}).filter(([key]) => metadataKeys.has(key)));
    const metadata = field('Document metadata (JSON)', 'textarea', JSON.stringify(editable, null, 2), { help: 'Editable fields: description, classification, domain, owner_note, licence_reference. Values must be text or true / false. Business context is inherited. Updating an indexed record may trigger reprocessing.' });
    date.input.max = today();
    const error = inlineError();
    form.append(el('p', 'dialog-notice', doc.title || 'Document'), date.wrap, metadata.wrap);
    dialogActions(form, 'Save metadata', error, async () => {
      await api(`/api/documents/${encodeURIComponent(doc.id)}/metadata`, { method: 'PATCH', body: { latest_date: date.input.value, metadata: metadataObject(metadata.input.value) } });
      $('form-dialog').close();
      notify('Document metadata updated.');
      await refreshDocuments().catch(alertError);
    });
    body.append(form);
    date.input.focus();
  }
  function requestEnrichment(doc) {
    const body = openDialog('Request document enrichment', 'MAKER–CHECKER REPROCESSING');
    const form = el('form');
    form.append(el('p', 'dialog-notice', `Submit only ${doc.title} for reprocessing under the current server profile. No new file is imported. A different checker must approve the captured source hash and processing/transfer scope. Existing published content stays available until the replacement generation commits.`));
    dialogActions(form, 'Submit for checker review', inlineError(), async () => {
      await api('/api/reprocessing', { method: 'POST', body: { folder_id: doc.folder_id, document_ids: [doc.id], idempotency_key: `ui:${crypto.randomUUID()}` } });
      $('form-dialog').close();
      notify('Enrichment request submitted; no checker approval was assumed.');
      await refreshDocuments();
    });
    body.append(form);
  }
  async function openCheckerPdf(doc, request) {
    const body = openDialog('Review PDF before approval', 'CHECKER SOURCE REVIEW');
    const epoch = state.epoch;
    body.append(el('h3', '', doc.title), el('p', 'dialog-notice', 'Original submitted content for this pending approval. Viewing does not approve the request. Close the viewer to confirm scope and approve or reject.'));
    const message=el('p','','Loading source…');body.append(message);
    let data;
    try { data=await api(`/api/processing-requests/${encodeURIComponent(request.id)}/review-pdf`); }
    catch(error) { message.className='inline-error';message.textContent=error.message;return; }
    if(epoch!==state.epoch || !message.isConnected || !$('form-dialog').open) return;
    message.textContent=`Version ${data.version} · ${data.page_count} pages`;
    body.append(el('pre','audit-detail',`SHA-256: ${data.source_sha256}`));
    const toolbar=el('div','pdf-debug-toolbar');
    const page=el('select');page.setAttribute('aria-label','Review PDF page');
    for(let n=1;n<=data.page_count;n++){const option=el('option','',`Page ${n}`);option.value=n;page.append(option);}
    const zoom=el('select');zoom.setAttribute('aria-label','Review PDF zoom');
    [100,150,200].forEach(n=>{const option=el('option','',`${n}%`);option.value=n;zoom.append(option);});
    const previous=button('Previous','mini-button'),next=button('Next','mini-button');
    toolbar.append(previous,page,next,zoom);body.append(toolbar);
    const viewport=el('div','checker-pdf-viewport');
    viewport.tabIndex=0;viewport.setAttribute('role','region');
    viewport.setAttribute('aria-label','PDF pages. Scroll to read the document.');
    const stack=el('div','checker-pdf-pages');viewport.append(stack);body.append(viewport);
    const pages=[];
    const observer=new IntersectionObserver((entries)=>{
      for(const entry of entries){
        if(!entry.isIntersecting) continue;
        const img=entry.target.querySelector('img');
        if(!img.src) img.src=img.dataset.src;
        observer.unobserve(entry.target);
      }
    },{root:viewport,rootMargin:'600px 0px'});
    for(let n=1;n<=data.page_count;n++){
      const sheet=el('figure','checker-pdf-page');
      const caption=el('figcaption','',`Page ${n} of ${data.page_count}`);
      const frame=el('div','checker-pdf-frame');
      const size=data.page_sizes?.[n-1];
      frame.style.aspectRatio=size ? `${size[0]} / ${size[1]}` : '3 / 4';
      const img=el('img');img.alt=`Submitted PDF page ${n}`;img.decoding='async';
      img.dataset.src=`/api/processing-requests/${encodeURIComponent(request.id)}/review-pdf/pages/${n}`;
      const status=el('p','checker-pdf-page-status','Loading page…');
      img.addEventListener('load',()=>{status.hidden=true;});
      img.addEventListener('error',()=>{
        status.hidden=false;status.replaceChildren(document.createTextNode(`Page ${n} could not load. `));
        const retry=button('Retry page','mini-button');
        retry.addEventListener('click',()=>{status.textContent='Loading page…';img.src=img.dataset.src;});
        status.append(retry);
      });
      frame.append(img,status);sheet.append(caption,frame);stack.append(sheet);
      pages.push(sheet);observer.observe(sheet);
    }
    const updateButtons=()=>{
      previous.disabled=Number(page.value)<=1;next.disabled=Number(page.value)>=data.page_count;
    };
    const jump=number=>{
      const target=Math.max(1,Math.min(data.page_count,number));
      page.value=String(target);viewport.scrollTop=pages[target-1].offsetTop;updateButtons();
    };
    let frameId=0;
    viewport.addEventListener('scroll',()=>{
      if(frameId) return;
      frameId=requestAnimationFrame(()=>{
        frameId=0;
        let low=0,high=pages.length-1;
        while(low<high){
          const mid=Math.ceil((low+high)/2);
          if(pages[mid].offsetTop<=viewport.scrollTop+40) low=mid;else high=mid-1;
        }
        page.value=String(low+1);updateButtons();
      });
    },{passive:true});
    previous.addEventListener('click',()=>jump(Number(page.value)-1));
    next.addEventListener('click',()=>jump(Number(page.value)+1));
    page.addEventListener('change',()=>jump(Number(page.value)));
    zoom.addEventListener('change',()=>{const current=Number(page.value);stack.style.width=`${zoom.value}%`;jump(current);});
    const done=button('Return to approval','button primary');
    done.addEventListener('click',()=>$('form-dialog').close());body.append(done);
    $('form-dialog').addEventListener('close',()=>{observer.disconnect();cancelAnimationFrame(frameId);},{once:true});
    updateButtons();

  }

  function renderApprovals() {
    const mine = $('review-scope').value === 'mine';
    const docs = state.documents.filter(doc => isPending(doc) && (!mine || reviewable(doc))).sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
    const list = $('approval-list');
    if (!docs.length && mine) return empty(list, 'No requests for you to review', 'Choose All pending requests to see other submissions.', 'check');
    if (!docs.length) return empty(list, 'No documents waiting for review', 'Nothing is awaiting approval in your folders.', 'check');
    list.replaceChildren();
    for (const doc of docs) {
      const card = el('article', 'approval-card');
      const top = el('div', 'approval-top');
      const file = el('span', 'file-icon'); file.append(icon('file')); file.dataset.ext = fileExt(doc.title);
      const copy = el('div'); copy.append(el('h3', '', doc.title || 'Untitled document'), el('p', '', doc.folder_path || folderById(doc.folder_id)?.path || 'Business folder'));
      top.append(file, copy, statusBadge(doc));
      const context = el('div', 'approval-context');
      for (const [label, value] of [['Maker', doc.maker_id], ['Submitted', formatDate(doc.created_at)], ['Version', `v${doc.version || 1}`], ['Latest date', formatDate(doc.latest_date)]]) { const item = el('div'); item.append(el('strong', '', label), document.createTextNode(text(value))); context.append(item); }
      const details = button('View record & metadata', 'mini-button'); details.addEventListener('click', () => openDocumentDetails(doc)); context.append(details);
      card.append(top, context);
      const request = doc.processing_request;
      if (canCancelRequest(doc)) card.append(cancelRequestButton(request.id));
      const own = sameId(doc.maker_id, state.user?.id) || sameId(request?.maker_id, state.user?.id);
      if (!request || request.status !== 'pending') {
        card.append(el('p', 'approval-lock-note', 'This legacy record needs a fresh maker submission before review. A checker ID alone is not approval.'));
      } else if (own || !canCheck(folderById(doc.folder_id))) {
        card.append(el('p', 'approval-lock-note', own ? 'This is your upload. A different authorised checker must review it.' : 'Checker or folder-owner access is required to review this document.'));
      } else {
        if (/\.pdf$/i.test(doc.title)) {
          const preview = button('Open PDF for review', 'button secondary');
          preview.addEventListener('click', () => openCheckerPdf(doc, request).catch(alertError));
          card.append(preview);
        }
        const form = el('form', 'review-form');
        const scope = el('details');
        scope.append(el('summary', '', 'Review source hash and exact processing/transfer scope'),
          el('pre', 'audit-detail', safeJSON({ source_sha256: request.source_sha256, source_version: request.source_version,
            scope_sha256: request.scope_sha256, scope: request.scope_json })));
        const confirm = field('I reviewed this source hash, version, destination and external-transfer scope', 'checkbox');
        let controls;
        if (doc.metadata?.domain === 'external') {
          controls = field('Checker control evidence references (JSON)', 'textarea', JSON.stringify({ licence: '', risk: '', data_protection: '', legal: '' }, null, 2),
            { help: 'Provide reviewed evidence references for every control. Maker-entered approval flags are not accepted.' });
          form.append(controls.wrap);
        }
        form.append(scope, confirm.wrap);
        const label = el('label', '', 'Review note');
        const note = el('input'); note.id = `review-${field.counter++}`; label.htmlFor = note.id; note.placeholder = 'Add context (required when rejecting)'; note.maxLength = 500;
        const reject = button('Reject', 'button danger'); reject.type = 'submit'; reject.value = 'reject'; reject.name = 'decision';
        const approve = button('Approve & ingest', 'button primary', 'check'); approve.type = 'submit'; approve.value = 'approve'; approve.name = 'decision';
        const error = inlineError();
        form.append(label, note, reject, approve, error);
        form.addEventListener('submit', (event) => {
          event.preventDefault();
          const decision = event.submitter?.value;
          if (!['approve', 'reject'].includes(decision)) { showInline(error, new Error('Choose Approve or Reject to submit a review.')); return; }
          if (decision === 'approve' && !confirm.input.checked) { showInline(error, new Error('Review and confirm the exact source and processing scope before approval.')); return; }
          if (decision === 'reject' && !note.value.trim()) { showInline(error, new Error('Add a review note explaining why the document is being rejected.')); note.focus(); return; }
          error.hidden = true;
          busy(form, async () => {
            let controlEvidence = {};
            if (controls && decision === 'approve') {
              try { controlEvidence = JSON.parse(controls.input.value); } catch { throw new Error('Control evidence must be a JSON object.'); }
              if (!controlEvidence || Array.isArray(controlEvidence) || typeof controlEvidence !== 'object') throw new Error('Control evidence must be a JSON object.');
            }
            await api(`/api/documents/${encodeURIComponent(doc.id)}/review`, { method: 'POST', body: {
              decision, note: note.value.trim(), request_id: request.id,
              scope_sha256: request.scope_sha256, control_evidence: controlEvidence
            } });
            notify(decision === 'approve' ? 'Document approved. Processing status is available in the library.' : 'Document rejected. Your review note has been recorded.');
            await refreshDocuments();
          }).catch((err) => showInline(error, err));
        });
        card.append(form);
      }
      list.append(card);
    }
  }


  const stageLabels = { cleaning_up: 'Cleaning up previous file', no_request: 'No request', awaiting_checker: 'Awaiting checker', queued: 'Queued', processing: 'Processing', indexed: 'Indexed', failed: 'Failed', cancelled: 'Cancelled', rejected: 'Rejected' };
  const stageOrder = ['cleaning_up', 'awaiting_checker', 'queued', 'processing', 'indexed', 'failed', 'rejected', 'cancelled'];
  const stepLabels = { source_fetch: 'Reading source', raw_storage: 'Saving source', parsing: 'Parsing text and tables', visual_enrichment: 'Reading images with VL', asset_storage: 'Saving images', extracted_storage: 'Saving extraction evidence', parsed_storage: 'Saving parsed content', chunking: 'Chunking', curated_storage: 'Saving chunks', embedding_guardrails: 'Checking text before embedding', text_embeddings: 'Generating embeddings', indexing: 'Writing search index', publication: 'Publishing', completed: 'Complete' };
  function stepText(item) {
    if (!item.current_step) return ({ indexed: 'Published generation available', awaiting_checker: 'Waiting for review', queued: 'Waiting for worker' })[item.stage] || 'Step not recorded';
    const p = item.progress || {};
    let value = stepLabels[item.current_step] || item.current_step.replaceAll('_', ' ');
    if (Number.isInteger(p.completed) && Number.isInteger(p.total)) value += ` · ${p.completed}/${p.total}`;
    return value;
  }
  async function refreshIngestion() {
    const epoch = state.epoch;
    const data = await api('/api/ingestion');
    if (epoch !== state.epoch) return;
    state.ingestion = data;
    renderIngestion();
  }
  function stageBadge(stage) {
    const badge = el('span', `status stage-${stage}`, stageLabels[stage] || stage);
    return badge;
  }
  function renderIngestion() {
    const data = state.ingestion;
    if (!data) { loading($('ingestion-list')); return; }
    const filter = $('ingestion-filter').value;
    const search = $('ingestion-search').value.toLowerCase().trim();
    const summary = el('div', 'ingestion-summary');
    for (const stage of stageOrder) {
      const chip = el('button', `ingestion-chip stage-${stage}${filter === stage ? ' active' : ''}`);
      chip.type = 'button';
      chip.append(el('strong', '', data.counts?.[stage] || 0), document.createTextNode(` ${stageLabels[stage]}`));
      chip.addEventListener('click', () => { $('ingestion-filter').value = filter === stage ? '' : stage; renderIngestion(); });
      summary.append(chip);
    }
    $('ingestion-summary').replaceChildren(summary);
    $('ingestion-updated').textContent = `Updated ${formatDate(data.generated_at, true)} · auto-refreshes every 5 s`;
    const items = (data.items || []).filter((item) => (!filter || item.stage === filter)
      && `${item.title} ${item.folder_path}`.toLowerCase().includes(search));
    if (!items.length) { empty($('ingestion-list'), 'No results', filter || search ? 'No documents match this filter.' : 'No ingestion activity in your folders yet.', 'upload'); return; }
    const scroll = el('div', 'table-scroll');
    scroll.tabIndex = 0;
    const table = el('table');
    const head = el('thead'); const tr = el('tr');
    for (const label of ['Document', 'Stage', 'Request', 'Submitted', 'PDF inspector', 'Actions']) { const th = el('th', '', label); th.scope = 'col'; tr.append(th); }
    head.append(tr);
    const body = el('tbody');
    for (const item of items) {
      const row = el('tr', item.stage === 'failed' ? 'ingestion-failed-row' : '');
      const nameCell = el('td'); const name = el('div');
      name.append(el('div', 'document-name', `${item.title} · v${item.version}`), el('div', 'document-path', item.folder_path));
      if (item.failure) name.append(el('div', 'ingestion-error-line', item.failure.message || 'Processing failed.'));
      nameCell.append(name);
      const stageCell = el('td'); stageCell.append(stageBadge(item.stage));
      stageCell.append(el('div', 'document-path', stepText(item)));
      if (item.stage === 'processing' && item.progress?.model) stageCell.append(el('div', 'document-path', item.progress.model));
      if (item.stage === 'processing') stageCell.append(el('span', 'loading-spinner ingestion-spin'));
      const reqCell = el('td', '', item.request_id ? `${item.request_kind} · ${item.request_id.slice(0, 8)}` : '—');
      if (item.stage === 'indexed' && ['cancelled', 'rejected'].includes(item.request_status)) reqCell.append(el('div', 'document-path', `Latest replacement ${item.request_status}; published document retained`));
      reqCell.append(el('div', 'document-path', `maker ${text(item.maker_id)} · checker ${text(item.checker_id)}`));
      const dateCell = el('td', '', formatDate(item.created_at, true));
      const actionsCell = el('td'); const actions = el('div', 'table-actions');
      const detail = button('Details', 'mini-button');
      detail.addEventListener('click', () => openIngestionDetails(item));
      actions.append(detail);
      if (item.can_cancel) actions.append(cancelRequestButton(item.request_id));
      if (item.can_retry) {
        const retry = button('Retry', 'mini-button');
        retry.addEventListener('click', () => retryIngestion(item, retry));
        actions.append(retry);
      }
      actionsCell.append(actions);
      const inspectorCell = el('td', '', '—');
      if (item.request_id && /\.pdf$/i.test(item.title)) {
        const inspect = button('PDF inspector', 'mini-button');
        inspect.setAttribute('aria-label', `PDF inspector for ${item.title}, version ${item.version}`);
        inspect.addEventListener('click', () => openPdfInspector(item).catch(alertError));
        inspectorCell.replaceChildren(inspect);
      }
      row.append(nameCell, stageCell, reqCell, dateCell, inspectorCell, actionsCell);
      body.append(row);
    }
    table.append(head, body); scroll.append(table);
    $('ingestion-list').replaceChildren(scroll);
  }
  function retryIngestion(item, control) {
    return busy(control, async () => {
      const result = await api(`/api/processing-requests/${encodeURIComponent(item.request_id)}/retry`, { method: 'POST', body: {} });
      notify(result.mode === 'requeued' ? 'Retry queued under the existing checker approval; no new approval needed.' : 'Source or scope changed since approval: a fresh request was created and needs checker approval.');
      await refreshIngestion();
      refreshDocuments().catch(() => {});
    }).catch(alertError);
  }
  async function loadPdfChoices() {
    const host=$('pdfdebug-content');
    const epoch=state.epoch;
    const data=await api('/api/ingestion');
    if (epoch !== state.epoch || host.dataset.loaded) return;
    $('view-pdfdebug').classList.remove('inspecting');
    const files = data.items.filter(item => item.request_id && /\.pdf$/i.test(item.title))
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (!files.length) {
      empty(host, 'No PDFs to inspect', 'PDFs appear here after an ingestion request is created.', 'file');
      return;
    }
    const toolbar = el('div', 'section-toolbar');
    const searchField = el('div', 'search-field');
    const search = el('input');
    search.type = 'search';
    search.placeholder = 'Search filename, folder, or run ID';
    search.setAttribute('aria-label', search.placeholder);
    searchField.append(icon('search'), search);
    const stage = el('select');
    stage.setAttribute('aria-label', 'Filter by ingestion stage');
    stage.append(new Option('All stages', ''));
    for (const value of new Set(files.map(item => item.stage))) {
      stage.append(new Option(stageLabels[value] || value, value));
    }
    toolbar.append(searchField, stage);
    const card = el('div', 'content-card');
    const heading = el('div', 'card-heading');
    const count = el('span', 'small-text muted');
    count.setAttribute('role', 'status');
    heading.append(el('h2', '', 'PDF files'), count);
    const results = el('div', 'table-scroll');
    card.append(heading, results);
    host.replaceChildren(toolbar, card);

    function renderFiles() {
      const query = search.value.trim().toLowerCase();
      const matches = files.filter(item => (!stage.value || stage.value === item.stage)
        && [item.title, item.folder_path, item.request_id].some(value => value?.toLowerCase().includes(query)));
      count.textContent = `${matches.length} of ${files.length} shown`;
      if (!matches.length) {
        empty(results, 'No matching PDFs', 'Try another search or choose a different stage.', 'search');
        return;
      }
      const table = el('table');
      table.setAttribute('aria-label', 'PDF files by most recent submission');
      const headers = table.createTHead().insertRow();
      for (const label of ['PDF file', 'Stage', 'Submitted', 'Action']) {
        const th = el('th', '', label);
        th.scope = 'col';
        headers.append(th);
      }
      const body = table.createTBody();
      for (const item of matches) {
        const row = body.insertRow();
        row.insertCell().append(el('div', 'document-name', item.title),
          el('div', 'document-path', `${item.folder_path} · v${item.version}`));
        row.insertCell().append(stageBadge(item.stage));
        row.insertCell().append(el('div', '', formatDate(item.created_at, true)),
          el('div', 'document-path', `Run ${item.request_id.slice(0, 8)}`));
        const inspect = button('Inspect PDF', 'mini-button');
        inspect.setAttribute('aria-label', `Inspect ${item.title}, version ${item.version}, run ${item.request_id.slice(0, 8)}`);
        inspect.addEventListener('click', () => openPdfInspector(item).catch(alertError));
        row.insertCell().append(inspect);
      }
      results.replaceChildren(table);
    }
    search.addEventListener('input', renderFiles);
    stage.addEventListener('change', renderFiles);
    renderFiles();
  }
  // Object kinds as Docling names them, grouped into families that share one colour on the page.
  const pdfKindFamilies = {
    text: ['Text', '#4f6bed'], paragraph: ['Text', '#4f6bed'], reference: ['Text', '#4f6bed'],
    section_header: ['Heading', '#7c3aed'], title: ['Heading', '#7c3aed'],
    list_item: ['List item', '#0d9488'], table: ['Table', '#15803d'], document_index: ['Table', '#15803d'],
    caption: ['Caption', '#c2416c'], footnote: ['Footnote', '#c2416c'],
    page_header: ['Page furniture', '#94a3b8'], page_footer: ['Page furniture', '#94a3b8'],
    formula: ['Formula or code', '#0e7490'], code: ['Formula or code', '#0e7490'],
  };
  function pdfKind(kind) {
    const key = String(kind || 'unknown').toLowerCase().split('.').pop();
    const pretty = key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
    if (pdfKindFamilies[key]) return { name: pretty, family: pdfKindFamilies[key][0], color: pdfKindFamilies[key][1] };
    if (/chart|plot|graph|diagram|picture|image|figure|logo|photo|map|icon|signature|stamp|screenshot|infographic/.test(key)) return { name: pretty, family: 'Picture or chart', color: '#d97706' };
    return { name: pretty, family: 'Other', color: '#64748b' };
  }
  async function openPdfInspector(item) {
    const host = $('pdfdebug-content');
    // Opened from the Ingestion monitor; the run id marks which PDF is showing, so a slower earlier
    // request cannot replace a newer one.
    host.dataset.loaded = item.request_id;
    location.hash = 'pdfdebug';
    const epoch = state.epoch;
    const isCurrent = () => epoch === state.epoch && host.dataset.loaded === item.request_id;
    const back = button('Back to ingestion', 'button secondary');
    back.addEventListener('click', () => { delete host.dataset.loaded; host.replaceChildren(); location.hash = 'ingestion'; });
    host.replaceChildren(el('div', 'pdfi-loading', 'Loading the page geometry…'));
    // Extracted tables and chart data are optional: the inspector still works without them.
    const dataRequest = api(`/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout/data`).catch(() => ({ items: [] }));
    let data;
    try { data = await api(`/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout`); }
    catch (error) { if (isCurrent()) host.replaceChildren(back, el('p', 'inline-error', error.message)); return; }
    if (!isCurrent()) return;
    const extracted = (await dataRequest).items || [];
    if (!isCurrent()) return;
    const dataById = new Map(extracted.map(d => [d.object_id, d]));
    // A table or chart has a Docling layout box (layout:<ref>:<page>:<n>); a chart also has a crop
    // (docling:<ref>:<page>). Both boxes show the same extracted data.
    const dataFor = obj => {
      const id = obj.object_id || '';
      if (id.startsWith('layout:')) {
        const parts = id.slice(7).split(':');
        if (parts.length >= 3) return dataById.get(`docling:${parts.slice(0, -2).join(':')}:${obj.page}`);
      }
      return dataById.get(id);
    };
    // While a PDF is open the page title is hidden, so the viewer gets the height instead.
    $('view-pdfdebug').classList.add('inspecting');
    const objects = data.objects.map((obj, index) => {
      const entry = { ...obj, index, kind: pdfKind(obj.object_kind), crop: obj.extractor !== 'docling-layout' };
      entry.data = dataFor(entry);
      return entry;
    });
    // Each extracted item points back at the box to select for it (the layout box, else the crop).
    extracted.forEach(d => { d.obj = objects.find(o => o.data === d && !o.crop) || objects.find(o => o.data === d); });
    const tableCount = extracted.filter(d => d.kind === 'table').length, chartCount = extracted.length - tableCount;
    const pages = [...new Set(objects.map(o => o.page))].sort((a, b) => a - b);
    const byPage = new Map(pages.map(n => [n, objects.filter(o => o.page === n)]));
    byPage.forEach(list => list.forEach((obj, i) => { obj.number = i + 1; }));
    const requestPath = `/api/processing-requests/${encodeURIComponent(item.request_id)}`;

    // Header: what this is, and the two ways out.
    const head = el('div', 'pdfi-head');
    const heading = el('div', 'pdfi-title');
    const crops = objects.filter(o => o.crop).length;
    const facts = [`${item.folder_path} · v${item.version}`, `${pages.length} ${pages.length === 1 ? 'page' : 'pages'} with objects`,
      `${objects.length - crops} layout regions`, `${crops} processing crops`];
    if (tableCount) facts.push(`${tableCount} ${tableCount === 1 ? 'table' : 'tables'} extracted`);
    if (chartCount) facts.push(`${chartCount} ${chartCount === 1 ? 'chart' : 'charts'} read by Granite`);
    if (data.captured_at) facts.push(`captured ${formatDate(data.captured_at, true)}`);
    const titleText = el('div');
    titleText.append(el('h2', '', item.title), el('p', '', facts.join('  ·  ')));
    heading.append(icon('file'), titleText);
    const actions = el('div', 'pdfi-actions');
    const annotated = el('a', 'button secondary', 'Annotated PDF');
    annotated.href = `${requestPath}/pdf-layout/annotated.pdf?layer=all`;
    annotated.download = `${item.title.replace(/\.pdf$/i, '')}-annotated.pdf`;
    actions.append(annotated, back);
    head.append(heading, actions);
    host.replaceChildren(head);
    if (data.coverage) host.append(el('p', 'pdfi-note', data.coverage));
    if (!pages.length) { const box = el('div'); host.append(box); empty(box, 'No boxes recorded', 'This run stored no object geometry.', 'file'); return; }

    // Toolbar: page stepper, then layer and zoom as segmented controls.
    const bar = el('div', 'pdfi-bar');
    const pager = el('div', 'pdfi-pager');
    const prev = button('', 'pdfi-step prev', 'chevron'); prev.setAttribute('aria-label', 'Previous page');
    const next = button('', 'pdfi-step', 'chevron'); next.setAttribute('aria-label', 'Next page');
    const pageSelect = el('select'); pageSelect.setAttribute('aria-label', 'PDF page');
    pages.forEach(n => { const o = el('option', '', `Page ${n}`); o.value = n; pageSelect.append(o); });
    pager.append(prev, pageSelect, el('span', 'pdfi-of', `of ${pages[pages.length - 1]}`), next);
    function segmented(label, options, value) {
      const group = el('div', 'pdfi-seg'); group.setAttribute('role', 'group'); group.setAttribute('aria-label', label);
      group.value = value;
      for (const [key, caption] of options) {
        const b = button(caption, 'pdfi-seg-item'); b.dataset.value = key; b.setAttribute('aria-pressed', String(key === value));
        b.addEventListener('click', () => {
          if (group.value === key) return;
          group.value = key;
          group.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
          group.dispatchEvent(new Event('change'));
        });
        group.append(b);
      }
      return group;
    }
    const layer = segmented('Boxes shown', [['all', 'All boxes'], ['layout', 'Layout'], ['crops', 'Crops'], ['none', 'Page only']], 'all');
    const zoom = segmented('Zoom', [['page', 'Whole page'], ['width', 'Fit width'], ['150', '150%'], ['200', '200%']], 'page');
    bar.append(pager, layer, zoom);
    host.append(bar);

    // Legend: one entry per kind family with its count in the whole file; selecting one hides or shows it.
    const hidden = new Set();
    const families = new Map();
    objects.forEach(o => { const f = families.get(o.kind.family) || { color: o.kind.color, count: 0 }; f.count++; families.set(o.kind.family, f); });
    const legend = el('div', 'pdfi-legend'); legend.setAttribute('role', 'group'); legend.setAttribute('aria-label', 'Object kinds; select one to hide or show it');
    [...families.entries()].sort((a, b) => b[1].count - a[1].count).forEach(([family, info]) => {
      const chip = button('', 'pdfi-kind'); chip.setAttribute('aria-pressed', 'true');
      chip.style.setProperty('--kind', info.color);
      chip.append(el('span', 'pdfi-swatch'), el('span', '', family), el('span', 'pdfi-count', info.count.toLocaleString()));
      chip.addEventListener('click', () => {
        if (hidden.has(family)) hidden.delete(family); else hidden.add(family);
        chip.setAttribute('aria-pressed', String(!hidden.has(family)));
        drawAll(); renderSide();
      });
      legend.append(chip);
    });
    if (crops) {
      const dashedKey = el('span', 'pdfi-key');
      dashedKey.append(el('span', 'pdfi-swatch dashed'), document.createTextNode('Dashed outline: processing crop'));
      legend.append(dashedKey);
    }
    // The legend lives in the side panel (see renderSide), so the page area keeps the screen height.

    const split = el('div', 'pdfi-split'), canvas = el('div', 'pdfi-canvas'), side = el('aside', 'pdfi-panel');
    canvas.tabIndex = 0; canvas.setAttribute('aria-label', 'PDF pages. Scroll, or use the left and right arrow keys to move between pages');
    side.setAttribute('aria-label', 'Objects on the current page');
    split.append(canvas, side); host.append(split);

    let selected = null, loadError = false;
    const ns = 'http://www.w3.org/2000/svg';
    const pageViews = new Map(), rects = new Map();
    const visible = obj => !hidden.has(obj.kind.family) && layer.value !== 'none'
      && !(layer.value === 'layout' && obj.crop) && !(layer.value === 'crops' && !obj.crop);
    // Every page is stacked in one scroll area and reserves its real aspect ratio, so lazy images never shift the layout.
    pages.forEach(n => {
      const size = byPage.get(n)[0].bbox;
      const fig = el('figure', 'pdfi-page'); fig.dataset.page = n;
      const sheet = el('div', 'pdfi-sheet'); sheet.style.aspectRatio = `${size.page_width} / ${size.page_height}`;
      const img = el('img'); img.alt = `Original PDF page ${n}`; img.loading = 'lazy'; img.decoding = 'async';
      img.src = `${requestPath}/pdf-layout/page/${n}`;
      img.addEventListener('load', () => sheet.classList.add('loaded'));
      img.addEventListener('error', () => { sheet.classList.add('failed'); if (!loadError) { loadError = true; renderSide(); } });
      sheet.append(img);
      const caption = el('figcaption');
      caption.append(el('strong', '', `Page ${n}`), el('span', '', `${byPage.get(n).length} objects`));
      fig.append(caption, sheet); canvas.append(fig);
      pageViews.set(n, { fig, sheet, size });
    });
    function drawPage(n) {
      const view = pageViews.get(n), size = view.size;
      view.sheet.querySelector('svg')?.remove();
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('viewBox', `0 0 ${size.page_width} ${size.page_height}`); svg.setAttribute('preserveAspectRatio', 'none');
      svg.classList.add('pdfi-overlay');
      const tagScale = size.page_width / 600;  // tags keep one size relative to the page
      for (const obj of byPage.get(n)) {
        if (!visible(obj)) continue;
        const b = obj.bbox, g = document.createElementNS(ns, 'g');
        g.classList.add('pdfi-box'); if (obj.crop) g.classList.add('crop'); if (selected === obj) g.classList.add('selected');
        g.style.setProperty('--kind', obj.kind.color);
        const rect = document.createElementNS(ns, 'rect');
        for (const [k, v] of Object.entries({ x: b.left, y: b.top, width: b.right - b.left, height: b.bottom - b.top })) rect.setAttribute(k, v);
        rect.classList.add('pdfi-outline');
        const title = document.createElementNS(ns, 'title'); title.textContent = `${obj.number}. ${obj.kind.name}${obj.crop ? ' (processing crop)' : ''}`;
        const tagH = 9 * tagScale, tagW = (5 + String(obj.number).length * 5) * tagScale, tagY = Math.max(0, b.top - tagH);
        const tag = document.createElementNS(ns, 'rect');
        for (const [k, v] of Object.entries({ x: b.left, y: tagY, width: tagW, height: tagH, rx: 1.5 * tagScale })) tag.setAttribute(k, v);
        tag.classList.add('pdfi-tag');
        const label = document.createElementNS(ns, 'text');
        label.setAttribute('x', b.left + tagW / 2); label.setAttribute('y', tagY + tagH * 0.76); label.setAttribute('font-size', 7 * tagScale);
        label.textContent = obj.number;
        g.append(title, rect, tag, label);
        g.addEventListener('click', () => select(obj, true));
        g.addEventListener('mouseenter', () => side.querySelector(`[data-object="${obj.index}"]`)?.classList.add('hover'));
        g.addEventListener('mouseleave', () => side.querySelector(`[data-object="${obj.index}"]`)?.classList.remove('hover'));
        rects.set(obj.index, g);
        svg.append(g);
      }
      view.sheet.append(svg);
    }
    const drawAll = () => { rects.clear(); pages.forEach(drawPage); };
    function select(obj, fromPage) {
      if (selected) rects.get(selected.index)?.classList.remove('selected');
      selected = selected === obj ? null : obj;
      if (selected) {
        rects.get(selected.index)?.classList.add('selected');
        if (String(selected.page) !== pageSelect.value) pageSelect.value = selected.page;
      }
      renderSide();
      if (selected && fromPage) side.querySelector(`[data-object="${selected.index}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    function detail(obj) {
      const b = obj.bbox, w = b.right - b.left, h = b.bottom - b.top;
      const share = Math.round(w * h / (b.page_width * b.page_height) * 1000) / 10;
      const list = el('dl', 'pdfi-detail');
      for (const [term, value] of [['Kind', obj.object_kind], ['Found by', obj.crop ? `${obj.extractor}, processing crop` : 'Docling layout'],
        ['Position', `${b.left.toFixed(1)} from left, ${b.top.toFixed(1)} from top`], ['Size', `${w.toFixed(1)} × ${h.toFixed(1)} pt, ${share}% of the page`],
        ['Object ID', obj.object_id]]) list.append(el('dt', '', term), el('dd', term === 'Object ID' ? 'mono' : '', value));
      if (!obj.data) return list;
      const wrap = el('div', 'pdfi-detail-wrap');
      wrap.append(list, dataBlock(obj.data, false));
      return wrap;
    }
    // One extracted table or chart: its rows as a table, where it came from, and how far to trust it.
    // In the Data list it also names the page and offers to show the object on the page.
    const numeric = /^[-+−(]?[$€£S]*\s*[\d.,]+\s*[%)]?[a-z]{0,2}$/i;
    function dataBlock(d, inList) {
      const box = el('section', `pdfi-reading ${d.kind}${inList && selected && selected.data === d ? ' selected' : ''}`);
      const head = el('div', 'pdfi-reading-head');
      const title = el('h4', '', d.kind === 'table' ? 'Table data' : d.mode === 'summary' ? 'Chart summary' : 'Chart data');
      if (inList) title.prepend(el('span', 'pdfi-reading-page', `Page ${d.page}`));
      const timing = d.seconds ? `, ${Number(d.seconds).toFixed(1)} s` : '';
      head.append(title, el('span', '', d.kind === 'table' ? 'Docling table structure' : `${d.source}${timing}`));
      box.append(head);
      if (d.caption) box.append(el('p', 'pdfi-reading-caption', d.caption));
      // The worker compared Granite's numbers with the numbers printed in the PDF inside the chart.
      if (d.check) {
        const { status, matched, total, totals_off: totalsOff } = d.check;
        const [label, tone] = totalsOff ? [`Totals do not add up in ${totalsOff} ${totalsOff === 1 ? 'row' : 'rows'}; check the cells against the chart`, 'bad'] : {
          matches: [`Numbers match the PDF text: ${matched} of ${total}`, 'ok'],
          partial: [`Only some numbers match the PDF text: ${matched} of ${total}`, 'warn'],
          'not-found': [`Numbers not found in the PDF text: ${matched} of ${total}`, 'bad'],
          'no-text': ['Not checked: the PDF has no text for this chart', 'none'],
          'no-numbers': ['Not checked: the reading has no numbers', 'none'],
        }[status] || ['Not checked', 'none'];
        box.append(el('p', `pdfi-check ${tone}`, label));
      }
      if (d.rows?.length) {
        const scroll = el('div', 'pdfi-reading-scroll');
        scroll.tabIndex = 0;
        scroll.setAttribute('aria-label', `${d.kind === 'table' ? 'Table' : 'Chart'} data on page ${d.page}, ${d.rows.length} rows; scroll for more`);
        const table = el('table');
        const [header, ...body] = d.rows;
        const headRow = table.createTHead().insertRow();
        header.forEach(cell => { const th = el('th', '', cell); th.scope = 'col'; headRow.append(th); });
        const tbody = table.createTBody();
        body.forEach(cells => {
          const row = tbody.insertRow();
          cells.forEach((cell, i) => {
            if (i === 0) { const th = el('th', '', cell); th.scope = 'row'; row.append(th); return; }
            const td = row.insertCell(); td.textContent = cell; if (numeric.test(cell)) td.className = 'num';
          });
        });
        scroll.append(table);
        box.append(scroll);
      } else if (d.text) {
        box.append(el('p', 'pdfi-reading-text', d.text));
      }
      const foot = el('div', 'pdfi-reading-foot');
      foot.append(el('p', 'pdfi-reading-note', d.kind === 'table'
        ? `${d.rows.length} rows. Structure found by Docling; cell text taken from the PDF. A cell that spans columns repeats its text in each.`
        : d.mode === 'summary'
          ? `${d.note ? `${d.note}. ` : ''}A model's description of the chart, not text from the PDF.`
          : d.check?.status === 'matches'
            ? 'A model reading. Every number is printed on the page, but check that each sits in the right row and column.'
            : 'A model reading of the picture, not text from the PDF. Check the numbers against the chart before relying on them.'));
      if (inList && d.obj) {
        const show = button('Show on page', 'mini-button');
        show.setAttribute('aria-label', `Show this ${d.kind} on page ${d.page}`);
        show.addEventListener('click', () => { if (selected !== d.obj) select(d.obj, false); goToPage(); });
        foot.append(show);
      }
      box.append(foot);
      return box;
    }
    // The side panel has two views: the objects on the current page, and the extracted tables and charts.
    let panelView = 'objects', dataScope = 'page', lastDataKey = '';
    const panelTabs = segmented('Side panel', [['objects', 'Objects'], ['data', 'Tables and charts']], 'objects');
    panelTabs.classList.add('pdfi-tabs');
    panelTabs.querySelector('[data-value="data"]').append(el('span', 'pdfi-tab-count', extracted.length));
    panelTabs.addEventListener('change', () => {
      panelView = panelTabs.value; lastDataKey = '';
      split.classList.toggle('wide', panelView === 'data');
      renderSide();
    });
    const tabsNode = () => (extracted.length ? [panelTabs] : []);
    const updatePager = current => {
      prev.disabled = pages.indexOf(current) <= 0;
      next.disabled = pages.indexOf(current) >= pages.length - 1;
    };
    function renderData(current) {
      // Scrolling the pages calls this often; rebuild only when what it shows has changed.
      const key = `${dataScope}|${dataScope === 'page' ? current : ''}|${selected ? selected.index : ''}`;
      if (key === lastDataKey) return;
      lastDataKey = key;
      const keepScroll = side.querySelector('.pdfi-data-list')?.scrollTop || 0;
      const items = dataScope === 'page' ? extracted.filter(d => d.page === current) : extracted;
      const head = el('div', 'pdfi-panel-head');
      const scope = segmented('Show tables and charts from', [['page', `Page ${current}`], ['all', 'Whole file']], dataScope);
      scope.classList.add('pdfi-scope');
      scope.addEventListener('change', () => { dataScope = scope.value; renderSide(); });
      const tables = items.filter(d => d.kind === 'table').length;
      head.append(scope, el('span', '', `${tables} ${tables === 1 ? 'table' : 'tables'}, ${items.length - tables} ${items.length - tables === 1 ? 'chart' : 'charts'}`));
      const list = el('div', 'pdfi-data-list');
      items.forEach(d => list.append(dataBlock(d, true)));
      side.replaceChildren(...tabsNode(), head, items.length ? list
        : el('p', 'pdfi-empty', dataScope === 'page' ? 'No tables or charts were extracted on this page.' : 'No tables or charts were extracted from this file.'));
      if (dataScope === 'all') list.scrollTop = keepScroll;
    }
    function renderSide() {
      const current = Number(pageSelect.value);
      updatePager(current);
      if (panelView === 'data') { renderData(current); return; }
      const all = byPage.get(current) || [];
      const shown = all.filter(visible);
      const top = el('div', 'pdfi-panel-head');
      top.append(el('h3', '', `Page ${current}`), el('span', '', layer.value === 'none' ? 'boxes hidden' : `${shown.length} of ${all.length} objects`));
      const nodes = [...tabsNode(), top, legend];
      if (loadError) nodes.push(el('p', 'inline-error', 'Some original pages could not be loaded. Check access and storage availability.'));
      const list = el('ol', 'pdfi-list');
      for (const obj of shown) {
        const li = el('li', `pdfi-item${selected === obj ? ' selected' : ''}`); li.dataset.object = obj.index;
        li.style.setProperty('--kind', obj.kind.color);
        const row = el('button', 'pdfi-row'); row.type = 'button'; row.setAttribute('aria-expanded', String(selected === obj));
        const b = obj.bbox;
        const name = el('span', 'pdfi-name', obj.kind.name);
        if (obj.data) name.append(el('span', `pdfi-data-tag ${obj.data.kind}`,
          obj.data.kind === 'table' ? 'Table data' : obj.data.mode === 'summary' ? 'Chart summary' : 'Chart data'));
        row.append(el('span', `pdfi-num${obj.crop ? ' crop' : ''}`, obj.number), name,
          el('span', 'pdfi-size', `${Math.round(b.right - b.left)} × ${Math.round(b.bottom - b.top)} pt`));
        row.addEventListener('click', () => select(obj, false));
        li.addEventListener('mouseenter', () => rects.get(obj.index)?.classList.add('hover'));
        li.addEventListener('mouseleave', () => rects.get(obj.index)?.classList.remove('hover'));
        li.append(row);
        if (selected === obj) li.append(detail(obj));
        list.append(li);
      }
      if (shown.length) nodes.push(list);
      else nodes.push(el('p', 'pdfi-empty', layer.value === 'none' ? 'Boxes are hidden. Choose a layer above to show them.' : 'Nothing of the shown kinds on this page.'));
      side.replaceChildren(...nodes);
    }
    // Smooth only for short moves; a jump across many pages lands at once.
    const goToPage = (smooth = true) => {
      const view = pageViews.get(Number(pageSelect.value)); if (!view) return;
      const top = view.fig.offsetTop - canvas.offsetTop - 16;
      const near = Math.abs(top - canvas.scrollTop) < canvas.clientHeight * 2.5;
      canvas.scrollTo({ top, behavior: smooth && near ? 'smooth' : 'auto' });
    };
    const step = delta => { const i = pages.indexOf(Number(pageSelect.value)) + delta; if (i >= 0 && i < pages.length) { pageSelect.value = pages[i]; renderSide(); goToPage(); } };
    // Keep the page selector and object list in step with whichever page is mostly in view while scrolling.
    let ticking = false;
    canvas.addEventListener('scroll', () => {
      if (ticking) return; ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const middle = canvas.scrollTop + canvas.clientHeight / 3; let best = null;
        for (const [n, view] of pageViews) { if (view.fig.offsetTop - canvas.offsetTop <= middle) best = n; else break; }
        if (best !== null && String(best) !== pageSelect.value) { pageSelect.value = best; renderSide(); }
      });
    }, { passive: true });
    // A property, not a listener: the host element outlives each inspector session.
    host.onkeydown = event => {
      if (event.key === 'Escape' && selected) select(selected, false);
      if (event.target === canvas && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) { event.preventDefault(); step(event.key === 'ArrowRight' ? 1 : -1); }
    };
    prev.addEventListener('click', () => step(-1));
    next.addEventListener('click', () => step(1));
    pageSelect.addEventListener('change', () => { renderSide(); goToPage(); });
    layer.addEventListener('change', () => { drawAll(); renderSide(); });
    // The viewer fills the rest of the screen: its height is what is left below it in the scrolling workspace.
    function sizeViewer() {
      const body = canvas.closest('.workspace-body');
      if (!body || window.matchMedia('(max-width: 760px)').matches) { split.style.removeProperty('--view-h'); return; }
      const top = canvas.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
      split.style.setProperty('--view-h', `${Math.max(440, Math.floor(body.clientHeight - top - 16))}px`);
    }
    // Whole page: every sheet is sized so the full page fits the visible area, whatever its shape.
    function fitPages() {
      canvas.dataset.zoom = zoom.value;
      const style = getComputedStyle(canvas);
      const caption = pageViews.values().next().value?.fig.querySelector('figcaption')?.offsetHeight || 0;
      const width = canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const height = canvas.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom) - caption;
      pageViews.forEach(view => {
        const ratio = view.size.page_width / view.size.page_height;
        view.sheet.style.width = zoom.value === 'page' ? `${Math.max(120, Math.floor(Math.min(width, height * ratio)))}px`
          : zoom.value === 'width' ? '100%' : `${zoom.value}%`;
      });
    }
    zoom.addEventListener('change', () => { fitPages(); goToPage(false); });
    host._pdfiObserver?.disconnect();
    host._pdfiObserver = new ResizeObserver(() => { if (!canvas.isConnected) { host._pdfiObserver.disconnect(); return; } fitPages(); });
    host._pdfiObserver.observe(canvas);
    const onResize = () => { if (!canvas.isConnected) { window.removeEventListener('resize', onResize); return; } sizeViewer(); };
    window.addEventListener('resize', onResize);
    drawAll(); renderSide(); sizeViewer(); fitPages();
  }

  function openIngestionDetails(item) {
    const body = openDialog('Ingestion details', 'PROCESSING TRACE');
    body.append(el('h3', '', `${item.title} · v${item.version}`));
    const info = el('div', 'business-context'); info.style.marginTop = '12px';
    for (const [label, value] of [['Folder', item.folder_path], ['Stage', stageLabels[item.stage]], ['Request', item.request_id], ['Kind', item.request_kind], ['Maker', item.maker_id], ['Checker', item.checker_id], ['Submitted', formatDate(item.created_at, true)], ['Reviewed', formatDate(item.reviewed_at, true)]]) {
      const p = el('p'); p.append(el('strong', '', `${label}: `), document.createTextNode(text(value))); info.append(p);
    }
    body.append(info);
    if (item.request_id && /\.pdf$/i.test(item.title)) {
      const inspect = button('Open PDF inspector', 'button primary');
      inspect.addEventListener('click', () => {
        $('form-dialog').close();
        openPdfInspector(item).catch(alertError);
      });
      body.append(inspect);
      const debug = button('PDF bounding boxes', 'button secondary');
      const panel = el('div');
      debug.addEventListener('click', () => busy(debug, async () => {
        const data = await api(`/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout`);
        panel.replaceChildren(el('p', 'muted', 'Geometry only · top-left origin · coordinates in page units. Layout detections and processing crops may overlap.'));
        if (data.coverage) panel.append(el('p', 'muted', data.coverage));
        if (!data.objects.length) panel.append(el('p', '', 'No object boxes recorded.'));
        const pages = [...new Set(data.objects.map(o => o.page))].sort((a,b) => a-b);
        const selector = el('select'); selector.setAttribute('aria-label', 'PDF page');
        pages.forEach(page => { const option = el('option', '', `Page ${page}`); option.value = page; selector.append(option); });
        const view = el('div');
        const draw = () => {
          view.replaceChildren();
          const objects = data.objects.filter(o => String(o.page) === selector.value);
          if (!objects.length) return;
          const box = objects[0].bbox;
          const ns = 'http://www.w3.org/2000/svg';
          const svg = document.createElementNS(ns, 'svg');
          svg.setAttribute('viewBox', `0 0 ${box.page_width} ${box.page_height}`);
          svg.style.width = '100%'; svg.style.maxHeight = '550px'; svg.style.border = '1px solid #bbb';
          objects.forEach((o, i) => {
            const b = o.bbox, rect = document.createElementNS(ns, 'rect');
            for (const [k,v] of Object.entries({x:b.left,y:b.top,width:b.right-b.left,height:b.bottom-b.top,fill:'none',stroke:o.extractor === 'docling-layout' ? '#2563eb' : '#d97706','stroke-width':1})) rect.setAttribute(k,v);
            const title = document.createElementNS(ns, 'title'); title.textContent = `${i+1}: ${o.object_kind} (${o.object_id})`; rect.append(title); svg.append(rect);
            const label = document.createElementNS(ns, 'text'); label.setAttribute('x', b.left); label.setAttribute('y', b.top+9); label.setAttribute('font-size', 9); label.textContent = i+1; svg.append(label);
          });
          view.append(svg);
          objects.forEach((o,i) => view.append(el('pre', 'audit-detail', `${i+1}. ${o.object_kind} · ${o.object_id} · ${o.extractor}\n${JSON.stringify(o.bbox)}`)));
        };
        selector.addEventListener('change', draw);
        panel.append(selector, view);
        const download = button('Download geometry JSON', 'mini-button');
        download.addEventListener('click', () => {
          const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
          const a = el('a'); a.href = url; a.download = `pdf-layout-${item.request_id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
        panel.append(download);
        panel.append(el('p', 'muted', 'Validate against original pages: blue = Docling layout, orange = processing crops. Numbers match this view. Original-content permission is required.'));
        for (const [layer, label] of [['all','Download annotated PDF'],['layout','Docling layout only'],['crops','Processing crops only']]) {
          const link = el('a', 'button secondary', label);
          link.href = `/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout/annotated.pdf?layer=${layer}`;
          link.target = '_blank'; link.rel = 'noopener'; panel.append(link);
        }
        draw();
      }).catch(alertError));
      body.append(debug, panel);
    }
    body.append(el('p', '', `Current step: ${stepText(item)}`));
    if (item.progress_updated_at) body.append(el('p', 'muted', `Step updated: ${formatDate(item.progress_updated_at, true)}`));
    const failure = item.failure;
    if (failure) {
      body.append(el('h3', '', 'Failure'));
      body.append(el('div', 'inline-error', failure.message || 'Processing failed.'));
      const facts = el('div', 'business-context');
      for (const [label, value] of [['Error code', failure.code], ['Failed at stage', failure.stage], ['Exception', failure.error_type]]) {
        const p = el('p'); p.append(el('strong', '', `${label}: `), document.createTextNode(text(value))); facts.append(p);
      }
      body.append(facts);
      if (!failure.diagnostics_visible) body.append(el('p', 'muted', 'Model response details are visible to makers, checkers and folder owners only.'));
      if (failure.detail) body.append(el('h3', '', 'Error detail'), el('pre', 'audit-detail', failure.detail));
      if (failure.model_responses?.length) {
        body.append(el('h3', '', 'Model response'));
        for (const entry of failure.model_responses) {
          body.append(el('p', 'muted', `${entry.kind.replaceAll('_', ' ')} · model ${text(entry.model)} · HTTP ${text(entry.http_status)} · ${formatDate(entry.at, true)}${entry.truncated ? ' · truncated' : ''}`));
          body.append(el('pre', 'audit-detail', entry.response || '(empty response body)'));
        }
      } else if (failure.diagnostics_visible) {
        body.append(el('p', 'muted', 'No model response was captured for this failure (it may predate capture, or failed before a model call).'));
      }
    }
    if (item.history?.length) {
      body.append(el('h3', '', 'Request history'));
      const hist = el('div', 'metadata-preview');
      for (const h of item.history) hist.append(el('p', '', `${formatDate(h.created_at, true)} · ${h.kind} · ${h.status} · ${h.id.slice(0, 8)}`));
      body.append(hist);
    }
    if (item.can_retry) {
      const retry = button('Retry (submit fresh request)', 'button primary', 'refresh');
      retry.style.marginTop = '16px';
      retry.addEventListener('click', () => retryIngestion(item, retry).then(() => $('form-dialog').close()));
      body.append(retry, el('p', 'muted', 'Retry re-queues this request under its existing checker approval. A fresh approval is only needed if the source or scope changed.'));
    }
  }
  async function refreshFolders() {
    const epoch = state.epoch;
    const result = await api('/api/folders');
    if (epoch !== state.epoch) return;
    state.folders = result.folders || [];
    state.documents = state.documents.filter((doc) => folderById(doc.folder_id));
    if (state.folderId && !folderById(state.folderId)) {
      state.folderId = null;
      folderRequest++;
      empty($('folder-detail'), 'Select a folder', 'Your permissions changed. Choose an accessible folder to continue.', 'folder');
    }
    renderFolders(); updateCounts(); renderDocuments(); renderApprovals();
    if (state.folderId && folderById(state.folderId)) await selectFolder(state.folderId);
  }
  function renderFolders() {
    const list = $('folder-list');
    list.replaceChildren();
    if (!state.folders.length) return empty(list, 'No accessible folders', 'Ask a folder owner to grant you access.', 'folder');
    for (const folder of [...state.folders].sort((a, b) => String(a.path || a.name).localeCompare(String(b.path || b.name)))) {
      const node = button('', 'folder-button');
      node.style.setProperty('--tone', HOME_TONES[[...String(folder.id)].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % HOME_TONES.length]);
      node.classList.toggle('selected', sameId(folder.id, state.folderId));
      node.setAttribute('aria-pressed', String(sameId(folder.id, state.folderId)));
      const copy = el('span', '', folder.name || folder.path);
      copy.append(el('small', '', folder.path || folder.business_unit || 'Business folder'));
      if (roles(folder).length) copy.append(el('small', '', `Your access · ${roles(folder).join(', ')}`));
      node.append(icon('folder'), copy);
      node.addEventListener('click', () => selectFolder(folder.id));
      list.append(node);
    }
  }
  async function ensureEmployees() {
    if (state.employees.length) return state.employees;
    const epoch = state.epoch;
    const data = await api('/api/employees');
    if (epoch === state.epoch) state.employees = data.employees || [];
    return epoch === state.epoch ? state.employees : [];
  }
  async function selectFolder(id) {
    const folder = folderById(id);
    if (!folder) return;
    state.folderId = id;
    const request = ++folderRequest;
    const epoch = state.epoch;
    renderFolders();
    const detail = $('folder-detail');
    detail.replaceChildren();
    const header = el('div', 'folder-detail-header');
    header.append(el('h2', '', folder.name || 'Business folder'), el('p', '', folder.path || 'Business folder'));
    detail.append(header);
    const metadata = el('div', 'folder-metadata');
    for (const [label, value] of [['Business unit', folder.business_unit], ['Division', folder.division], ['Department', folder.department], ['Region', folder.region], ['Organisation', folder.organisation], ['Your roles', roles(folder).join(', ') || (canManage(folder) ? 'owner' : 'Inherited access')]]) { const cell = el('div'); cell.append(el('span', 'metadata-label', label), document.createTextNode(text(value))); metadata.append(cell); }
    detail.append(metadata);
    const heading = el('div', 'access-section-heading'); heading.append(el('h3', '', 'People & permissions'));
    detail.append(heading);
    const grantsContainer = el('div', 'grant-list');
    detail.append(grantsContainer);
    loading(grantsContainer, 'Loading folder access…');
    const manage = canManage(folder);
    try {
      const [result] = await Promise.all([api(`/api/folders/${encodeURIComponent(id)}/grants`), manage ? ensureEmployees() : Promise.resolve()]);
      if (epoch !== state.epoch || request !== folderRequest) return;
      const grants = result.grants || [];
      grantsContainer.replaceChildren();
      if (!grants.length) grantsContainer.append(el('p', 'access-note', 'No explicit grants were returned. Inherited permissions may still apply.'));
      for (const grant of grants) {
        const row = el('div', 'grant-row');
        const employee = state.employees.find((entry) => sameId(entry.id, grant.employee_id));
        const name = grant.employee_name || employee?.name || grant.employee_id;
        const person = el('div', 'grant-person', name);
        person.append(el('small', '', text(grant.employee_id)));
        row.append(el('span', 'avatar grant-avatar', initials(name)), person, el('span', `role-chip ${['owner', 'delegate'].includes(grant.role) ? grant.role : ''}`, grant.role));
        const ownerRights = roles(folder).includes('owner') || sameId(folder.owner_id, state.user?.id);
        if (manage && (ownerRights || !['owner', 'delegate'].includes(grant.role))) {
          const remove = button('Remove', 'mini-button');
          remove.setAttribute('aria-label', `Remove ${grant.role} access for ${name}`);
          if (grant.role === 'owner' && grants.filter((entry) => entry.role === 'owner').length <= 1) {
            remove.disabled = true;
            remove.title = 'Every folder must retain at least one direct owner.';
          }
          remove.addEventListener('click', () => confirmRemoveGrant(folder, grant, name));
          row.append(remove);
        }
        grantsContainer.append(row);
      }
      if (manage) detail.append(grantForm(folder));
      detail.append(el('p', 'access-note', manage ? 'Owners and delegated owners can manage grants. At least one owner is required for every business folder; server-side policy protects the last owner.' : 'Only a folder owner or delegated owner can change grants. Contact the owner to request a different role.'));
    } catch (error) {
      if (epoch !== state.epoch || request !== folderRequest) return;
      grantsContainer.replaceChildren(el('p', 'access-note', error.status === 403 ? 'The access register is restricted to folder owners and delegated owners.' : error.message));
    }
  }
  function grantForm(folder) {
    const form = el('form', 'grant-form');
    form.append(el('h3', '', 'Grant folder access'));
    const employee = field('Employee', 'select', '', { required: true });
    option(employee.input, 'Select an employee', '');
    for (const person of [...state.employees].sort((a, b) => String(a.name).localeCompare(String(b.name)))) option(employee.input, `${person.name} · ${person.id}`, String(person.id));
    const role = field('Role', 'select', '', { required: true });
    const ownerRights = roles(folder).includes('owner') || sameId(folder.owner_id, state.user?.id);
    for (const [value, label] of [['reader', 'Reader'], ['maker', 'Maker'], ['checker', 'Checker'], ...(ownerRights ? [['delegate', 'Delegated owner'], ['owner', 'Owner']] : [])]) option(role.input, label, value);
    const submit = button('Grant access', 'button primary', 'plus'); submit.type = 'submit';
    const fields = el('div', 'grant-fields'); fields.append(employee.wrap, role.wrap, submit);
    const error = inlineError();
    form.append(fields, error);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      error.hidden = true;
      busy(form, async () => {
        await api(`/api/folders/${encodeURIComponent(folder.id)}/grants`, { method: 'PUT', body: { employee_id: employee.input.value, role: role.input.value, enabled: true } });
        notify('Folder access granted.');
        await refreshFolders();
      }).catch((err) => showInline(error, err));
    });
    return form;
  }
  function confirmRemoveGrant(folder, grant, name) {
    const body = openDialog('Remove folder access?', 'PERMISSION CHANGE');
    const form = el('form');
    form.append(el('p', 'dialog-notice', `Remove the ${grant.role} grant for ${name} on ${folder.path || folder.name}? Other explicit or inherited roles may still provide access.`));
    const error = inlineError();
    dialogActions(form, 'Remove grant', error, async () => {
      await api(`/api/folders/${encodeURIComponent(folder.id)}/grants`, { method: 'PUT', body: { employee_id: grant.employee_id, role: grant.role, enabled: false } });
      $('form-dialog').close(); notify('Access grant removed.');
      await refreshFolders().catch(alertError);
    });
    body.append(form);
  }
  function createFolder() {
    const body = openDialog('Create a business folder', 'FOLDER GOVERNANCE');
    const form = el('form');
    const name = field('Folder name', 'text', '', { required: true, placeholder: 'e.g. Operational policies', maxLength: 80, help: 'Use letters, numbers, spaces, hyphens, or underscores.' });
    name.input.pattern = '[A-Za-z0-9][A-Za-z0-9 _\\-]{0,79}';
    const parent = field('Parent folder', 'select', '', { required: true, help: 'Business roots are provisioned by the Data Office. New subfolders inherit the predefined business context of their parent.' });
    for (const folder of state.folders.filter(canManage)) option(parent.input, folder.path || folder.name, String(folder.id));
    if (state.folderId && canManage(folderById(state.folderId))) parent.input.value = String(state.folderId);
    const error = inlineError();
    form.append(name.wrap, parent.wrap, contextBox(parent.input), el('p', 'dialog-notice', 'Every folder must have an owner. The service assigns ownership and business context according to your account and parent folder.'));
    dialogActions(form, 'Create folder', error, async () => {
      if (!name.input.value.trim()) throw new Error('Enter a folder name.');
      await api('/api/folders', { method: 'POST', body: { name: name.input.value.trim(), parent_id: parent.input.value || null } });
      $('form-dialog').close(); notify('Business folder created.');
      await refreshFolders().catch(alertError);
    });
    body.append(form);
    name.input.focus();
  }

  async function refreshAudit() {
    const epoch = state.epoch;
    if (!state.events.length) loading($('audit-list'), 'Loading activity…');
    try {
      const data = await api('/api/audit');
      if (epoch !== state.epoch) return;
      state.events = (data.events || []).sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
      renderAudit();
    } catch (error) {
      if (epoch !== state.epoch) return;
      $('audit-result-count').textContent = '';
      empty($('audit-list'), 'Activity is unavailable', error.message, 'audit');
      throw error;
    }
  }
  function renderAudit() {
    const search = $('audit-search').value.toLowerCase().trim();
    const events = state.events.filter((event) => `${event.action || ''} ${event.actor_id || ''} ${event.target_id || ''} ${safeJSON(event.details)}`.toLowerCase().includes(search));
    $('audit-result-count').textContent = `${events.length} of ${state.events.length} loaded events`;
    renderStats();
    const list = $('audit-list');
    if (!events.length) return empty(list, search ? 'No matching activity' : 'No activity to show yet', search ? 'Try another action, employee ID, or record.' : 'Recorded actions will appear here as your workspace is used.', 'audit');
    list.replaceChildren();
    for (const event of events) {
      const row = el('article', 'activity-row');
      const kind = String(event.action || '').split('.')[0].split('_')[0];
      const kindIcon = { auth: 'user', chat: 'chat', mcp: 'search', document: 'file', ingestion: 'upload', folder: 'folder', access: 'folder', governance: 'check', approval: 'check' }[kind] || 'audit';
      const symbol = el('span', 'activity-icon'); symbol.dataset.kind = kind; symbol.append(icon(kindIcon));
      const body = el('div', 'activity-body');
      const heading = el('div', 'activity-heading');
      const date = el('time', '', formatDate(event.created_at, true));
      if (event.created_at) date.dateTime = event.created_at;
      heading.append(el('h3', '', text(event.action, 'Workspace activity').replaceAll('_', ' ').replaceAll('.', ' · ')), date);
      body.append(heading, el('p', '', `By ${text(event.actor_id, 'System')} · Record ${text(event.target_id, 'Workspace')}`));
      if (event.details && (typeof event.details !== 'object' || Object.keys(event.details).length)) {
        const details = el('details'); details.append(el('summary', '', 'View event details'), el('pre', 'audit-detail', safeJSON(event.details))); body.append(details);
      }
      row.append(symbol, body); list.append(row);
    }
  }

  $('review-scope').addEventListener('change', renderApprovals);
  $('login-form').addEventListener('submit', (event) => {
    event.preventDefault(); $('login-error').hidden = true;
    busy($('login-form'), async () => {
      const result = await api('/api/login', { method: 'POST', body: { employee_id: $('employee-id').value.trim(), password: $('password').value } });
      if (!result.user) throw new Error('The service did not return a signed-in employee. Please try again.');
      await enterWorkspace(result.user);
    }).catch((error) => showInline($('login-error'), error));
  });
  $('demo-user').addEventListener('change', () => { if ($('demo-user').value) { $('employee-id').value = $('demo-user').value; $('password').focus(); } });
  $('logout-button').addEventListener('click', () => busy($('logout-button'), async () => {
    await api('/api/logout', { method: 'POST', body: {} });
    showLogin();
    $('employee-id').focus();
  }).catch(alertError));
  $('menu-button').addEventListener('click', () => { if ($('sidebar').classList.contains('open')) closeNavigation(); else openNavigation(); });
  $('sidebar-overlay').addEventListener('click', () => { closeNavigation(); $('menu-button').focus(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('sidebar').classList.contains('open')) { closeNavigation(); $('menu-button').focus(); }
  });
  const mobileQuery = window.matchMedia('(max-width: 760px)');
  mobileQuery.addEventListener('change', () => { closeNavigation(); $('sidebar').inert = mobileQuery.matches; });
  $('sidebar').inert = mobileQuery.matches;
  $('dismiss-alert').addEventListener('click', () => { $('global-alert').hidden = true; });
  $('close-dialog').addEventListener('click', () => { if (!dialogBusy) $('form-dialog').close(); });
  $('form-dialog').addEventListener('cancel', (event) => { if (dialogBusy) event.preventDefault(); });
  $('chat-form').addEventListener('submit', (event) => { event.preventDefault(); submitQuestion(); });
  $('chat-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitQuestion(); } });
  $('chat-input').addEventListener('input', () => { $('chat-input').style.height = 'auto'; $('chat-input').style.height = `${Math.min($('chat-input').scrollHeight, 180)}px`; });
  document.querySelectorAll('[data-question]').forEach((node) => node.addEventListener('click', () => { if (!state.chatBusy) { $('chat-input').value = node.dataset.question; $('chat-input').focus(); } }));
  $('new-conversation').addEventListener('click', () => { resetConversation(); $('chat-input').focus(); });
  $('document-search').addEventListener('input', renderDocuments);
  $('document-status').addEventListener('change', renderDocuments);
  $('refresh-documents').addEventListener('click', () => busy($('refresh-documents'), refreshDocuments).catch(alertError));
  $('refresh-approvals').addEventListener('click', () => busy($('refresh-approvals'), refreshDocuments).catch(alertError));
  $('refresh-ingestion').addEventListener('click', () => busy($('refresh-ingestion'), refreshIngestion).catch(alertError));
  $('ingestion-filter').addEventListener('change', renderIngestion);
  $('ingestion-search').addEventListener('input', renderIngestion);
  $('upload-button').addEventListener('click', openUpload);
  $('home-upload').addEventListener('click', openUpload);
  $('home-refresh').addEventListener('click', () => busy($('home-refresh'), refreshDocuments).catch(alertError));
  $('home-ask').addEventListener('submit', (event) => {
    event.preventDefault();
    const question = $('home-question').value.trim();
    if (!question || state.chatBusy) return;
    $('home-question').value = '';
    location.hash = 'ask';
    $('chat-input').value = question;
    submitQuestion();
  });
  $('rail-logout').addEventListener('click', () => $('logout-button').click());
  // Motion helpers. Everything is skipped when the system asks for reduced motion.
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function placeIndicator(indicator, active) {
    if (!indicator) return;
    if (!active || !active.offsetParent) { indicator.style.opacity = '0'; return; }
    indicator.style.height = `${active.offsetHeight}px`;
    indicator.style.transform = `translateY(${active.offsetTop}px)`;
    indicator.style.opacity = '1';
    // The first placement jumps into position; later moves glide.
    if (!indicator.classList.contains('ready')) requestAnimationFrame(() => indicator.classList.add('ready'));
  }
  function moveIndicators() {
    placeIndicator($('nav-indicator'), document.querySelector('#primary-nav .nav-item.active'));
    placeIndicator($('rail-indicator'), document.querySelector('.rail-nav .rail-btn.active'));
  }
  function countUp(node, value, format) {
    node.textContent = format(value);  // final value first: correct even if frames never run (background tab)
    if (reducedMotion.matches || value < 2 || document.hidden) return;
    node.textContent = format(0);
    const start = performance.now();
    const duration = 700;
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      node.textContent = format(Math.round(value * (1 - Math.pow(1 - t, 3))));
      if (t < 1 && node.isConnected) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  window.addEventListener('resize', () => requestAnimationFrame(moveIndicators));
  // Desktop navigation: full panel, or collapsed to the icon rail. The choice is remembered per browser.
  function setNavCollapsed(collapsed, save = true) {
    $('workspace').classList.toggle('nav-collapsed', collapsed);
    requestAnimationFrame(moveIndicators);
    window.setTimeout(moveIndicators, 460);  // after the fold transition settles
    if (save) { try { localStorage.setItem('kb.navCollapsed', collapsed ? '1' : '0'); } catch { /* storage may be blocked */ } }
  }
  $('nav-collapse').addEventListener('click', () => { setNavCollapsed(true); $('nav-expand').focus(); });
  $('nav-expand').addEventListener('click', () => { setNavCollapsed(false); $('nav-collapse').focus(); });
  try { setNavCollapsed(localStorage.getItem('kb.navCollapsed') === '1', false); } catch { /* default: expanded */ }
  $('create-folder-button').addEventListener('click', createFolder);
  $('refresh-access').addEventListener('click', () => busy($('refresh-access'), refreshFolders).catch(alertError));
  $('refresh-audit').addEventListener('click', () => busy($('refresh-audit'), refreshAudit).catch(alertError));
  $('audit-search').addEventListener('input', renderAudit);
  window.addEventListener('hashchange', route);

  api('/api/me').then((data) => data.user ? enterWorkspace(data.user) : showLogin()).catch((error) => showLogin(error.status === 401 ? '' : error.message));
})();
