/* Local sample only. Credentials stay on the bridge; all model/file text is rendered as text. */
const $ = id => document.getElementById(id), active = new Set(['queued', 'running', 'cancelling', 'reconciling']);
let modelSetup = null, draftProfile = null;
let session = null, bootstrap = null, catalog = null, events = new Map(), busy = false, pending = null, watch = null, refreshTimer = null, refreshing = null;
const labels = { harness_select_skills: 'Select skills', harness_read_skill_resource: 'Read skill reference', harness_read_file: 'Read file', harness_write_file: 'Write file', harness_list_files: 'List files', harness_calculate: 'Calculate', harness_ask_user: 'Ask for clarification', harness_plan: 'Update task plan' };
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined)
    n.textContent = text; if (cls)
    n.className = cls; return n; }
async function request(path, body) { const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'X-Sample-Client': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' }); const data = await response.json(); if (!response.ok) {
    const error = new Error(data.detail || 'Request failed.');
    error.status = response.status;
    throw error;
} return data; }
function controls() { $('operation').hidden = !busy; for (const b of document.querySelectorAll('#files button'))
    b.disabled = busy; const status = session?.state.status; const allowed = session?.actions || {}; $('send').disabled = (modelSetup && session?.state?.config_digest !== modelSetup.revision) || busy || !!pending || !allowed.turn || active.has(status) || ['waiting_approval', 'outcome_unknown'].includes(status); $('cancel').disabled = busy || !!pending || !allowed.cancel || !['queued', 'running', 'waiting_input', 'waiting_approval'].includes(status); $('new').disabled = busy || !!pending; $('sessions').disabled = busy || !!pending; $('model-setup').disabled = busy || !!pending; for (const b of document.querySelectorAll('#setup-dialog button, #setup-dialog input, #setup-dialog select, #setup-dialog textarea')) b.disabled = busy || !!pending; $('model-thinking').disabled = busy || !!pending || !draftProfile?.execution; $('retry').hidden = !pending; $('retry').disabled = busy; for (const b of document.querySelectorAll('[data-prompt]'))
    b.disabled = $('send').disabled; }
async function action(work) { if (busy)
    return; busy = true; $('error').hidden = true; controls(); try {
    await work();
}
catch (error) {
    $('error').textContent = error.message;
    $('error').hidden = false;
}
finally {
    busy = false;
    controls();
} }
function messages() { const state = session.state; return [...state.history.flatMap(t => t.messages), ...(state.run && !state.run.archived ? state.run.frames[0].messages : [])]; }
function render() {
    const state = session.state, run = state.run, status = state.status;
    $('status').textContent = ({ idle: 'Ready', running: 'Working', queued: 'Starting', completed: 'Completed', waiting_input: 'Needs your input', failed: 'Stopped with an error', cancelled: 'Cancelled' })[status] || status.replaceAll('_', ' ');
    $('hint').textContent = active.has(status) ? 'The agent is working. You can stop this run.' : status === 'waiting_input' ? 'Reply below to continue the same task.' : status === 'failed' ? 'Inspect the last activity. You can send a new message.' : 'Enter to send · Shift + Enter for a new line';
    const panel = $('messages'), bottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 70;
    panel.replaceChildren();
    for (const m of messages()) {
        let text = m.content, role = m.role;
        if (m.call?.name === 'harness_ask_user')
            text = m.call.arguments.question;
        if (role === 'tool') {
            try {
                const result = JSON.parse(text);
                if (!Object.hasOwn(result, 'answer'))
                    continue;
                text = result.answer;
                role = 'user';
            }
            catch {
                continue;
            }
        }
        if (!text)
            continue;
        const article = node('article', undefined, 'bubble ' + (role === 'user' && !m.runtime_feedback ? 'user' : 'assistant'));
        article.append(node('strong', m.runtime_feedback ? 'Completion check' : m.completion === 'rejected' ? 'Agent draft — check failed' : role === 'user' ? 'You' : 'Maintenance agent'), node('div', text));
        panel.append(article);
    }
    if (!panel.children.length)
        panel.append(node('div', 'Your asset list is loaded. Ask for a maintenance plan, or choose a prompt below.', 'bubble assistant'));
    if (run?.stream?.text) {
        const article = node('article', undefined, 'bubble assistant provisional');
        article.append(node('strong', active.has(status) ? 'Generating · provisional' : 'Incomplete draft'), node('div', run.stream.text));
        panel.append(article);
    }
    if (bottom)
        panel.scrollTop = panel.scrollHeight;
    if (modelSetup && state.config_digest !== modelSetup.revision) $('hint').textContent = 'This conversation uses a previous model setup. Start a new chat to continue.';
    const failure = [...events.values()].filter(e => e.kind === 'run.failed').at(-1);
    $('failure').hidden = status !== 'failed';
    $('failure').textContent = status === 'failed' ? (failure?.data.reason || 'The run stopped. Inspect its last tool call and send a follow-up to continue.') : '';
    $('question').hidden = status !== 'waiting_input';
    $('question-text').textContent = status === 'waiting_input' ? run.pending.arguments.question : '';
    const selected = new Set(run?.frames?.at(-1)?.active_skills || []);
    $('skills').replaceChildren();
    for (const skill of catalog.skills) {
        const card = node('div', undefined, 'skill');
        card.append(node('span', selected.has(skill.name) ? 'Active' : 'Available', 'pill'), node('strong', skill.name), node('p', skill.description), node('p', 'Version ' + skill.version, 'small'));
        $('skills').append(card);
    }
    $('skill-count').textContent = selected.size + ' active';
    const open = new Set([...$('activity').querySelectorAll('details[open]')].map(n => n.dataset.id));
    $('activity').replaceChildren();
    const all = messages(), results = new Map(all.filter(m => m.role === 'tool').map(m => [m.call_id, m]));
    for (const m of all.filter(m => m.call)) {
        const result = results.get(m.call_id);
        let value = null;
        try {
            value = result ? JSON.parse(result.content) : null;
        }
        catch {
            value = result?.content;
        }
        const asking = m.call.name === 'harness_ask_user' && !result && status === 'waiting_input';
        const label = result ? (value?.unavailable ? 'Stopped' : value?.error ? 'Rejected safely' : 'Completed') : asking ? 'Waiting for you' : active.has(status) ? 'Running' : status === 'failed' ? 'Stopped' : 'Not completed';
        const detail = node('details', undefined, 'activity-item ' + (label === 'Running' ? 'running' : label === 'Rejected safely' || label === 'Stopped' ? 'failed' : ''));
        detail.dataset.id = m.call_id;
        detail.open = open.has(m.call_id);
        const summary = node('summary', labels[m.call.name] || m.call.name);
        summary.append(node('span', label));
        detail.append(summary, node('pre', JSON.stringify({ arguments: m.call.arguments, ...(result ? { result: value } : {}) }, null, 2)));
        $('activity').append(detail);
    }
    for (const event of events.values()) {
        const check = event.data?.completion;
        if (event.kind !== 'model.completed' || !check?.status) continue;
        const detail = node('details', undefined, 'activity-item ' + (check.status === 'rejected' ? 'failed' : ''));
        detail.dataset.id = 'check-' + event.revision;
        detail.open = open.has(detail.dataset.id);
        const summary = node('summary', 'Completion check');
        summary.append(node('span', check.status === 'passed' ? 'Passed' : 'Needs correction'));
        detail.append(summary, node('pre', JSON.stringify(check, null, 2)));
        $('activity').append(detail);
    }
    if (!$('activity').children.length)
        $('activity').append(node('p', 'Activity will appear when the agent starts working.', 'muted small'));
    const usage = [...events.values()].filter(e => e.kind === 'model.completed').reduce((total, e) => total + (e.data.input_tokens || 0) + (e.data.output_tokens || 0), 0);
    $('usage').textContent = usage ? usage.toLocaleString() + ' tokens' : '';
    $('files').replaceChildren();
    const files = Object.entries(state.files || {});
    $('file-count').textContent = files.length + (files.length === 1 ? ' file' : ' files');
    for (const [path, file] of files) {
        const row = node('div', undefined, 'file'), info = node('div');
        info.append(node('strong', path), node('small', `Version ${file.version} · ${file.bytes} bytes`));
        const preview = node('button', 'Preview'), download = node('button', 'Download');
        preview.onclick = () => action(() => previewFile(path));
        download.onclick = () => action(() => downloadFile(path));
        row.append(info, preview, download);
        $('files').append(row);
    }
    controls();
}
async function refresh() { if (!session)
    return; const id = session.id; const row = await request('/v1/sessions/' + id); if (session?.id !== id)
    return; session = row; let cursor = Math.max(0, ...events.keys()); for (let page = 0; page < 30; page++) {
    const batch = await request(`/v1/sessions/${id}/events?after=${cursor}`);
    if (session?.id !== id)
        return;
    for (const event of batch.events)
        events.set(event.revision, event);
    if (batch.events.length < 100)
        break;
    cursor = batch.events.at(-1).revision;
} render(); }
function scheduleRefresh() { if (refreshTimer)
    return; refreshTimer = setTimeout(async () => { refreshTimer = null; if (refreshing) {
    scheduleRefresh();
    return;
} refreshing = refresh().catch(error => { $('error').textContent = error.message; $('error').hidden = false; }).finally(() => { refreshing = null; }); }, 200); }
async function stream(w) { while (w === watch) {
    try {
        const cursor = Math.max(0, ...events.keys());
        const response = await fetch(`/v1/sessions/${w.id}/stream?after=${cursor}`, { headers: { 'X-Sample-Client': '1' }, signal: w.controller.signal, cache: 'no-store' });
        if (!response.ok)
            throw Error('Stream unavailable');
        $('connection').textContent = 'Live';
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        while (w === watch) {
            const { done, value } = await reader.read();
            if (done)
                break;
            buffer = (buffer + decoder.decode(value, { stream: true })).replaceAll('\r\n', '\n');
            if (buffer.length > 262144)
                throw Error('Stream frame too large');
            let boundary;
            while ((boundary = buffer.indexOf('\n\n')) >= 0) {
                const block = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                const data = block.split('\n').filter(x => x.startsWith('data: ')).map(x => x.slice(6)).join('\n');
                if (!data)
                    continue;
                if (block.includes('event: access.closed'))
                    throw Error('Access changed');
                const event = JSON.parse(data);
                if (w !== watch)
                    return;
                events.set(event.revision, event);
                scheduleRefresh();
            }
        }
    }
    catch (error) {
        if (error.name === 'AbortError')
            return;
        $('connection').textContent = 'Reconnecting…';
    }
    if (w === watch)
        await new Promise(resolve => setTimeout(resolve, 1000));
} }
async function openSession(id) { watch?.controller.abort(); watch = null; clearTimeout(refreshTimer); refreshTimer = null; if (refreshing)
    await refreshing; events = new Map(); session = { id }; await refresh(); sessionStorage.setItem('maintenance-session', id); $('sessions').value = id; const w = { id, controller: new AbortController() }; watch = w; void stream(w); }
async function list() { const result = await request('/v1/sessions'); $('sessions').replaceChildren(); for (const row of result.sessions)
    $('sessions').add(new Option(row.title + ' · ' + row.id.slice(0, 8), row.id)); if (session)
    $('sessions').value = session.id; return result.sessions; }
async function sendPending() { const saved = pending; let row; try {
    row = await request(saved.path, saved.body);
}
catch (error) {
    if (error.status && error.status < 500) {
        pending = null;
        if (session)
            await refresh();
    }
    else
        error.message += ' Use Retry same command; do not resubmit a new one.';
    throw error;
} pending = null; if (saved.clear)
    $('message').value = ''; await saved.after(row); }
async function mutate(path, body, after, clear = false) { if (pending)
    throw Error('Resolve the pending command first.'); pending = { path, body: { command_id: crypto.randomUUID(), ...body }, after, clear }; await sendPending(); }
async function command(kind, fields, clear = false) { await mutate(`/v1/sessions/${session.id}/${kind}`, { expected_revision: session.revision, ...fields }, async () => { await refresh(); }, clear); }
async function newChat() { await mutate('/v1/sessions', { agent_id: bootstrap.agent, title: 'Maintenance planning' }, async (row) => { await list(); await openSession(row.id); await command('files', { path: 'assets.csv', content: bootstrap.assets, expected_digest: null }); }); }
async function fileResponse(path) { const response = await fetch(`/v1/sessions/${session.id}/files/${path.split('/').map(encodeURIComponent).join('/')}`, { headers: { 'X-Sample-Client': '1' }, cache: 'no-store' }); if (!response.ok)
    throw Error('Output could not be loaded.'); return response; }
async function previewFile(path) { $('preview-title').textContent = path; $('preview-content').textContent = await (await fileResponse(path)).text(); $('preview').showModal(); }
async function downloadFile(path) { const url = URL.createObjectURL(await (await fileResponse(path)).blob()), link = node('a'); link.href = url; link.download = path.split('/').at(-1); link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
$('send').onclick = () => action(async () => { const text = $('message').value.trim(); if (text)
    await command('turns', { text }, true); });
async function cancelRun() {
    const id = session.id, runId = session.state.run.id;
    for (let attempt = 0; attempt < 3; attempt++) {
        await refresh();
        // Retry only a definite revision rejection for the same run. An uncertain
        // response keeps its exact command envelope for the explicit Retry action.
        if (session.id !== id || session.state.run?.id !== runId) {
            throw Error('The active run changed. Review it before stopping.');
        }
        if (!['queued', 'running', 'waiting_input', 'waiting_approval'].includes(session.state.status)) return;
        try {
            await command('cancel', {});
            return;
        } catch (error) {
            if (error.status !== 409 || pending || attempt === 2) throw error;
        }
    }
}
$('cancel').onclick = () => action(cancelRun);
$('new').onclick = () => action(newChat);
$('retry').onclick = () => action(sendPending);
$('sessions').onchange = () => action(() => openSession($('sessions').value));
$('close-preview').onclick = () => $('preview').close();
$('message').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (!$('send').disabled)
        $('send').click();
} };
for (const b of document.querySelectorAll('[data-prompt]'))
    b.onclick = () => { $('message').value = b.dataset.prompt; $('message').focus(); };
void action(async () => { bootstrap = await request('/sample/bootstrap'); $('model').textContent = bootstrap.model; catalog = await request('/v1/agents/' + bootstrap.agent + '/capabilities'); try { setupStatus(await request('/sample/model-setup')); } catch {} const rows = await list(), saved = sessionStorage.getItem('maintenance-session'); await openSession(rows.some(r => r.id === saved) ? saved : bootstrap.session_id); });

function fillProfile(value) {
    draftProfile = structuredClone(value);
    $('profile-ref').value = draftProfile.model_ref || '';
    $('served-model').value = draftProfile.model_name || '';
    $('model-endpoint').value = draftProfile.base_url || '';
    $('model-thinking').checked = draftProfile.execution?.enable_thinking !== false && !!draftProfile.execution;
    $('model-thinking').disabled = !draftProfile.execution;
    $('reasoning-policy').textContent = draftProfile.execution?.family === 'gemma4' ? 'Gemma: preserve tool-call reasoning; omit earlier answer reasoning.' : draftProfile.execution?.family === 'qwen3_8' ? 'Qwen: retain reasoning across turns within context limits.' : 'Generic compatible model: no family-specific reasoning retention.';
    $('profile-json').value = JSON.stringify(draftProfile, null, 2);
}
function formProfile() {
    const value = structuredClone(draftProfile);
    value.model_ref = $('profile-ref').value.trim();
    value.model_name = $('served-model').value.trim();
    value.base_url = $('model-endpoint').value.trim();
    value.api_key_env = 'MODEL_API_KEY';
    if (value.execution) {
        value.execution.enable_thinking = $('model-thinking').checked;
        if (!value.execution.enable_thinking) delete value.execution.reasoning_effort;
    }
    return value;
}
function setupStatus(value) {
    modelSetup = value;
    $('runtime-version').textContent = 'Harness ' + value.version;
    $('model').textContent = value.current.model_name;
    $('setup-current').textContent = 'Active: ' + value.current.model_name + ' · ' + value.current.base_url + ' · output allowance ' + value.output_tokens + ' tokens';
}
async function showSetup() {
    setupStatus(await request('/sample/model-setup'));
    const select = $('model-preset'); select.replaceChildren(new Option('Current configuration', 'current'));
    for (let i = 0; i < modelSetup.presets.length; i++) select.add(new Option(modelSetup.presets[i].model_name + (modelSetup.presets[i].model_ref.endsWith('preview') ? ' (experimental preview)' : ''), String(i)));
    select.add(new Option('Custom compatible model', 'custom'));
    fillProfile(modelSetup.current); $('model-key').value = ''; $('reuse-key').checked = false; $('no-auth').checked = false;
    $('setup-result').textContent = ''; $('setup-dialog').showModal();
}
$('model-setup').onclick = () => action(showSetup);
$('close-setup').onclick = () => { $('model-key').value = ''; $('setup-dialog').close(); };
$('setup-dialog').addEventListener('cancel', () => { $('model-key').value = ''; });
$('model-preset').onchange = () => {
    const choice = $('model-preset').value;
    fillProfile(choice === 'current' ? modelSetup.current : choice === 'custom' ? {model_ref:'custom', model_name:'', base_url:'', api_key_env:'MODEL_API_KEY', protocol:'openai_chat', stream:true, request_timeout_seconds:120} : modelSetup.presets[Number(choice)]);
    $('model-key').value = ''; $('reuse-key').checked = false; $('no-auth').checked = false;
};
for (const id of ['profile-ref', 'served-model', 'model-endpoint', 'model-thinking']) $(id).oninput = $(id).onchange = () => { draftProfile = formProfile(); $('profile-json').value = JSON.stringify(draftProfile, null, 2); };
$('load-profile-json').onclick = () => action(async () => { try { const value = JSON.parse($('profile-json').value); if (!value || Array.isArray(value) || typeof value !== 'object') throw Error(); fillProfile(await request('/sample/model-validate', value)); $('setup-result').textContent = 'JSON loaded into form. Review endpoint and credentials before applying.'; } catch (error) { $('setup-result').textContent = error.message || 'Enter a valid Model JSON object.'; } });
$('import-profile').onchange = async () => { const file = $('import-profile').files[0]; if (!file) return; if (file.size > 65536) { $('setup-result').textContent = 'Profile is too large (maximum 64 KiB).'; return; } $('profile-json').value = await file.text(); $('load-profile-json').click(); };
$('export-profile').onclick = () => action(async () => { const profile = await request('/sample/model-validate', formProfile()); const url = URL.createObjectURL(new Blob([JSON.stringify(profile,null,2)+'\n'], {type:'application/json'})); const a = node('a'); a.href=url; a.download='model-profile.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); $('setup-result').textContent='Profile downloaded. It contains no API key entered in the credential field.'; });
$('apply-profile').onclick = () => action(async () => {
    const key = $('model-key').value; $('model-key').value = '';
    watch?.controller.abort(); watch = null; clearTimeout(refreshTimer); refreshTimer = null;
    $('setup-result').textContent = 'Applying model setup and restarting the local worker…';
    try {
        await mutate('/sample/model-setup', {expected_revision:modelSetup.revision, model:formProfile(), api_key:key, reuse_key:$('reuse-key').checked, no_auth:$('no-auth').checked}, async value => {
            setupStatus(value); bootstrap = await request('/sample/bootstrap'); catalog = await request('/v1/agents/' + bootstrap.agent + '/capabilities');
            $('setup-dialog').close(); await newChat();
        });
    } catch (error) { $('setup-result').textContent = error.message; if (!pending && session?.id) await openSession(session.id); throw error; }
});
