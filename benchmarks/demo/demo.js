'use strict';
const $ = (id) => document.getElementById(id);
let catalog = [], selected = null, runId = null, completed = 0, busy = false, failed = false;
const results = new Map();
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-VISTRA-Demo':'1'}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'The request failed. Reload the demo and try again.');
  return result;
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function status(text, error = false) { $('run-status').textContent = text; $('run-status').classList.toggle('error', error); }
function controls() {
  $('run').disabled = busy || failed || !selected || !!selected.preview_only_reason || completed >= selected.turns.length;
  $('reset').disabled = busy || !selected;
  $('language').disabled = busy;
  $('area').disabled = busy;
  $('search').disabled = busy;
  for (const button of $('case-list').querySelectorAll('button')) button.disabled = busy;
  $('run').textContent = busy ? 'VISTRA is responding…' : 'Run next turn · live';
}
function list() {
  const query = $('search').value.toLowerCase().trim();
  const rows = catalog.filter(row => (!$('area').value || row.track === $('area').value) && (!query || (row.family_id + ' ' + row.title).toLowerCase().includes(query)));
  $('count').textContent = rows.length;
  $('case-list').replaceChildren(...rows.map(row => {
    const button = el('button', undefined, 'case-item');
    button.classList.toggle('selected', row.family_id === selected?.family_id);
    button.append(el('small', row.family_id), el('span', row.title));
    if (row.preview_only) button.append(el('span', 'preview', 'tag'));
    button.disabled = busy;
    button.onclick = () => choose(row.family_id);
    return button;
  }));
  if (!rows.length) $('case-list').append(el('p', 'No matching scenarios.', 'empty'));
}
function renderTurns() {
  const language = $('language').value;
  $('turns').replaceChildren(...selected.turns.map((turn, index) => {
    const card = el('article', undefined, 'turn');
    const heading = el('div', undefined, 'turn-heading');
    heading.append(el('span', `TURN ${index + 1} · ${turn.owner_alias} / ${turn.session_alias}`), el('span', index < completed ? 'REPLIED · UNSCORED' : 'NOT RUN'));
    const content = el('div', undefined, 'turn-content');
    content.append(el('p', 'USER PROMPT', 'section-label'), el('p', turn.messages[language], 'prompt'));
    const expected = el('div', undefined, 'expected');
    expected.append(el('p', 'CANDIDATE EXPECTED BEHAVIOR', 'section-label'), el('p', 'Handling: ' + selected.private_expectations.expected_handling[turn.turn_id]));
    const checks = selected.private_expectations.assertions.filter(check => check.turn_id === turn.turn_id && check.check !== 'output_language');
    for (const check of checks) {
      const text = check.parameters.required_behavior || check.parameters.requirement;
      if (text && !expected.textContent.includes(text)) expected.append(el('p', text));
    }
    content.append(expected, el('p', 'ACTUAL VISTRA REPLY', 'section-label'));
    const response = results.get(turn.turn_id);
    content.append(el('div', response?.result.answer || 'Run this turn to see the response.', response ? 'answer' : 'answer pending'));
    if (response) {
      content.append(el('p', `${response.elapsed_seconds}s · ${response.result.pipeline_status || 'unknown status'} · ${response.result.response_mode || 'unknown route'} · unscored`, 'result-meta'));
      const details = el('details');
      details.append(el('summary', 'Full backend response'), el('pre', JSON.stringify(response.result, null, 2)));
      content.append(details);
    }
    card.append(heading, content);
    return card;
  }));
}
function fresh() {
  runId = null; completed = 0; failed = false; results.clear();
  status(selected.preview_only_reason ? 'Preview only: required replay adapter is not implemented.' : 'Ready. Run the first turn when you are ready.');
  renderTurns(); controls();
}
async function choose(id) {
  if (busy) return;
  busy = true; controls();
  try {
    selected = await api('/api/cases/' + encodeURIComponent(id));
    $('case-id').textContent = selected.family_id;
    $('case-title').textContent = selected.title;
    $('case-meta').textContent = `${selected.track} · ${selected.difficulty} · ${selected.turns.length} turn${selected.turns.length > 1 ? 's' : ''} · ${selected.split} split · draft labels`;
    $('preview-reason').hidden = !selected.preview_only_reason;
    $('preview-reason').textContent = selected.preview_only_reason || '';
    $('assertions').textContent = JSON.stringify(selected.private_expectations, null, 2);
    history.replaceState(null, '', '?case=' + encodeURIComponent(id) + '&lang=' + $('language').value);
    fresh(); list();
  } catch (error) { status(error.message, true); }
  finally { busy = false; controls(); }
}
$('run').onclick = async () => {
  if (busy || !selected || failed) return;
  busy = true; controls(); status(`Running turn ${completed + 1}/${selected.turns.length}. Live provider calls can take a few minutes…`);
  try {
    if (!runId) runId = (await api('/api/runs', {family_id:selected.family_id, language:$('language').value})).run_id;
    const response = await api('/api/runs/' + runId + '/next', {});
    results.set(response.turn_id, response); completed = response.completed_turns;
    renderTurns();
    status(completed === selected.turns.length ? 'All turns finished. Compare the replies with the candidate expectations; no score was assigned.' : `Turn ${completed} finished. Run the next turn to continue the same scenario.`);
  } catch (error) { failed = true; status(error.message + ' Use Fresh run before retrying.', true); }
  finally { busy = false; controls(); }
};
$('reset').onclick = fresh;
$('language').onchange = () => { fresh(); history.replaceState(null, '', '?case=' + encodeURIComponent(selected.family_id) + '&lang=' + $('language').value); };
$('area').onchange = list;
$('search').oninput = list;
(async () => {
  try {
    const health = await api('/health');
    $('health').textContent = `${health.families} families · API ${health.api_key_configured ? 'key configured' : 'key missing'}`;
    catalog = await api('/api/cases');
    const params = new URLSearchParams(location.search);
    const id = catalog.some(row => row.family_id === params.get('case')) ? params.get('case') : 'VB1-ROUTE-101';
    if (params.get('lang') === 'en') $('language').value = 'en';
    $('area').value = catalog.find(row => row.family_id === id).track;
    await choose(id);
  } catch (error) { status(error.message, true); }
})();
