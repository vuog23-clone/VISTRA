/* Same-origin demo client. No keys, credentials, or model calls live in the browser. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const icon = name => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('icon'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`); svg.append(use); return svg;
  };
  const state = { session: null, sessions: [], detail: null, busy: false, loading: false, ready: false, request: 0 };
  const storage = {
    get() { try { return localStorage.getItem('vistra.active-session'); } catch { return null; } },
    set(value) { try { localStorage.setItem('vistra.active-session', value); } catch {} }
  };
  const newID = () => crypto.randomUUID();
  let toastTimer;
  function toast(message) {
    $('toast').textContent = message; $('toast').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3000);
  }
  function showError(message) { $('error-text').textContent = message; $('error-banner').hidden = false; }
  function clearError() { $('error-banner').hidden = true; }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, credentials: 'same-origin',
      headers: { 'X-VISTRA-Demo': '1', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.detail || 'The request could not be completed.');
    return data;
  }
  function scrollChat(force = true) {
    const area = $('chat-area');
    if (force) area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
  }
  function syncControls() {
    const blocked = state.busy || state.loading;
    $('send-button').disabled = blocked || !state.ready || !$('message-input').value.trim();
    $('message-input').disabled = blocked;
    $('new-chat').disabled = blocked;
    $('clear-chat').disabled = blocked || !state.detail?.messages?.length;
    $('reload-chat').disabled = blocked;
    $('retrieval-limit').disabled = blocked;
    document.querySelectorAll('.session-item,.session-delete,.suggestion,.forget-button,.clarification-choice').forEach(button => { button.disabled = blocked; });
  }
  function renderSessions() {
    const list = $('session-list'); list.replaceChildren();
    if (!state.sessions.length) list.append(el('p', 'sidebar-empty', 'Your travel stories will live here.'));
    for (const session of state.sessions) {
      const row = el('div', `session-row${session.id === state.session ? ' active' : ''}`);
      const button = el('button', `session-item${session.id === state.session ? ' active' : ''}`);
      button.dataset.sessionId = session.id;
      button.type = 'button'; button.title = session.title;
      if (session.id === state.session) button.setAttribute('aria-current', 'page');
      button.append(icon('chat'), el('span', '', session.destination || session.title));
      button.addEventListener('click', () => openSession(session.id));
      const remove = el('button', 'icon-button session-delete');
      remove.type = 'button'; remove.title = 'Delete conversation';
      remove.setAttribute('aria-label', `Delete conversation: ${session.destination || session.title}`);
      remove.append(icon('trash')); remove.addEventListener('click', () => confirmDelete(session.id));
      row.append(button, remove); list.append(row);
    }
    const active = state.sessions.find(session => session.id === state.session);
    $('conversation-title').textContent = active?.destination ? `A little closer to ${active.destination}` : active?.title || 'A new beginning';
    syncControls();
  }
  function safeURL(url) {
    try { const parsed = new URL(url); return parsed.protocol === 'https:' && !parsed.username && !parsed.password; } catch { return false; }
  }
  function markdown(container, content, sources) {
    if (!window.marked || !window.DOMPurify) { container.textContent = content; return; }
    const allowed = new Set((sources || []).map(source => source.url).filter(safeURL));
    const fragment = DOMPurify.sanitize(marked.parse(content, { breaks: true, gfm: true }), {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ['p','br','strong','em','code','pre','h1','h2','h3','h4','ul','ol','li','blockquote','table','thead','tbody','tr','th','td','a','hr','del'],
      ALLOWED_ATTR: ['href','title','start']
    });
    for (const link of fragment.querySelectorAll('a')) {
      if (!allowed.has(link.getAttribute('href'))) link.removeAttribute('href');
      else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    }
    for (const table of fragment.querySelectorAll('table')) {
      const wrapper = el('div', 'table-scroll'); table.replaceWith(wrapper); wrapper.append(table);
    }
    container.replaceChildren(fragment);
  }
  function placeGallery(cards) {
    const box = el('section', 'place-gallery'); box.setAttribute('aria-label', 'Retrieved places and photos');
    const heading = el('div', 'gallery-heading'); heading.append(el('h3', '', 'Places found'), el('span', 'gallery-count', String(cards.length)));
    box.append(heading, el('p', 'gallery-note', 'Saved place details. Hours and prices may have changed.'));
    const grid = el('div', 'gallery-grid'); grid.tabIndex = 0; grid.setAttribute('aria-label', 'Scroll to explore place matches');
    const row = (list, label, value) => { const field = el('div', 'place-field'); field.append(el('dt', '', label), el('dd', '', value)); list.append(field); };
    for (const card of cards) {
      const tile = el('article', 'place-card'); const photo = el('div', 'place-photo');
      const placeholder = () => { const fallback = el('div', 'photo-unavailable'); fallback.append(icon('pin'), el('span', '', 'Photo unavailable')); photo.replaceChildren(fallback); };
      if (typeof card.image_url === 'string' && /^\/api\/photos\/[A-Za-z0-9_.:%-]+\/\d+\/\d+$/.test(card.image_url)) {
        const image = el('img'); image.src = card.image_url; image.alt = `${card.name} — indexed place photo`; image.loading = 'lazy'; image.decoding = 'async';
        image.addEventListener('error', placeholder, {once:true}); photo.append(image);
      } else placeholder();
      tile.append(photo); const info = el('div', 'place-info');
      const top = el('div', 'place-category'); top.append(el('span', '', card.category || 'Place'));
      if (card.source_id) top.append(el('span', 'place-source', `[${card.source_id}]`));
      info.append(top, el('h4', '', card.name));
      const rating = typeof card.rating === 'number' ? `★ ${card.rating.toFixed(1)}${Number.isInteger(card.review_count) ? ` · ${card.review_count.toLocaleString()} reviews` : ''}` : 'Rating not listed';
      info.append(el('p', 'place-rating', rating));
      if (card.address) { const address = el('p', 'place-address', card.address); address.title = card.address; info.append(address); }
      else if (card.province) info.append(el('p', 'place-address', card.province));
      const details = el('details', 'place-details'); details.append(el('summary', '', 'Place details'));
      const fields = el('dl');
      const price = card.price_range || {};
      const formatMoney = value => `${Number(value).toLocaleString()}${price.currency ? ` ${price.currency}` : ''}`;
      const hasMin = typeof price.min === 'number', hasMax = typeof price.max === 'number';
      row(fields, 'Price range', hasMin && hasMax ? `${formatMoney(price.min)} – ${formatMoney(price.max)}` : hasMin ? `From ${formatMoney(price.min)}` : hasMax ? `Up to ${formatMoney(price.max)}` : 'Not listed');
      if (card.province) row(fields, 'Location', card.province);
      if (typeof card.distance_km === 'number') row(fields, 'Straight-line distance', `${card.distance_km.toFixed(1)} km`);
      if (typeof card.latitude === 'number' && typeof card.longitude === 'number') row(fields, 'Coordinates', `${card.latitude.toFixed(5)}, ${card.longitude.toFixed(5)}`);
      const hours = card.opening_hours || {};
      for (const [day, label] of [['monday','Mon'],['tuesday','Tue'],['wednesday','Wed'],['thursday','Thu'],['friday','Fri'],['saturday','Sat'],['sunday','Sun']]) {
        const windows = hours[day];
        const text = !Array.isArray(windows) ? 'Unknown' : windows.length === 0 ? 'Closed in saved schedule' : windows.map(window => window.open && window.close ? `${window.open}–${window.close}` : 'Unknown').join(', ');
        row(fields, label, text);
      }
      if (hours.timezone) row(fields, 'Time zone', hours.timezone);
      details.append(fields); info.append(details);
      if (safeURL(card.maps_url)) { const maps = el('a', 'place-map-link', 'View on Google Maps ↗'); maps.href = card.maps_url; maps.target = '_blank'; maps.rel = 'noopener noreferrer'; info.append(maps); }
      tile.append(info); grid.append(tile);
    }
    box.append(grid); return box;
  }
  function weatherPanel(weather) {
    const box = el('section', 'weather-panel'); box.setAttribute('aria-label', 'Weather for your trip');
    const heading = el('div', 'gallery-heading'); heading.append(el('h3', '', 'Weather for your trip'));
    if (weather.location) heading.append(el('span', 'gallery-count', weather.location));
    box.append(heading, el('p', 'gallery-note', weather.note));
    if (weather.start_date && weather.end_date) box.append(el('p', 'weather-range', `${weather.start_date} → ${weather.end_date}${weather.timezone ? ` · ${weather.timezone}` : ''}`));
    const grid = el('div', 'weather-days');
    grid.tabIndex = 0; grid.setAttribute('aria-label', 'Daily weather forecasts');
    const value = (number, unit) => typeof number === 'number' ? `${number}${unit}` : 'Unknown';
    for (const day of weather.days || []) {
      const tile = el('article', 'weather-day');
      tile.append(el('strong', '', day.date), el('p', 'weather-condition', day.condition),
        el('p', 'weather-temperature', `${value(day.temperature_min_c, '°')} – ${value(day.temperature_max_c, '°C')}`),
        el('p', '', `Rain chance: ${value(day.precipitation_probability_pct, '%')}`),
        el('p', '', `Precipitation: ${value(day.precipitation_mm, ' mm')}`),
        el('p', '', `Max wind: ${value(day.wind_max_kmh, ' km/h')}`));
      grid.append(tile);
    }
    if (grid.children.length) box.append(grid);
    const attribution = el('p', 'weather-attribution');
    const link = el('a', '', 'Weather data: Open-Meteo'); link.href = 'https://open-meteo.com/en/docs'; link.target = '_blank'; link.rel = 'noopener noreferrer'; attribution.append(link);
    if (weather.fetched_at) attribution.append(document.createTextNode(` · Fetched ${weather.fetched_at}`));
    box.append(attribution); return box;
  }
  function planningPanel(planning) {
    const box = el('section', 'weather-panel'); box.setAttribute('aria-label', 'Itinerary planning checks');
    const status = {verified:'Checks passed against tool data',partial:'Itinerary draft · some details remain unverified',
      failed:'The draft did not pass itinerary validation',needs_clarification:'One quick choice, then we’ll continue'};
    box.append(el('h3', 'planning-heading', status[planning.status] || 'Itinerary planning'));
    if (planning.status === 'needs_clarification' && planning.clarification_options?.length) {
      const choices = el('div', 'clarification-choices');
      for (const choice of planning.clarification_options.slice(0, 4)) {
        if (typeof choice.label !== 'string' || typeof choice.reply !== 'string') continue;
        const button = el('button', 'clarification-choice', choice.label);
        button.type = 'button'; button.disabled = state.busy || state.loading;
        button.addEventListener('click', () => sendMessage(choice.reply));
        choices.append(button);
      }
      box.append(choices);
    }
    const messages = [...new Set((planning.issues || []).map(issue => issue.message))];
    if (messages.length) { const list = el('ul', 'planning-issues'); messages.slice(0,8).forEach(message => list.append(el('li','',message))); box.append(list); }
    if (planning.tool_trace?.length) {
      const details = el('details', 'place-details'); details.append(el('summary', '', 'Tools and evidence notebook'));
      const list = el('ol', 'planning-trace'); planning.tool_trace.forEach(line => list.append(el('li','',line))); details.append(list); box.append(details);
    }
    return box;
  }
  function addMessage(role, content, sources = [], note = '', cards = [], weather = null, planning = null) {
    $('welcome').hidden = true;
    const row = el('article', `message ${role}`);
    const main = el('div', 'message-main');
    if (role !== 'user') { const avatar = el('div', 'message-avatar'); avatar.append(icon('compass')); row.append(avatar); }
    main.append(el('div', 'message-name', role === 'user' ? 'You' : 'VISTRA'));
    const body = el('div', 'message-body');
    if (role === 'user') body.textContent = content; else markdown(body, content, sources);
    main.append(body);
    if (role === 'assistant') {
      const usable = sources.filter(source => safeURL(source.url));
      if (usable.length) {
        const links = el('div', 'sources'); links.setAttribute('aria-label', 'Travel sources');
        usable.forEach(source => {
          const link = el('a', 'source-link'); link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.append(el('span', 'source-id', source.id), el('span', '', source.name)); links.append(link);
        }); main.append(links);
      }
      const tools = el('div', 'message-tools');
      const copy = el('button', 'icon-button'); copy.type = 'button'; copy.setAttribute('aria-label', 'Copy reply'); copy.title = 'Copy reply'; copy.append(icon('copy'));
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(content); toast('Reply copied'); } catch { toast('Copy is unavailable in this browser'); } });
      tools.append(copy); if (note) tools.append(el('span', 'turn-note', note)); main.append(tools);
      if (planning) main.append(planningPanel(planning));
      if (weather) main.append(weatherPanel(weather));
      if (cards.length) main.append(placeGallery(cards));
    }
    row.append(main); $('messages').append(row); scrollChat(); return row;
  }
  function thinking() {
    const row = el('article', 'message assistant'); const avatar = el('div', 'message-avatar'); avatar.append(icon('compass'));
    const main = el('div', 'message-main'); main.append(el('div', 'message-name', 'VISTRA'));
    const line = el('div', 'thinking'); line.setAttribute('role', 'status');
    const dots = el('span', 'thinking-dots'); dots.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 3; i++) dots.append(el('span'));
    const label = el('span', '', 'Remembering the conversation'); line.append(dots, label); main.append(line); row.append(avatar, main); $('messages').append(row);
    const body = el('div', 'message-body streaming'); main.append(body);
    scrollChat(); return { row, label, body };
  }
  function renderMemory() {
    const detail = state.detail || { trip_state: {}, memories: [], summary: '' };
    const fields = [['destination','Destination'],['dates','When'],['budget','Budget'],['travelers','Travelers'],['goal','The plan'],['pending_clarification','Still to decide']];
    const cards = $('trip-details'); cards.replaceChildren();
    for (const [key, label] of fields) {
      if (!detail.trip_state?.[key]) continue;
      const card = el('div', 'detail-card'); card.append(el('span', '', label), el('p', '', String(detail.trip_state[key]))); cards.append(card);
    }
    if (detail.trip_state?.requirements?.length) {
      const card = el('div', 'detail-card'); card.append(el('span', '', 'What matters'), el('p', '', detail.trip_state.requirements.join('\n'))); cards.append(card);
    }
    $('session-memory-empty').hidden = Boolean(cards.children.length);
    $('summary-block').hidden = !detail.summary; $('conversation-summary').textContent = detail.summary || '';
    const list = $('preference-list'); list.replaceChildren();
    const labels = { diet:'Food preferences', pace:'Your pace', interests:'What you love', lodging:'Places to stay', transport:'Getting around', language:'Language', home_location:'Home base', accessibility:'Accessibility', travel_style:'Travel style' };
    const memories = detail.memories || [];
    for (const memory of memories) {
      const card = el('div', 'preference-card'); card.append(el('span', 'preference-label', memory.kind === 'trip' ? 'Saved trip' : labels[memory.key] || 'Preference'), el('p', '', memory.content));
      const remove = el('button', 'icon-button forget-button'); remove.type = 'button'; remove.title = 'Forget this detail'; remove.setAttribute('aria-label', `Forget ${labels[memory.key] || 'saved trip'}`); remove.append(icon('close'));
      remove.addEventListener('click', async () => {
        if (state.busy) return;
        state.busy = true; syncControls();
        try { await api(`/api/memory/${encodeURIComponent(memory.kind)}/${encodeURIComponent(memory.key)}`, { method:'DELETE' }); await refreshDetail(); toast('Saved detail removed'); }
        catch (error) { showError(error.message); }
        finally { state.busy = false; syncControls(); }
      }); card.append(remove); list.append(card);
    }
    $('longterm-memory-empty').hidden = memories.length > 0;
    $('memory-count').textContent = memories.length; $('memory-count').hidden = memories.length === 0;
    syncControls();
  }
  async function refreshDetail() {
    const session = state.session; const detail = await api(`/api/sessions/${encodeURIComponent(session)}`);
    if (state.session === session) { state.detail = detail; renderMemory(); }
    return detail;
  }
  async function refreshSessions() { state.sessions = (await api('/api/sessions')).sessions; renderSessions(); }
  let deleteTarget = null;
  function confirmDelete(session) {
    if (state.busy || state.loading || !state.ready) return;
    deleteTarget = session;
    const item = state.sessions.find(item => item.id === session);
    $('delete-chat-name').textContent = item?.title || 'This conversation';
    $('delete-chat-dialog').showModal();
  }
  async function deleteConversation() {
    const session = deleteTarget;
    if (!session || state.busy || state.loading) return;
    $('delete-chat-dialog').close(); deleteTarget = null;
    clearError(); state.busy = true; syncControls();
    try {
      await api(`/api/sessions/${encodeURIComponent(session)}`, { method:'DELETE' });
      state.sessions = state.sessions.filter(item => item.id !== session); renderSessions();
      state.busy = false;
      if (state.session === session) await openSession(newID());
      toast('Conversation deleted. Your preferences are kept.');
    } catch (error) { showError(error.message); }
    finally { state.busy = false; syncControls(); }
  }
  function syncPanels() {
    const small = matchMedia('(max-width:760px)').matches;
    $('sidebar').inert = small && !$('sidebar').classList.contains('open');
    $('sidebar').setAttribute('aria-hidden', String($('sidebar').inert));
    const modal = matchMedia('(max-width:1150px)').matches && !$('memory-panel').hidden;
    $('memory-panel').setAttribute('role', modal ? 'dialog' : 'complementary');
    if (modal) $('memory-panel').setAttribute('aria-modal','true'); else $('memory-panel').removeAttribute('aria-modal');
  }
  function closeSidebar() { $('sidebar').classList.remove('open'); $('sidebar-overlay').hidden = true; $('menu-button').setAttribute('aria-expanded', 'false'); syncPanels(); }
  async function openSession(session) {
    if (state.busy || state.loading) return;
    state.loading = true;
    const ticket = ++state.request;
    state.session = session; storage.set(session); clearError(); closeSidebar();
    $('messages').replaceChildren(); $('welcome').hidden = false; state.detail = null; renderMemory(); renderSessions();
    try {
      const detail = await api(`/api/sessions/${encodeURIComponent(session)}`);
      if (ticket !== state.request) return;
      state.detail = detail;
      for (const message of detail.messages) addMessage(message.role, message.content, message.sources || [], '', message.cards || [], message.weather || null, message.planning || null);
      $('welcome').hidden = detail.messages.length > 0; renderMemory(); scrollChat();
      if (detail.busy) showError('A reply is still being completed in another window. Reload this chat shortly.');
    } catch (error) { if (ticket === state.request) showError(error.message); }
    state.loading = false; syncControls(); $('message-input').focus();
  }
  function resizeInput() { $('message-input').style.height = 'auto'; $('message-input').style.height = `${Math.min($('message-input').scrollHeight, 140)}px`; syncControls(); }
  async function sendMessage(message) {
    if (state.busy || state.loading || !state.ready || !message.trim()) return;
    if (message.length > 8000) { showError('Please keep your message under 8,000 characters.'); return; }
    clearError(); state.busy = true; $('message-input').value = ''; resizeInput(); addMessage('user', message);
    const pending = thinking(); let received = false, preview = '';
    function showPreview(text, phase = 'writing') {
      preview = text; pending.body.textContent = text;
      pending.label.textContent = phase === 'checking' ? 'Checking the reply' : 'Writing your reply';
      const area = $('chat-area');
      if (area.scrollHeight-area.scrollTop-area.clientHeight < 160) area.scrollTop = area.scrollHeight;
    }
    async function revealCheckedAnswer(answer) {
      if (preview.trim() === answer.trim() || document.hidden || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const words = answer.match(/\s*\S+\s*/gu) || [answer];
      const batch = Math.max(1, Math.ceil(words.length/120));
      let text = '';
      for (let i = 0; i < words.length; i += batch) {
        text += words.slice(i, i+batch).join(''); showPreview(text);
        await new Promise(resolve => setTimeout(resolve, 12));
      }
    }
    try {
      const response = await fetch('/api/chat', { method:'POST', credentials:'same-origin', headers:{ 'Content-Type':'application/json','X-VISTRA-Demo':'1' }, body:JSON.stringify({ session_id:state.session, message, retrieval_limit:Number($('retrieval-limit').value) }) });
      if (!response.ok) { const data = await response.json(); throw new Error(data.error || data.detail || 'Unable to send this message.'); }
      if (!response.body) throw new Error('This browser cannot read streamed replies.');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      async function consume(block) {
        const lines = block.split('\n'); const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
        const text = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!text) return;
        const data = JSON.parse(text);
        if (event === 'progress') pending.label.textContent = data.message;
        if (event === 'answer') showPreview(data.text, data.phase);
        if (event === 'error') throw new Error(data.message);
        if (event === 'result') {
          received = true;
          await revealCheckedAnswer(data.answer);
          pending.row.remove();
          const note = data.needs_clarification ? 'A little more detail will help' : data.recalled_long_term > 0 ? 'With your preferences in mind' : '';
          addMessage('assistant', data.answer, data.sources || [], note, data.cards || [], data.weather || null, data.planning || null);
          if (data.media_error) showError(data.media_error);
        }
      }
      try {
        while (true) {
          const { value, done } = await reader.read(); buffer += decoder.decode(value, { stream:!done }); buffer = buffer.replace(/\r\n/g, '\n');
          let boundary; while ((boundary = buffer.indexOf('\n\n')) >= 0) { const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2); await consume(block); }
          if (done) break;
        }
      } finally { reader.releaseLock(); }
      if (!received) throw new Error('The connection ended before the reply arrived. Reload the chat to check whether it was saved.');
      await Promise.all([refreshDetail(), refreshSessions()]);
    } catch (error) {
      pending.row.remove(); showError(error.message);
      if (!received) { $('message-input').value = message; resizeInput(); }
    } finally { state.busy = false; syncControls(); $('message-input').focus(); }
  }
  function toggleMemory(open) {
    const show = open ?? $('memory-panel').hidden;
    $('memory-panel').hidden = !show; $('memory-toggle').setAttribute('aria-expanded', String(show));
    $('memory-overlay').hidden = !show;
    syncPanels();
    if (show) $('memory-close').focus(); else $('memory-toggle').focus();
  }
  function setTab(name) {
    const session = name === 'session'; $('tab-session').setAttribute('aria-selected', String(session)); $('tab-longterm').setAttribute('aria-selected', String(!session));
    $('tab-session').tabIndex = session ? 0 : -1; $('tab-longterm').tabIndex = session ? -1 : 0;
    $('session-memory').hidden = !session; $('longterm-memory').hidden = session;
  }
  $('chat-form').addEventListener('submit', event => { event.preventDefault(); sendMessage($('message-input').value.trim()); });
  $('message-input').addEventListener('input', resizeInput);
  $('message-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); } });
  document.querySelectorAll('.suggestion').forEach(button => button.addEventListener('click', () => { $('message-input').value = button.dataset.prompt; resizeInput(); $('message-input').focus(); }));
  $('new-chat').addEventListener('click', () => openSession(newID()));
  $('reload-chat').addEventListener('click', () => state.ready ? openSession(state.session) : boot());
  $('memory-toggle').addEventListener('click', () => toggleMemory()); $('memory-close').addEventListener('click', () => toggleMemory(false)); $('memory-overlay').addEventListener('click', () => toggleMemory(false));
  $('menu-button').addEventListener('click', () => { const open = !$('sidebar').classList.contains('open'); $('sidebar').classList.toggle('open', open); $('sidebar-overlay').hidden = !open; $('menu-button').setAttribute('aria-expanded', String(open)); syncPanels(); if (open) $('new-chat').focus(); });
  $('sidebar-overlay').addEventListener('click', closeSidebar);
  $('tab-session').addEventListener('click', () => setTab('session')); $('tab-longterm').addEventListener('click', () => setTab('longterm'));
  document.querySelector('.memory-tabs').addEventListener('keydown', event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const target = $('tab-session').getAttribute('aria-selected') === 'true' ? 'longterm' : 'session'; setTab(target); $(`tab-${target}`).focus(); } });
  document.addEventListener('keydown', event => {
    if ($('delete-chat-dialog').open) return;
    if (event.key === 'Escape') { closeSidebar(); if (!$('memory-panel').hidden) toggleMemory(false); }
    const panel = !$('memory-panel').hidden && matchMedia('(max-width:1150px)').matches ? $('memory-panel') : $('sidebar').classList.contains('open') && matchMedia('(max-width:760px)').matches ? $('sidebar') : null;
    if (event.key === 'Tab' && panel) {
      const targets = [...panel.querySelectorAll('button:not(:disabled):not([tabindex="-1"]),a[href]')].filter(node => node.getClientRects().length);
      const first = targets[0], last = targets[targets.length - 1];
      if (first && event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (last && !event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('resize', () => { resizeInput(); syncPanels(); });
  $('clear-chat').addEventListener('click', () => confirmDelete(state.session));
  $('delete-chat-confirm').addEventListener('click', deleteConversation);
  $('delete-chat-cancel').addEventListener('click', () => $('delete-chat-dialog').close());
  $('delete-chat-dialog').addEventListener('close', () => { deleteTarget = null; });
  async function boot() {
    state.session = storage.get() || newID(); storage.set(state.session); setTab('session');
    try {
      const data = await api('/api/bootstrap'); state.sessions = data.sessions; state.ready = true;
      $('connection').className = 'connection ready'; $('connection').replaceChildren(el('span'), document.createTextNode('Connected'));
      await openSession(state.session);
    } catch (error) {
      $('connection').className = 'connection offline'; $('connection').replaceChildren(el('span'), document.createTextNode('Unavailable')); showError(error.message);
    }
    renderSessions(); resizeInput(); syncPanels(); syncControls();
  }
  boot();
})();
