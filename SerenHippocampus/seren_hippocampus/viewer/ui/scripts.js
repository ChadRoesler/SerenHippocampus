// ── SerenHippocampus viewer — leaf logic ─────────────────────────────────────
// Snaps onto the SerenMeninges shell, which provides api() (same-origin, sends
// the saved bearer), escapeHtml(), showTab() and the 🔑 token modal.
//
// The page answers three questions for someone who did not build the stack:
// did the sleep run, what is waiting for review, and if it broke, what broke.

const $ = (id) => document.getElementById(id);

function relAge(ts) {
    if (!ts) return '-';
    const s = Math.round((Date.now() - ts * 1000) / 1000);
    if (s < 0) return `in ${relSpan(-s)}`;
    return `${relSpan(s)} ago`;
}
function relSpan(s) {
    if (s < 60) return `${s}s`;
    const m = Math.round(s / 60); if (m < 60) return `${m}m`;
    const h = Math.round(m / 60); if (h < 48) return `${h}h`;
    return `${Math.round(h / 24)}d`;
}
function fmtTs(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    return `${d.toLocaleString()} (${relAge(ts)})`;
}
function showError(msg, hint) {
    $('error-slot').innerHTML = `<div class="err">⚠ ${escapeHtml(msg)}${hint ? `<span class="hint">${escapeHtml(hint)}</span>` : ''}</div>`;
}
function clearError() { $('error-slot').innerHTML = ''; }

// ----------------------------------------------------------------------------
function renderPills(root, status) {
    const mem = $('memoryBadge'), mod = $('modelBadge');
    if (mem) { mem.textContent = root.memory_reachable ? 'memory: reachable' : 'memory: unreachable';
               mem.className = 'head-pill' + (root.memory_reachable ? '' : ' hot'); }
    if (mod) { mod.textContent = status.model_configured ? 'model: wired' : 'model: mechanical';
               mod.className = 'head-pill'; }
}

function renderOverview(root, status, queue) {
    const last = status.last_sleep || null;
    const tend = status.last_tend || null;
    const stat = (label, value, cls = '') =>
        `<div class="stat ${cls}"><div class="big">${escapeHtml(value)}</div><div class="lbl">${escapeHtml(label)}</div></div>`;
    const pendingOps = (queue.drafts || []).reduce((n, d) => n + (d.operations || []).filter(o => o.status === 'pending').length, 0);
    $('stats').innerHTML = [
        stat('last sleep', last ? (last.error ? 'failed' : 'ok') : 'never', last ? (last.error ? 'bad' : 'ok') : ''),
        stat('when', last ? relAge(last.finished_at) : '-'),
        stat('bedtime', status.bedtime_at ? relAge(status.bedtime_at) : '-'),
        stat('waiting for review', pendingOps, pendingOps ? 'ok' : ''),
        stat('last tend', tend ? (tend.error ? 'failed' : `${(tend.resubmitted || []).length} resubmitted`) : 'never', tend && tend.error ? 'bad' : ''),
    ].join('');

    const lines = [];
    if (!root.memory_reachable) lines.push(`<b>SerenMemory is not answering</b> at ${escapeHtml(root.memory)}. Nothing can sleep until it does: check that Memory is running, and that the bearer in this service's config is the one Memory was installed with.`);
    if (last && last.error) lines.push(`The last sleep stopped with: <b>${escapeHtml(last.error)}</b>.`);
    if (last && last.quiet && !last.error) lines.push(`The last sleep found <b>nothing new to draft from</b>, so no model was called; it only purged what was flagged and tidied.`);
    if (last && last.catch_up && !last.error) lines.push(`The last sleep was a <b>catch-up</b> after a long gap${status.catch_up_next ? '' : ''}: it drafted and purged but aged nothing out, so anything that never had its chance is still there for review.`);
    if (status.catch_up_next && !(last && last.catch_up)) lines.push(`The next sleep will be a <b>catch-up</b>: it has been a while, so it will draft but not age anything out.`);
    if (status.sleep_at) lines.push(`Sleeps happen daily at <b>${escapeHtml(status.sleep_at)}</b> local time.`);
    const chk = status.last_check || null;
    if (chk && chk.status === 'waiting_for_brief') lines.push(`It is past bedtime and <b>no brief has arrived</b>${chk.misses ? ` (${escapeHtml(chk.misses)} check${chk.misses === 1 ? '' : 's'} so far)` : ''}. The hippocampus sleeps only on a brief: write one with submit_brief and the next check will draft on it.`);
    if (chk && chk.status === 'chain_open') lines.push(`A brief is waiting, but a draft is <b>still under review</b>. The sleep starts once that chain has landed.`);
    if (chk && chk.status === 'not_bedtime') lines.push(`Not bedtime yet. A brief written now would start a sleep on the next check.`);
    if (status.brief_wanted) lines.push(`The hippocampus has <b>asked for a brief</b> before the sleep due ${escapeHtml(status.sleep_due_at ? relAge(status.sleep_due_at) : 'soon')}; the note is waiting in Memory's near-term tier and none has arrived yet.`);
    if (last && !last.error && last.brief_requested) lines.push(last.brief_answered ? `The last sleep ran on a brief <b>the main model wrote</b>.` : `The last sleep asked for a brief and <b>none came</b>, so the small model pulled one from the fragments.`);
    if (last && !last.error && !last.quiet) lines.push(`The last sleep purged ${last.purged} flagged memor${last.purged === 1 ? 'y' : 'ies'}, looked at ${last.clusters} topic${last.clusters === 1 ? '' : 's'}, and proposed ${last.operations} operation${last.operations === 1 ? '' : 's'}${last.draft_id ? ' in one draft' : ''}${last.held_back ? `; ${last.held_back} short-term memories were already waiting on an earlier draft` : ''}.`);
    if (!status.model_configured) lines.push(`No small model is wired, so the hippocampus runs <b>mechanically</b>: topics with enough evidence become new cores, verbatim memories are kept as they are, and nothing is proposed as evidence for, or a replacement of, an existing memory - those are judgements.`);
    if (status.mode === 'external') lines.push(`Mode is <b>external</b>: nothing here runs on a timer. Something else posts /sleep and /tend, or you press the buttons.`);
    $('explain').innerHTML = lines.length ? lines.map(l => `<p class="hint">${l}</p>`).join('') : `<p class="hint">Quiet and healthy.</p>`;
}

function kvTable(obj, keys) {
    return `<div class="kv">${keys.filter(k => obj[k] !== undefined && obj[k] !== null && !(typeof obj[k] === 'object'))
        .map(k => `<span class="k">${escapeHtml(k.replace(/_/g, ' '))}</span><span class="v">${escapeHtml(k.endsWith('_at') ? fmtTs(obj[k]) : String(obj[k]))}</span>`).join('')}</div>`;
}

function renderReports(status) {
    const s = status.last_sleep;
    $('sleep-report').innerHTML = !s ? `<div class="empty">No sleep has run yet.</div>` :
        `<div class="entry ${s.error ? 'bad' : ''}">${s.error ? `<div class="content"><b>Stopped:</b> ${escapeHtml(s.error)}</div>` : ''}
            ${kvTable(s, ['started_at', 'finished_at', 'duration_seconds', 'quiet', 'catch_up', 'purged', 'brief_id', 'brief_pulled', 'clusters', 'operations', 'draft_id', 'held_back'])}
            ${s.tidy && s.tidy.deferred ? `<div class="meta"><span class="badge">tidy</span> waits for the end of the cycle - age-out runs when the chain lands</div>` :
              s.tidy ? `<div class="meta"><span class="badge">tidy</span> aged out ${s.tidy.aged_out ?? '-'} · near expired ${(s.tidy.near || {}).expired ?? '-'} · completed ${(s.tidy.near || {}).completed_promoted ?? '-'} · pruned swept ${s.tidy.pruned_swept ?? '-'}</div>` : ''}
         </div>`;
    const t = status.last_tend;
    $('tend-report').innerHTML = !t ? `<div class="empty">No tend has run yet.</div>` :
        `<div class="entry ${t.error ? 'bad' : ''}">${t.error ? `<div class="content"><b>Stopped:</b> ${escapeHtml(t.error)}</div>` : ''}
            ${kvTable(t, ['started_at', 'finished_at', 'examined'])}
            <div class="meta">${(t.resubmitted || []).length ? t.resubmitted.map(r => `<span class="badge pending">attempt ${r.attempt}</span> ${r.operations} op(s) · <code class="id">${escapeHtml(r.draft_id)}</code>`).join(' ') : 'nothing needed a redraft'}
            ${(t.ended || []).length ? ` · <span class="badge denied">${t.ended.length} chain(s) ended</span>` : ''}</div>
         </div>`;
}

function opCard(op) {
    const kind = escapeHtml(op.kind);
    const target = op.target_core_id ? ` <code class="id">→ ${escapeHtml(op.target_core_id)}</code>` : '';
    return `<div class="op">
        <div class="meta"><span class="badge ${escapeHtml(op.status)}">${escapeHtml(op.status)}</span><span class="badge">${kind}</span>${target}
            ${op.evidence_count ? `<span>evidence ${escapeHtml(op.evidence_count)}</span>` : ''}</div>
        <div class="content">${escapeHtml(op.content || '(attach: evidence only)')}</div>
        ${op.restated_content ? `<div class="critique">restate the core as: ${escapeHtml(op.restated_content)}</div>` : ''}
        ${op.rationale ? `<div class="critique">why: ${escapeHtml(op.rationale)}</div>` : ''}
        ${op.critique ? `<div class="critique"><b>denied:</b> ${escapeHtml(op.critique)}</div>` : ''}
    </div>`;
}

function draftCard(d) {
    const pending = (d.operations || []).filter(o => o.status === 'pending').length;
    return `<div class="entry">
        <div class="meta"><span class="badge pending">${pending} waiting</span><span class="badge">attempt ${escapeHtml(d.attempt)}</span>
            ${d.terminal ? '<span class="badge terminal">terminal</span>' : ''}<span>${escapeHtml(fmtTs(d.created_at))}</span><code class="id">${escapeHtml(d.id)}</code></div>
        <div class="content">${escapeHtml(d.summary || '')}</div>
        ${(d.operations || []).map(opCard).join('')}
    </div>`;
}

function renderQueue(queue) {
    if (queue.error) {
        $('queue-list').innerHTML = `<div class="err">⚠ ${escapeHtml(queue.error)}<span class="hint">The queue lives in SerenMemory; this page could not read it.</span></div>`;
        return;
    }
    const ds = queue.drafts || [];
    $('queue-list').innerHTML = ds.length ? ds.map(draftCard).join('') : `<div class="empty">Nothing is waiting for review.</div>`;
}

function pct(x) { return x == null ? '-' : `${Math.round(x * 100)}%`; }

function renderAudit(a) {
    if (a.error) {
        $('audit-models').innerHTML = `<div class="err">⚠ ${escapeHtml(a.error)}<span class="hint">The record lives in SerenMemory; this page could not read it.</span></div>`;
        $('audit-chains').innerHTML = '';
        return;
    }
    const ms = a.models || [];
    // Plain words first: one line per model a person can read without the table.
    $('audit-models').innerHTML = ms.length ? `<div class="entry"><table class="audit">
        <tr><th>model</th><th title="approved on the first attempt, of those reviewed">first pass</th>
            <th title="the attempt an operation landed on, averaged">attempts to land</th>
            <th title="a redraft denied again: the critique did not take">critique ignored</th>
            <th title="landed only because the reviewer rewrote it">rewritten</th>
            <th title="the last permitted attempt was still denied">chains lost</th><th>drafts</th></tr>
        ${ms.map(m => `<tr><td>${escapeHtml(m.model)}</td><td>${pct(m.first_pass_rate)}</td>
            <td>${escapeHtml(m.mean_attempts ?? '-')}</td>
            <td>${m.redrafts_reviewed ? `${escapeHtml(m.repeated_denials)} of ${escapeHtml(m.redrafts_reviewed)}` : '-'}</td>
            <td>${escapeHtml(m.edited_on_approval)}</td><td>${escapeHtml(m.chains_ended_denied)}</td>
            <td>${escapeHtml(m.drafts)}</td></tr>`).join('')}
        </table></div>` : `<div class="empty">No drafts yet.</div>`;

    const cs = a.chains || [];
    $('audit-chains').innerHTML = cs.length ? cs.map(c => {
        const b = c.brief;
        const brief = !b ? `<div class="critique">no brief (a sleep by hand)</div>`
            : b.missing ? `<div class="critique">brief <code class="id">${escapeHtml(b.id)}</code> is gone</div>`
            : `<div class="critique"><b style="color:inherit">brief:</b> ${escapeHtml(b.summary)}
                ${(b.promote_hints || []).length ? ` · keep: ${escapeHtml(b.promote_hints.join(', '))}` : ''}
                ${(b.noise_hints || []).length ? ` · noise: ${escapeHtml(b.noise_hints.join(', '))}` : ''}</div>`;
        const outcomeCls = c.outcome === 'landed' ? 'approved' : (c.outcome === 'ended denied' ? 'denied' : 'pending');
        return `<details class="entry">
            <summary class="meta"><span class="badge ${outcomeCls}">${escapeHtml(c.outcome)}</span>
                <span>${escapeHtml(fmtTs(c.started_at))}</span><span>${escapeHtml(c.attempts.length)} attempt(s)</span>
                <span>${escapeHtml(c.landed)} landed</span><code class="id">${escapeHtml(c.cluster_id)}</code></summary>
            ${brief}
            ${c.attempts.map(t => `<div class="attempt">
                <div class="meta"><span class="badge">attempt ${escapeHtml(t.attempt)}</span>${t.terminal ? '<span class="badge terminal">terminal</span>' : ''}
                    <span>${escapeHtml((t.model && (t.model.served || t.model.name || t.model.mode)) || 'model not recorded')}</span>
                    ${t.model && t.model.prompt ? `<span>prompt ${escapeHtml(t.model.prompt)}</span>` : ''}
                    <span>${escapeHtml(fmtTs(t.created_at))}</span></div>
                ${(t.operations || []).map(op => opCard(op) + (op.edited_content ? `<div class="critique"><b style="color:#e5a03a">rewritten by the reviewer:</b> ${escapeHtml(op.edited_content)}</div>` : '')).join('')}
            </div>`).join('')}
        </details>`;
    }).join('') : `<div class="empty">No chains yet.</div>`;
}

function renderReplays(list) {
    const sel = $('replay-draft');
    if (!sel) return;
    const rows = list.entries || [];
    const keep = sel.value;
    sel.innerHTML = rows.length ? rows.map(r => `<option value="${escapeHtml(r.draft_id)}">${escapeHtml(fmtTs(r.created_at))} · attempt ${escapeHtml(r.attempt)} · ${escapeHtml(r.calls)} call(s) · ${escapeHtml(r.model || 'model not recorded')}</option>`).join('')
        : '<option value="">no drafts with saved calls yet</option>';
    if (keep) sel.value = keep;
}

function replaySide(title, side, reviewed) {
    const c = side.checks || {};
    const inv = (c.invented_numbers || []).length ? `<span class="badge denied">invented ${escapeHtml(c.invented_numbers.join(', '))}</span>` : '<span class="badge approved">nothing invented</span>';
    const calls = (side.calls || []).map((call, i) => `<div class="attempt">
        <div class="meta"><span class="badge">${escapeHtml(call.stage)}</span><span>${escapeHtml(call.topic || 'untagged')}</span>
            ${call.seconds != null ? `<span>${escapeHtml(call.seconds)}s</span>` : ''}${call.error ? `<span class="badge denied">${escapeHtml(call.error)}</span>` : ''}</div>
        ${(call.ops || []).map(op => opCard(Object.assign({status: 'proposed'}, op))).join('') || '<div class="empty">no operations</div>'}
    </div>`).join('');
    return `<div class="entry"><div class="meta"><b>${escapeHtml(title)}</b></div>
        <div class="meta">${inv}<span>${escapeHtml(c.valid ?? 0)} valid</span><span>${escapeHtml(c.failed_calls ?? 0)} failed</span>
            <span>overlap with what landed ${c.overlap_landed == null ? '-' : Math.round(c.overlap_landed * 100) + '%'}</span><span>${escapeHtml(c.seconds ?? 0)}s</span></div>
        ${reviewed && reviewed.length ? `<div class="critique">what the reviewer said: ${reviewed.map(o => escapeHtml(o.status) + (o.critique ? ' - ' + escapeHtml(o.critique) : '')).join('; ')}</div>` : ''}
        ${calls}</div>`;
}

async function runReplay() {
    const id = $('replay-draft').value, url = $('replay-url').value.trim();
    if (!id) return;
    const btn = $('btn-replay'), st = $('replay-status');
    btn.disabled = true; st.textContent = 'replaying… (a slow model takes as long as a sleep did)';
    try {
        const r = await api('/replay', { method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({ draft_id: id, url }) });
        const o = r.original || {}, c = r.candidate || {};
        const oName = (o.model && (o.model.model_served || o.model.model_name)) || 'original';
        $('replay-result').innerHTML = `
            ${(r.landed || []).length ? `<div class="critique">what landed in this chain: ${r.landed.map(escapeHtml).join(' · ')}</div>` : ''}
            <div class="replay-cols">${replaySide('original · ' + oName, o, o.reviewed_ops)}${replaySide('candidate · ' + (c.served || c.url), c)}</div>`;
        st.textContent = '';
    } catch (e) { st.textContent = `replay failed: ${e.message}`; }
    finally { btn.disabled = false; }
}

function renderEvents(ev) {
    const rows = ev.entries || [];
    const el = $('events-list');
    if (!el) return;
    el.innerHTML = rows.length ? rows.map(e => `<div class="entry ${e.event === 'sleep_failed' ? 'bad' : ''}">
        <div class="meta"><span class="badge ${e.event === 'sleep_failed' ? 'denied' : 'pending'}">${escapeHtml(e.event.replace(/_/g, ' '))}</span>
            <span>${escapeHtml(fmtTs(e.at))}</span>
            ${e.draft_id ? `<code class="id">${escapeHtml(e.draft_id)}</code>` : ''}
            ${e.operations != null ? `<span>${escapeHtml(e.operations)} op(s)</span>` : ''}
            ${e.count != null ? `<span>${escapeHtml(e.count)} purged</span>` : ''}
            ${e.attempt != null ? `<span>attempt ${escapeHtml(e.attempt)}${e.terminal ? ' (terminal)' : ''}</span>` : ''}
            ${ev.webhook ? `<span class="badge ${e.delivered ? 'approved' : (e.delivered === false ? 'denied' : '')}">${e.delivered ? 'sent' : (e.delivered === false ? 'not sent' : 'kept')}</span>` : ''}</div>
        ${e.error ? `<div class="content">${escapeHtml(e.error)}</div>` : ''}
        ${e.delivery_error ? `<div class="critique">webhook: ${escapeHtml(e.delivery_error)}</div>` : ''}
    </div>`).join('') : `<div class="empty">Nothing has happened yet.</div>`;
}

function renderHistory(hist) {
    const rows = (hist.entries || []);
    $('history-list').innerHTML = rows.length ? rows.map(r => `<div class="entry ${r.error ? 'bad' : ''}">
        <div class="meta"><span class="badge ${r.error ? 'denied' : 'approved'}">${escapeHtml(r.kind)}</span><span>${escapeHtml(fmtTs(r.finished_at))}</span>
            <span>${escapeHtml(r.duration_seconds ?? '-')}s</span>${r.kind === 'sleep' ? `<span>${escapeHtml(r.operations ?? 0)} op(s) proposed, ${escapeHtml(r.purged ?? 0)} purged</span>` : `<span>${escapeHtml(r.resubmitted ?? 0)} resubmitted, ${escapeHtml(r.ended ?? 0)} chain(s) ended</span>`}</div>
        ${r.error ? `<div class="content"><b>Stopped:</b> ${escapeHtml(r.error)}</div>` : ''}
    </div>`).join('') : `<div class="empty">Nothing has run yet.</div>`;
}

// ----------------------------------------------------------------------------
async function load() {
    clearError();
    let root = {}, status = {}, queue = { drafts: [] }, hist = { entries: [] }, events = { entries: [] };
    try { root = await api('/'); } catch (e) { showError(`This service did not answer: ${e.message}`); return; }
    try { root.memory_reachable = (await api('/health')).memory_reachable; } catch (e) { root.memory_reachable = false; }
    try { status = await api('/status'); }
    catch (e) { showError(`Could not read /status: ${e.message}`, 'If this service has a bearer token, set it via 🔑 Token.'); return; }
    try { queue = await api('/queue'); } catch (e) { queue = { drafts: [], error: e.message }; }
    try { hist = await api('/history'); } catch (e) { hist = { entries: [] }; }
    try { events = await api('/events'); } catch (e) { events = { entries: [] }; }
    let audit = { chains: [], models: [] }, replays = { entries: [] };
    try { replays = await api('/replays'); } catch (e) { replays = { entries: [] }; }
    try { audit = await api('/audit'); } catch (e) { audit = { chains: [], models: [], error: e.message }; }
    renderPills(root, status);
    renderOverview(root, status, queue);
    renderReports(status);
    renderQueue(queue);
    renderHistory(hist);
    renderEvents(events);
    renderAudit(audit);
    renderReplays(replays);
}

async function runNow(kind) {
    const btn = $(`btn-${kind}`), out = $('action-result');
    btn.disabled = true; out.textContent = `${kind}…`;
    try {
        const r = await api(`/${kind}`, { method: 'POST' });
        out.textContent = r.refused ? r.message :
            r.error ? `${kind} stopped: ${r.error}` :
            kind === 'sleep' ? `sleep done: ${r.operations} operation(s) proposed, ${r.purged} purged` :
                               `tend done: ${(r.resubmitted || []).length} resubmitted`;
    } catch (e) {
        out.textContent = /409/.test(e.message) ? 'one is already running' : `failed: ${e.message}`;
    } finally {
        btn.disabled = false;
        load();
    }
}

document.addEventListener('DOMContentLoaded', load);
