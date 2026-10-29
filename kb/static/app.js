'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, folders: [], documents: [], employees: [], events: [], view: 'ask', folderId: null, health: null, epoch: 0, chatBusy: false, chatGeneration: 0 };
  const viewNames = { ask: 'Ask knowledge', library: 'Document library', approvals: 'Approvals', ingestion: 'Ingestion monitor', pdfdebug: 'PDF object inspector', access: 'Folders & access', audit: 'Activity log' };
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
  function statusKey(doc) { return String(doc.status || 'unknown').toLowerCase().replace(/[\s-]+/g, '_'); }
  function isPending(doc) { return doc.processing_request?.status === 'pending' || ['pending', 'pending_approval', 'staged', 'awaiting_approval'].includes(statusKey(doc)); }
  function isFailed(doc) { return ['failed', 'error', 'processing_failed'].includes(statusKey(doc)); }
  function statusBadge(doc) {
    const key = statusKey(doc);
    const labels = { pending: 'Pending approval', pending_approval: 'Pending approval', staged: 'Pending approval', awaiting_approval: 'Pending approval', indexed: 'Indexed', approved: 'Approved', ready: 'Ready', processing: 'Processing', ingesting: 'Processing', rejected: 'Rejected', failed: 'Failed', error: 'Failed', archived: 'Archived' };
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
    const timer = window.setTimeout(() => controller.abort(), path === '/chat' ? 180000 : 60000);
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
    resetConversation();
    state.folderId = null;
    empty($('folder-detail'), 'A place for every permission', 'Select a folder to view its business context and access grants.', 'folder');
    $('mode-badge').textContent = 'Checking service mode';
    $('mode-badge').className = 'mode-badge';
    $('mode-title').textContent = 'Service mode';
    $('mode-description').textContent = 'Service status is being checked. No external inference is assumed.';
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
    const next = Object.hasOwn(viewNames, requested) ? requested : 'ask';
    const changed = state.view !== next;
    state.view = next;
    for (const [key, title] of Object.entries(viewNames)) {
      $(`view-${key}`).hidden = key !== next;
      const item = document.querySelector(`[data-view="${key}"]`);
      item.classList.toggle('active', key === next);
      if (key === next) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
      if (key === next) $('breadcrumb-current').textContent = title;
    }
    document.title = `${viewNames[next]} | Knowledge`;
    closeNavigation();
    if (changed) {
      $('main-content').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    if (next === 'library') renderDocuments();
    if (next === 'approvals') renderApprovals();
    if (next === 'pdfdebug' && !$('pdfdebug-content').dataset.loaded) loadPdfChoices().catch(alertError);
    window.clearInterval(ingestionTimer); ingestionTimer = null;
    if (next === 'ingestion') { refreshIngestion().catch(alertError); ingestionTimer = window.setInterval(() => { if (state.user && state.view === 'ingestion' && !document.hidden) refreshIngestion().catch(() => {}); }, 5000); }
    if (next === 'access') { renderFolders(); if (state.folderId) selectFolder(state.folderId); }
    if (next === 'audit') refreshAudit().catch(alertError);
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

  function resetConversation() {
    if (state.chatBusy) return;
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
    avatar.append(kind === 'user' ? document.createTextNode(initials(state.user?.name)) : icon('spark'));
    const time = el('time', '', new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date()));
    time.dateTime = new Date().toISOString();
    header.append(avatar, document.createTextNode(kind === 'user' ? 'You' : 'Knowledge'), time);
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
    heading.append(icon('book'), document.createTextNode(`${citations.length} supporting source${citations.length === 1 ? '' : 's'}`));
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
  function appendTrace(node, trace) {
    if (!Array.isArray(trace) || !trace.length) return;
    const details = el('details', 'trace-details');
    details.append(el('summary', '', `Operational trace · ${trace.length} tool call${trace.length === 1 ? '' : 's'}`));
    const list = el('div', 'trace-list');
    trace.forEach((step, index) => {
      const item = el('div', 'trace-step');
      const heading = el('div', 'trace-step-title');
      heading.append(el('strong', '', `${String(index + 1).padStart(2, '0')}  ${text(step.tool, 'Tool call')}`));
      if (Number.isFinite(Number(step.duration_ms)) && step.duration_ms !== null) heading.append(el('span', '', `${Math.round(Number(step.duration_ms))} ms`));
      item.append(heading);
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
    addMessage('user', message);
    $('chat-input').value = '';
    $('chat-input').style.height = '';
    $('chat-input').disabled = true;
    $('send-button').disabled = true;
    $('new-conversation').disabled = true;
    const thinking = el('div', 'thinking', 'Checking permissions and finding supporting sources…');
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

  function updateCounts() {
    const count = state.documents.filter(isPending).length;
    $('approval-count').textContent = String(count);
    $('approval-count').hidden = count === 0;
    $('upload-button').disabled = !state.folders.some(canMake);
    $('upload-button').title = $('upload-button').disabled ? 'Maker, owner, or delegated-owner access is required for uploads.' : 'Upload a document for approval';
    const manageable = state.folders.filter(canManage);
    $('create-folder-button').disabled = manageable.length === 0;
    $('create-folder-button').title = manageable.length ? 'Create a subfolder with inherited business context' : 'Folder-owner or delegated-owner access is required to create a subfolder';
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
      empty($('document-list'), search || status ? 'No matching documents' : 'Your library starts here', search || status ? 'Try a different search or status filter.' : 'Approved, permitted documents will appear here. Makers can upload a document to start the review process.', 'book');
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
      const file = el('span', 'file-icon'); file.append(icon('file'));
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
      await api('/api/documents', { method: 'POST', body: payload });
      $('form-dialog').close();
      notify('Document uploaded and submitted for checker approval.');
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
  function renderApprovals() {
    const docs = state.documents.filter(isPending).sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
    const list = $('approval-list');
    if (!docs.length) return empty(list, 'You’re all caught up', 'There are no documents awaiting approval in the folders you can access.', 'check');
    list.replaceChildren();
    for (const doc of docs) {
      const card = el('article', 'approval-card');
      const top = el('div', 'approval-top');
      const file = el('span', 'file-icon'); file.append(icon('file'));
      const copy = el('div'); copy.append(el('h3', '', doc.title || 'Untitled document'), el('p', '', doc.folder_path || folderById(doc.folder_id)?.path || 'Business folder'));
      top.append(file, copy, statusBadge(doc));
      const context = el('div', 'approval-context');
      for (const [label, value] of [['Maker', doc.maker_id], ['Submitted', formatDate(doc.created_at)], ['Version', `v${doc.version || 1}`], ['Latest date', formatDate(doc.latest_date)]]) { const item = el('div'); item.append(el('strong', '', label), document.createTextNode(text(value))); context.append(item); }
      const details = button('View record & metadata', 'mini-button'); details.addEventListener('click', () => openDocumentDetails(doc)); context.append(details);
      card.append(top, context);
      const request = doc.processing_request;
      const own = sameId(doc.maker_id, state.user?.id) || sameId(request?.maker_id, state.user?.id);
      if (!request || request.status !== 'pending') {
        card.append(el('p', 'approval-lock-note', 'This legacy record needs a fresh maker submission before review. A checker ID alone is not approval.'));
      } else if (own || !canCheck(folderById(doc.folder_id))) {
        card.append(el('p', 'approval-lock-note', own ? 'This is your upload. A different authorised checker must review it.' : 'Checker or folder-owner access is required to review this document.'));
      } else {
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


  const stageLabels = { no_request: 'No request', awaiting_checker: 'Awaiting checker', queued: 'Queued', processing: 'Processing', indexed: 'Indexed', failed: 'Failed', cancelled: 'Cancelled', rejected: 'Rejected' };
  const stageOrder = ['awaiting_checker', 'queued', 'processing', 'indexed', 'failed', 'rejected', 'cancelled'];
  const stepLabels = { source_fetch: 'Reading source', raw_storage: 'Saving source', parsing: 'Parsing text and tables', visual_enrichment: 'Reading images with VL', asset_storage: 'Saving images', parsed_storage: 'Saving parsed content', chunking: 'Chunking', curated_storage: 'Saving chunks', embedding_guardrails: 'Checking text before embedding', text_embeddings: 'Generating embeddings', indexing: 'Writing search index', publication: 'Publishing', completed: 'Complete' };
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
    if (!items.length) { empty($('ingestion-list'), 'Nothing to show', filter || search ? 'No documents match this filter.' : 'No ingestion activity in your folders yet.', 'upload'); return; }
    const scroll = el('div', 'table-scroll');
    scroll.tabIndex = 0;
    const table = el('table');
    const head = el('thead'); const tr = el('tr');
    for (const label of ['Document', 'Stage', 'Request', 'Submitted', 'Actions']) { const th = el('th', '', label); th.scope = 'col'; tr.append(th); }
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
      if (item.can_retry) {
        const retry = button('Retry', 'mini-button');
        retry.addEventListener('click', () => retryIngestion(item, retry));
        actions.append(retry);
      }
      actionsCell.append(actions);
      row.append(nameCell, stageCell, reqCell, dateCell, actionsCell);
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
    const data=await api('/api/ingestion');
    if (host.dataset.loaded) return;
    host.replaceChildren(el('p','','Choose an ingestion run to inspect its PDF:'));
    for(const item of data.items.filter(i=>i.request_id && /\.pdf$/i.test(i.title))) {
      const choose=button(`${item.title} · ${item.stage}`, 'button secondary');
      choose.addEventListener('click',()=>openPdfInspector(item).catch(alertError));host.append(choose);
    }
  }
  async function openPdfInspector(item) {
    const host = $('pdfdebug-content');
    host.dataset.loaded='1';
    host.replaceChildren(el('p', '', 'Loading PDF geometry…'));
    const data = await api(`/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout`);
    host.replaceChildren(el('h2', '', item.title));
    if (!state.user?.pii_unmasked) {
      host.append(el('p', 'inline-error', 'Viewing original PDF content requires unmasked-content permission. Use an authorized account such as Alex Demo1 (E001). Geometry remains available in ingestion Details.'));
      return;
    }
    if (data.coverage) host.append(el('p', 'muted', data.coverage));
    const pages = [...new Set(data.objects.map(o => o.page))].sort((a,b) => a-b);
    if (!pages.length) { host.append(el('p', '', 'No boxes were recorded for this run.')); return; }
    const toolbar = el('div','pdf-debug-toolbar');
    const pageSelect = el('select'); pageSelect.setAttribute('aria-label','PDF page');
    pages.forEach(n => { const o=el('option','',`Page ${n}`); o.value=n; pageSelect.append(o); });
    const layer = el('select'); layer.setAttribute('aria-label','Object layer');
    [['all','All boxes'],['layout','Docling layout'],['crops','Processing crops'],['none','Original page only']].forEach(([value,label]) => {const o=el('option','',label);o.value=value;layer.append(o);});
    const zoom = el('select'); zoom.setAttribute('aria-label','Zoom');
    [100,150,200].forEach(n => {const o=el('option','',`${n}%`);o.value=n;zoom.append(o);});
    toolbar.append(pageSelect,layer,zoom);host.append(toolbar);
    const split=el('div','pdf-debug-split'), scroll=el('div','pdf-debug-scroll'), stage=el('div','pdf-debug-stage'), side=el('div','pdf-debug-objects');
    scroll.append(stage);split.append(scroll,side);host.append(split);
    let selected=null;
    const draw = () => {
      stage.replaceChildren();side.replaceChildren();stage.style.width=`${zoom.value}%`;
      const objects=data.objects.filter(o=>String(o.page)===pageSelect.value);
      const b=objects[0].bbox;
      const img=el('img');img.alt=`Original PDF page ${pageSelect.value}`;
      img.src=`/api/processing-requests/${encodeURIComponent(item.request_id)}/pdf-layout/page/${pageSelect.value}`;
      img.addEventListener('error',()=>{side.prepend(el('p','inline-error','Could not load the original PDF page. Check access and storage availability.'));});
      stage.append(img);
      const ns='http://www.w3.org/2000/svg'; const svg=document.createElementNS(ns,'svg');
      svg.setAttribute('viewBox',`0 0 ${b.page_width} ${b.page_height}`);svg.setAttribute('preserveAspectRatio','none');svg.classList.add('pdf-debug-overlay');stage.append(svg);
      objects.forEach((obj,i)=>{
        const isLayout=obj.extractor==='docling-layout';
        if(layer.value==='none'||(layer.value==='layout'&&!isLayout)||(layer.value==='crops'&&isLayout))return;
        const box=obj.bbox,color=isLayout?'#2563eb':'#d97706';
        const rect=document.createElementNS(ns,'rect');
        for(const [k,v] of Object.entries({x:box.left,y:box.top,width:box.right-box.left,height:box.bottom-box.top,fill:selected===obj.object_id?'#fde047':'transparent','fill-opacity':selected===obj.object_id?'.22':'0',stroke:color,'stroke-width':selected===obj.object_id?3:1}))rect.setAttribute(k,v);
        rect.style.pointerEvents='all';rect.style.cursor='pointer';rect.addEventListener('click',()=>{selected=obj.object_id;draw();});svg.append(rect);
        const label=document.createElementNS(ns,'text');label.setAttribute('x',box.left+2);label.setAttribute('y',Math.min(box.top+11,b.page_height-2));label.setAttribute('font-size','10');label.setAttribute('fill',color);label.setAttribute('stroke','white');label.setAttribute('stroke-width','2');label.setAttribute('paint-order','stroke');label.textContent=`${i+1} ${obj.object_kind}`;svg.append(label);
        const entry=button(`${i+1}. ${obj.object_kind}`, 'button secondary');entry.addEventListener('click',()=>{selected=obj.object_id;draw();});
        const card=el('div','pdf-debug-object');if(selected===obj.object_id)card.classList.add('selected');
        card.append(entry,el('p','muted',obj.extractor),el('code','',obj.object_id),el('pre','',JSON.stringify(box,null,2)));side.append(card);
      });
    };
    [pageSelect,layer,zoom].forEach(control=>control.addEventListener('change',draw));draw();
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
        $('form-dialog').close(); location.hash = 'pdfdebug';
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
      node.classList.toggle('selected', sameId(folder.id, state.folderId));
      node.setAttribute('aria-pressed', String(sameId(folder.id, state.folderId)));
      const copy = el('span', '', folder.name || folder.path);
      copy.append(el('small', '', folder.path || folder.business_unit || 'Business folder'));
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
      empty($('audit-list'), 'Activity is unavailable', error.message, 'audit');
      throw error;
    }
  }
  function renderAudit() {
    const search = $('audit-search').value.toLowerCase().trim();
    const events = state.events.filter((event) => `${event.action || ''} ${event.actor_id || ''} ${event.target_id || ''} ${safeJSON(event.details)}`.toLowerCase().includes(search));
    const list = $('audit-list');
    if (!events.length) return empty(list, search ? 'No matching activity' : 'No activity to show yet', search ? 'Try another action, employee ID, or record.' : 'Recorded actions will appear here as your workspace is used.', 'audit');
    list.replaceChildren();
    for (const event of events) {
      const row = el('article', 'activity-row');
      const symbol = el('span', 'activity-icon'); symbol.append(icon('audit'));
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
  $('create-folder-button').addEventListener('click', createFolder);
  $('refresh-access').addEventListener('click', () => busy($('refresh-access'), refreshFolders).catch(alertError));
  $('refresh-audit').addEventListener('click', () => busy($('refresh-audit'), refreshAudit).catch(alertError));
  $('audit-search').addEventListener('input', renderAudit);
  window.addEventListener('hashchange', route);

  api('/api/me').then((data) => data.user ? enterWorkspace(data.user) : showLogin()).catch((error) => showLogin(error.status === 401 ? '' : error.message));
})();
