/* Review workspace. All source text is escaped before entering HTML. */
let manageTab = 'reviews';
let manageFilter = 'all';
let manageData = null;
let candidateData = null;
const flagNames = {conflict:'Conflict', inconsistent:'Inconsistent', stale:'Stale'};
const decisionNames = {pending:'Pending review', approved:'Approved', needs_changes:'Needs changes', rejected:'Rejected'};
const roleNames = {admin:'Administrator', reviewer:'Reviewer', viewer:'Read-only user'};
const researchStatus = {awaiting_critique:'Awaiting counter-evidence check',awaiting_material:'Awaiting materials',awaiting_assessment:'Awaiting model assessment',awaiting_review:'Awaiting human review',followup_accepted:'Accepted for follow-up research',rejected:'Rejected'};

function researchCard(item) {
  const stale = item.release !== manageData.release;
  const candidate = item.candidate;
  const assessment = item.assessment;
  const critique = candidate.critique;
  return `<article class="review-card" data-research-id="${esc(item.id)}" data-version="${item.version}">
    <div class="review-card-head"><span class="claim-number">${esc(shortId(item.id))} · ${esc(new Date(item.created_at*1000).toLocaleString('en-US'))}</span><span class="tag ${stale?'red':'gold'}">${stale?'Previous-version task':esc(researchStatus[item.status]||item.status)}</span></div>
    <h3>${esc(item.question)}</h3><p class="panel-sub">Proposal input: ${item.context.proposer_evidence_count ?? item.context.evidence_ids.length} original evidence items; the counter-evidence check reads ${critique ? item.context.evidence_ids.length - item.context.proposer_evidence_count : 0} more; ${item.context.omitted_evidence_count} related evidence items remain outside this review. Do not treat this as exhaustive retrieval.</p><p><strong>Hypothesis to verify: </strong>${esc(candidate.hypothesis)}</p><p class="panel-sub"><strong>Uncertainty: </strong>${esc(candidate.uncertainty)}</p>
    <div class="review-evidence">${candidate.support_evidence.map(id=>`<a href="${href('evidence',id)}">Support ${esc(shortId(id))}</a>`).join('')}${candidate.counter_evidence.map(id=>`<a href="${href('evidence',id)}">Counter-evidence ${esc(shortId(id))}</a>`).join('')}</div>
    ${critique?`<div class="panel-sub"><strong>Independent counter-evidence check (model provisional): ${esc(critique.verdict)}</strong><p>${esc(critique.challenge)}</p><p>Assumptions to check: ${esc(critique.assumptions.join('; ')||'None listed')}</p><p>Next check: ${esc(critique.next_check)}</p><div class="review-evidence">${critique.counter_evidence.map(id=>`<a href="${href('evidence',id)}">Counter-evidence candidate ${esc(shortId(id))}</a>`).join('')}</div></div>`:''}
    <div class="data-list">${candidate.requests.map((request,index)=>`<div class="data-row"><span><strong>${index+1}. ${request.kind==='source'?'Request original material':'Request validation access'}: ${esc(request.question)}</strong><small>Reason: ${esc(request.why)}</small><small>Acceptance criteria: ${esc(request.acceptance)}</small></span></div>`).join('')}</div>
    <p class="panel-sub">Submitted materials: ${item.materials.length ? item.materials.map(row=>`<a href="/api/manage/research/material?task=${encodeURIComponent(item.id)}&id=${encodeURIComponent(row.id)}" target="_blank" rel="noopener">${esc(row.filename)}</a> · SHA-256 ${esc(row.sha256.slice(0,12))}`).join('; ') : 'None'}</p>
    ${assessment?`<div class="panel-sub"><strong>Provisional model assessment: ${esc(assessment.assessment)}</strong><p>${esc(assessment.reason)}</p><p>Remaining gap: ${esc(assessment.remaining_gap)}</p></div>`:''}
    ${!stale && item.status==='awaiting_critique'?`<button class="mini-action research-critique" type="button" data-task-id="${esc(item.id)}">Retry counter-evidence check (without duplicating the proposal)</button>`:''}
    ${!stale && item.status==='awaiting_material'?`<form class="research-material-form" data-task-id="${esc(item.id)}" data-version="${item.version}"><label class="form-field">Add materials (UTF-8 .md/.txt file)<input name="file" type="file" accept=".md,.markdown,.txt,text/plain" required></label><button class="mini-action" type="submit">Submit to research workspace</button><p class="form-message" role="status"></p></form>`:''}
    ${!stale && item.status==='awaiting_assessment'?`<button class="mini-action research-assess" type="button" data-task-id="${esc(item.id)}">Run one model assessment</button>`:''}
    ${!stale && item.status==='awaiting_review'?`<form class="research-decision-form" data-task-id="${esc(item.id)}" data-version="${item.version}"><label class="form-field">Review note<textarea name="note" rows="2" minlength="3" maxlength="4000" required></textarea></label><label class="form-field">Decision<select name="decision"><option value="accept_for_followup">Accept for follow-up research (still unverified)</option><option value="reject">Reject</option></select></label><button class="mini-action" type="submit">Save decision</button><p class="form-message" role="status"></p></form>`:''}</article>`;
}

function reviewCard(item, release = manageData.release, candidate = false) {
  const review = item.review || {decision:'pending', flags:[], note:''};
  const flags = [...new Set([...item.auto_flags, ...review.flags])];
  const writable = state.me.user?.role !== 'viewer';
  const evidence = candidate ? `<div class="review-evidence">${item.evidence_details.map((row) => `<details><summary>${esc(row.source)} · ${row.page ? `PDF page ${row.page}` : esc(row.section)} · ${esc(shortId(row.id))}</summary><p>${esc(row.statement)}</p><blockquote class="quote-block">${esc(row.quote)}</blockquote><small>${esc(row.source_path)}</small></details>`).join('')}</div>` : `<div class="review-evidence"><a href="${href('topic',item.topic_id)}">Open topic ↗</a>${item.evidence.slice(0,4).map((id) => `<a href="${href('evidence',id)}">${esc(shortId(id))}</a>`).join('')}${item.evidence.length > 4 ? `<span>Another ${item.evidence.length-4} items</span>` : ''}</div>`;
  const conditions = candidate ? `<div class="claim-explain"><strong>Synthesis rationale</strong> · ${esc(item.reasoning)}</div><div class="conditions">${Object.entries(item.conditions).map(([key,value])=>`<span class="meta-chip">${esc(key)}: ${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</span>`).join('')}</div>` : '';
  const validApproval = candidate ? !!item.approved : review.decision === 'approved' && flags.length === 0;
  const decisionLabel = review.decision === 'approved' && !validApproval ? 'Approval has flags' : decisionNames[review.decision];
  return `<article class="review-card" data-claim="${esc(item.id)}" data-release="${esc(release)}" data-version="${review.version || 0}"><div class="review-card-head"><span class="claim-number">${esc(item.topic)} · ${esc(shortId(item.id))}</span><span class="tag ${validApproval ? 'mint' : review.decision === 'rejected' ? 'red' : 'gold'}">${item.carried_approval ? 'Valid approval carried forward' : esc(decisionLabel)}</span></div><h3>${esc(item.text)}</h3><div class="review-tags">${flags.length ? flags.map((flag) => `<span class="issue-tag ${flag}">${esc(flagNames[flag])}${item.auto_flags.includes(flag) ? ' · Automatic' : ''}</span>`).join('') : '<span class="muted-caption">No risk flags</span>'}</div>${conditions}${evidence}${writable ? `<div class="review-controls"><label>Review decision<select class="review-decision">${Object.entries(decisionNames).map(([value,name]) => `<option value="${value}" ${review.decision===value?'selected':''}>${name}</option>`).join('')}</select></label><fieldset><legend>Manual flags</legend>${Object.entries(flagNames).map(([value,name]) => `<label><input class="review-flag" type="checkbox" value="${value}" ${review.flags.includes(value)?'checked':''}> ${name}</label>`).join('')}</fieldset><label class="review-note-label">Review note<textarea class="review-note" rows="2" maxlength="4000" placeholder="Record rationale, open checks, or suggested changes">${esc(review.note)}</textarea></label><button class="primary-action save-review" type="button">Save review</button></div>` : `<p class="muted-caption">${review.username ? `Reviewer: ${esc(review.username)} · ${esc(review.note)}` : 'No human review yet'}</p>`}</article>`;
}

async function renderManage(token) {
  setHeader('manage');
  const role = state.me.user.role;
  if ((role === 'viewer' || state.me.quarantined_sources?.length) && manageTab === 'research') manageTab = 'reviews';
  main.innerHTML = `${pageIntro('WORKSPACE','System management','Review compiled claims, flag issues, and manage Markdown sources and team members.')}
    <div class="page-loading"><span class="loading-orbit"></span><p>Loading review records...</p></div>`;
  [manageData, {candidate: candidateData}] = await Promise.all([getJSON('/api/manage/reviews'),getJSON('/api/manage/candidate')]);
  if (token !== state.navToken) return;
  if (!manageData.release && candidateData) manageTab = 'candidate';
  if (!candidateData && manageTab === 'candidate') manageTab = 'reviews';
  const tabs = `<div class="manage-tabs" role="tablist">${candidateData ? `<button data-manage-tab="candidate" class="${manageTab==='candidate'?'active':''}">Pending candidate ${candidateData.stats.needs_review}</button>` : ''}<button data-manage-tab="reviews" class="${manageTab==='reviews'?'active':''}">Claim review</button><button data-manage-tab="history" class="${manageTab==='history'?'active':''}">Review history ${manageData.stats.stale_reviews}</button>${role !== 'viewer' ? `${manageData.release ? `<button data-manage-tab="ontology" class="${manageTab==='ontology'?'active':''}">Ontology and relations</button>` : ''}${manageData.release && !state.me.quarantined_sources?.length ? `<button data-manage-tab="research" class="${manageTab==='research'?'active':''}">Research and material requests</button>` : ''}<button data-manage-tab="markdown" class="${manageTab==='markdown'?'active':''}">Markdown materials</button>` : ''}${role === 'admin' ? `${manageData.release ? `<button data-manage-tab="incidents" class="${manageTab==='incidents'?'active':''}">Source corrections</button>` : ''}<button data-manage-tab="users" class="${manageTab==='users'?'active':''}">User management</button><button data-manage-tab="audit" class="${manageTab==='audit'?'active':''}">Audit log</button>` : ''}</div>`;
  main.innerHTML = `${pageIntro('WORKSPACE','System management','Review compiled claims, flag issues, and manage Markdown sources and team members.')}
    ${notice(state.me.quarantined_sources?.length ? 'Source quarantine is active. Knowledge browsing, search, and answers are paused. Review the impact under Source corrections, rebuild, verify, then release quarantine.' : 'Review decisions are saved independently. A candidate reaches readers only after human approval and administrator publication. Existing approvals carry forward only when topic inputs and claim fingerprints match exactly.')}
    <div class="stats-grid">${stat('Total claims',manageData.stats.total,'Current published release')}${stat('Pending review',manageData.stats.pending,'No review decision yet')}${stat('Valid approvals',manageData.stats.approved,'Approved with no risk flags')}${stat('Flagged items',manageData.stats.flagged,'Automatic or manual flags')}</div>${tabs}<div id="manage-content"></div>`;
  await renderManageContent();
}

async function renderManageContent() {
  const panel = $('#manage-content');
  if (!panel) return;
  if (manageTab === 'candidate') {
    const data = candidateData;
    if (!data) {panel.innerHTML = '<div class="empty-state">No pending candidate release.</div>';return;}
    const items = [...data.items].sort((a,b) => Number(a.approved) - Number(b.approved));
    panel.innerHTML = `${notice('The candidate is not visible to readers. Verify claims, conditions, rationale, and original excerpts. Resolve automatic conflicts or inconsistencies and rebuild before publication.')}
      <div class="stats-grid">${stat('Candidate claims',data.stats.total,'Verify each item')}${stat('Reviews needed',data.stats.needs_review,'Publication gate')}${stat('Carried approvals',data.stats.carried,'Topic inputs match exactly')}${stat('Changed topics',data.stats.changed_topics,'Requires new review')}</div>
      <p class="panel-sub">Candidate release ${esc(data.release.slice(0,12))} · Based on ${esc(data.base_release?.slice(0,12) || 'Initial build')}</p>
      <div class="panel"><h3>Release changes</h3><div class="data-list">${Object.entries({sources_added:'Added sources',sources_removed:'Removed sources',sources_changed:'Changed sources',topics_added:'Added topics',topics_removed:'Removed topics',topics_changed:'Changed topics'}).map(([key,title])=>`<div class="data-row"><span><strong>${title} · ${data.changes[key].length}</strong><small>${esc(data.changes[key].join(', ') || 'None')}</small></span></div>`).join('')}</div></div>
      <div class="review-list">${items.map(item=>reviewCard(item,data.release,true)).join('') || '<div class="empty-state">Candidate has no claims.</div>'}</div>
      ${state.me.user.role === 'admin' ? `<div class="panel" style="margin-top:20px"><h3>Publication decision</h3><p class="panel-sub">${data.ready ? 'All candidate claims have valid approvals.' : `There are ${data.stats.needs_review} claims not yet approved.`}</p><button id="publish-candidate" class="primary-action" type="button" ${data.ready?'':'disabled'}>Publish candidate release</button><form id="discard-candidate-form" style="margin-top:18px"><label class="form-field">Reason for discarding<textarea name="reason" rows="2" minlength="3" maxlength="2000" required></textarea></label><button class="mini-action" type="submit">Discard candidate release</button><p class="form-message" role="status"></p></form></div>` : ''}`;
  } else if (manageTab === 'reviews') {
    const items = manageData.items.filter((item) => manageFilter === 'all' ||
      (manageFilter === 'pending' && (!item.review || item.review.decision === 'pending')) ||
      (manageFilter === 'flagged' && (item.auto_flags.length || item.review?.flags?.length)) ||
      item.review?.decision === manageFilter);
    panel.innerHTML = `<div class="filter-row manage-filters">${[['all','All'],['pending','Pending review'],['flagged','Flagged'],['approved','Approved'],['needs_changes','Needs changes'],['rejected','Rejected']].map(([key,label]) => `<button class="filter-btn ${manageFilter===key?'active':''}" data-manage-filter="${key}">${label}</button>`).join('')}</div><p class="panel-sub">Current release ${esc(manageData.release?.slice(0,12) || 'No published release')} · Showing ${items.length} items</p><div class="review-list">${items.map(item=>reviewCard(item)).join('') || '<div class="empty-state">No claims match this filter.</div>'}</div>`;
  } else if (manageTab === 'ontology') {
    await renderEngineerWorkbench(panel);
  } else if (manageTab === 'history') {
    panel.innerHTML = `${notice('Reviews from an older release do not approve a new release automatically. The records below are stale; review current claims again.')}<div class="data-list" style="margin-top:18px">${manageData.history.map((row) => `<div class="data-row"><span><strong>${esc(shortId(row.claim_id))} · ${esc(decisionNames[row.decision])} <span class="issue-tag stale">Stale</span></strong><small>Release ${esc(row.release.slice(0,12))} · ${esc(row.username)} · ${new Date(row.updated_at*1000).toLocaleString('en-US')}</small><small>${esc(row.note)}</small></span></div>`).join('') || '<div class="empty-state">No previous-release reviews.</div>'}</div>`;
  } else if (manageTab === 'research') {
    const data = await getJSON('/api/manage/research');
    const topics = [...new Map(manageData.items.map(item=>[item.topic_id,item.topic])).entries()];
    panel.innerHTML = `${notice('The model generates unverified hypotheses and material requests only. Submitted materials stay in the research workspace. Model assessment and human acceptance do not write published knowledge. Research tasks are tied to a release and require replanning after it changes.')}
      ${data.suggestions.length?`<div class="panel" style="margin:18px 0"><h3>Open gaps identified by the system</h3><p class="panel-sub">From the current release model report. Select a gap to populate the question; a human decides whether to pursue it.</p><div class="data-list">${data.suggestions.map(gap=>`<div class="data-row"><span><strong>${esc(gap.topic)}</strong><small>${esc(gap.question)}</small></span><button class="mini-action research-gap" type="button" data-topic-id="${esc(gap.topic_id)}" data-question="${esc(gap.question)}">Research this gap</button></div>`).join('')}</div></div>`:''}
      <div class="panel" style="margin:18px 0"><h3>Propose a research question</h3><form id="research-plan-form"><label class="form-field">Question or current knowledge gap<textarea name="question" rows="2" minlength="3" maxlength="1000" required></textarea></label><label class="form-field">Limit to topic<select name="topic_id"><option value="">Search by question</option>${topics.map(([id,title])=>`<option value="${esc(id)}">${esc(title)}</option>`).join('')}</select></label><button class="primary-action" type="submit">Generate material and validation requests</button><p class="form-message" role="status">A new question calls the model at most twice; the local token budget limits usage.</p></form></div>
      <div class="review-list">${data.tasks.map(researchCard).join('') || '<div class="empty-state">No research tasks yet. Start with a clear knowledge gap.</div>'}</div>`;
  } else if (manageTab === 'markdown') {
    const data = await getJSON('/api/manage/markdown');
    panel.innerHTML = `<div class="detail-layout"><div><div class="panel"><h3>Import Markdown</h3><p class="panel-sub">Select a UTF-8 .md file and review its content before adding it to raw/. Build and validate before it appears in the published knowledge base.</p><form id="markdown-form"><label class="form-field">Select file<input id="markdown-file" type="file" accept=".md,.markdown,text/markdown" required></label><div id="markdown-preview" class="markdown-preview">Select a file to preview its text</div><button class="primary-action" type="submit">Add to materials pending build</button><p id="markdown-message" class="form-message" role="status"></p></form></div></div><aside class="side-panel"><h3>Markdown files</h3><div class="file-list">${data.files.map((file) => `<div class="file-row"><strong>${esc(file.name)}</strong><span>${file.published?'Published':'Pending build'} · ${(file.size/1024).toFixed(1)} KB</span></div>`).join('') || '<p class="panel-sub">No Markdown files yet</p>'}</div></aside></div>`;
  } else if (manageTab === 'incidents') {
    const [registry, listed] = await Promise.all([getJSON('/api/manage/incidents'), getJSON('/api/manage/sources')]);
    const active = Object.entries(registry.active);
    panel.innerHTML = `${notice('Quarantine immediately pauses knowledge reads without calling a model. During a build, quarantined sources are treated as withdrawn. Release quarantine only after publication and verification that no evidence from that source remains.')}
      <div class="detail-layout" style="margin-top:18px"><div><div class="panel"><h3>Quarantine source</h3><form id="quarantine-form"><label class="form-field">Source<select name="source_id" required>${listed.sources.filter((s)=>!registry.active[s.id]).map((s)=>`<option value="${esc(s.id)}">${esc(s.title)} · ${esc(s.path)}</option>`).join('')}</select></label><label class="form-field">Reason<textarea name="reason" rows="2" minlength="3" maxlength="2000" required></textarea></label><button class="mini-action" type="button" id="preview-impact">Preview impact</button> <button class="primary-action" type="submit">Quarantine now</button><p class="form-message" role="status"></p></form><div id="impact-preview" class="panel-sub"></div></div></div><aside class="side-panel"><h3>Active quarantines · ${active.length}</h3>${active.map(([id,row])=>`<div class="panel" style="margin-bottom:12px"><strong>${esc(row.source_path)}</strong><p class="panel-sub">${esc(row.reason)}</p><p class="panel-sub">${row.topics.length} topics · ${row.topics.reduce((n,t)=>n+t.claims_at_risk,0)} claims need review</p><form class="resolve-form" data-source-id="${esc(id)}"><label class="form-field">Verification and resolution note<textarea name="reason" rows="2" minlength="3" maxlength="2000" required></textarea></label><button class="mini-action" type="submit">Release quarantine</button><p class="form-message" role="status"></p></form></div>`).join('') || '<p class="panel-sub">No quarantined sources.</p>'}</aside></div>`;
  } else if (manageTab === 'users') {
    const data = await getJSON('/api/manage/users');
    panel.innerHTML = `<div class="detail-layout"><div><div class="panel"><h3>Team members</h3><div class="file-list">${data.users.map((user) => `<div class="user-row"><span><strong>${esc(user.username)}</strong><small>${user.active?'Active':'Inactive'}</small></span><select data-user-role="${user.id}" data-active="${user.active}" aria-label="${esc(user.username)} role">${['admin','reviewer','viewer'].map((role) => `<option value="${role}" ${user.role===role?'selected':''}>${roleNames[role]}</option>`).join('')}</select><button class="mini-action" type="button" data-user-toggle="${user.id}" data-active="${user.active?'0':'1'}">${user.active?'Disable':'Enable'}</button></div>`).join('')}</div></div></div><aside class="side-panel"><h3>Add user</h3><form id="user-form"><label class="form-field">Username<input name="username" minlength="3" maxlength="40" autocomplete="off" required></label><label class="form-field">Initial password (at least 12 characters)<input name="password" type="password" minlength="12" autocomplete="new-password" required></label><label class="form-field">Role<select name="role"><option value="viewer">Read-only user</option><option value="reviewer">Reviewer</option><option value="admin">Administrator</option></select></label><button class="primary-action" type="submit">Create user</button><p class="form-message" role="status"></p></form></aside></div>`;
  } else if (manageTab === 'audit') {
    const data = await getJSON('/api/manage/audit');
    panel.innerHTML = `<div class="panel"><h3>Recent actions</h3><div class="data-list">${data.events.map((event) => `<div class="data-row"><span><strong>${esc(event.action)} · ${esc(event.target)}</strong><small>${esc(event.username)} · ${new Date(event.created_at*1000).toLocaleString('en-US')}</small></span></div>`).join('')}</div></div>`;
  }
}

document.addEventListener('click', async (event) => {
  if (event.target.id === 'publish-candidate') {
    const button = event.target;
    button.disabled = true;
    try {
      await apiPost('/api/manage/candidate/publish',{});
      state.me = await getJSON('/api/me');state.overview = null;state.cache.clear();
      candidateData = null;manageTab = 'reviews';
      showToast('Candidate release published');await renderRoute();
    } catch(error) {showToast(error.message);button.disabled=false;}
  }
  const critiqueButton = event.target.closest('.research-critique');
  if (critiqueButton) {
    critiqueButton.disabled = true;
    try {
      await apiPost('/api/manage/research/critique',{task_id:critiqueButton.dataset.taskId});
      showToast('Counter-evidence check recorded');await renderManageContent();
    } catch(error) {showToast(error.message);critiqueButton.disabled=false;}
  }
  const gapButton = event.target.closest('.research-gap');
  if (gapButton) {
    const form = $('#research-plan-form');
    form.elements.question.value = gapButton.dataset.question;
    form.elements.topic_id.value = gapButton.dataset.topicId;
    form.scrollIntoView({behavior:'smooth',block:'start'});
  }
  const assessButton = event.target.closest('.research-assess');
  if (assessButton) {
    assessButton.disabled = true;
    try {
      await apiPost('/api/manage/research/assess',{task_id:assessButton.dataset.taskId});
      showToast('Assessment recorded; human review is still required');
      await renderManageContent();
    } catch(error) {showToast(error.message);assessButton.disabled=false;}
  }
  if (event.target.id === 'preview-impact') {
    const form = $('#quarantine-form');
    try {
      const report = await getJSON(`/api/manage/impact?source_id=${encodeURIComponent(form.elements.source_id.value)}`);
      $('#impact-preview').innerHTML = `<strong>Impact scope</strong> · ${report.evidence_ids.length} evidence items, ${report.topics.length} topics: ${report.topics.map((t)=>`${esc(t.title)} (${t.claims_at_risk} claims)`).join(', ') || 'No published evidence found'}. Another ${report.related_sources_to_check.length} sources sharing topics need investigation, of which ${report.related_sources_to_check.filter((s)=>s.same_declared_origin).length} declare a shared origin.`;
    } catch (error) {showToast(error.message);}
  }
  const tab = event.target.closest('[data-manage-tab]');
  if (tab) {manageTab = tab.dataset.manageTab; document.querySelectorAll('[data-manage-tab]').forEach((node) => node.classList.toggle('active',node===tab)); await renderManageContent();}
  const filter = event.target.closest('[data-manage-filter]');
  if (filter) {manageFilter = filter.dataset.manageFilter; await renderManageContent();}
  const save = event.target.closest('.save-review');
  if (save) {
    const card = save.closest('[data-claim]');
    save.disabled = true;
    try {
      await apiPost('/api/manage/review',{release:card.dataset.release,claim_id:card.dataset.claim,
        version:Number(card.dataset.version),
        decision:card.querySelector('.review-decision').value,
        flags:[...card.querySelectorAll('.review-flag:checked')].map((node) => node.value),
        note:card.querySelector('.review-note').value});
      showToast('Review saved');
      state.cache.clear();
      await renderManage(state.navToken);
    } catch (error) {
      if (error.status === 409) {
        await renderManage(state.navToken);
        showToast('Review changed by another user or release. Reload and inspect it.');
      } else {showToast(error.message);save.disabled=false;}
    }
  }
  const toggle = event.target.closest('[data-user-toggle]');
  if (toggle) {
    const id = Number(toggle.dataset.userToggle);
    const role = document.querySelector(`[data-user-role="${id}"]`).value;
    try {await apiPost('/api/manage/user',{id,role,active:toggle.dataset.active==='1'});showToast('User updated');await renderManageContent();}
    catch(error) {showToast(error.message);}
  }
});
document.addEventListener('change', async (event) => {
  if (event.target.id === 'markdown-file') {
    const file = event.target.files[0];
    $('#markdown-preview').textContent = file ? (await file.text()).slice(0,8000) : 'Select a file to preview its text';
  }
  const role = event.target.closest('[data-user-role]');
  if (role) {
    try {await apiPost('/api/manage/user',{id:Number(role.dataset.userRole),role:role.value,active:role.dataset.active==='1'});showToast('Role updated');await renderManageContent();}
    catch(error) {showToast(error.message);await renderManageContent();}
  }
});
document.addEventListener('submit', async (event) => {
  if (event.target.id === 'discard-candidate-form') {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');button.disabled = true;
    try {
      await apiPost('/api/manage/candidate/discard',{reason:form.elements.reason.value});
      state.me = await getJSON('/api/me');candidateData = null;manageTab = 'reviews';
      showToast('Candidate discarded; its record remains');await renderRoute();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
  if (event.target.id === 'research-plan-form') {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    form.querySelector('.form-message').textContent = 'The model is analyzing evidence and knowledge gaps...';
    try {
      await apiPost('/api/manage/research/plan',{question:form.elements.question.value,topic_id:form.elements.topic_id.value||null});
      showToast('Research request generated');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
  if (event.target.matches('.research-material-form')) {
    event.preventDefault();
    const form = event.target;
    const file = form.elements.file.files[0];
    if (!file) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await apiPost('/api/manage/research/material',{task_id:form.dataset.taskId,version:Number(form.dataset.version),filename:file.name,content:await file.text()});
      showToast('Material saved to the separate research workspace');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
  if (event.target.matches('.research-decision-form')) {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await apiPost('/api/manage/research/decision',{task_id:form.dataset.taskId,version:Number(form.dataset.version),decision:form.elements.decision.value,note:form.elements.note.value});
      showToast('Decision recorded; published knowledge is unchanged');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
  if (event.target.id === 'quarantine-form') {
    event.preventDefault();
    const form = event.target;
    try {
      await apiPost('/api/manage/quarantine',{source_id:form.elements.source_id.value,reason:form.elements.reason.value});
      state.cache.clear();state.overview=null;state.me=await getJSON('/api/me');
      showToast('Source quarantined; knowledge reads are paused');
      await renderManage(state.navToken);
    } catch(error) {form.querySelector('.form-message').textContent=error.message;}
  }
  if (event.target.matches('.resolve-form')) {
    event.preventDefault();
    const form = event.target;
    try {
      await apiPost('/api/manage/resolve',{source_id:form.dataset.sourceId,reason:form.elements.reason.value});
      state.cache.clear();state.overview=null;state.me=await getJSON('/api/me');
      showToast('Quarantine released; inspect the new release');
      await renderManage(state.navToken);
    } catch(error) {form.querySelector('.form-message').textContent=error.message;}
  }
  if (event.target.id === 'markdown-form') {
    event.preventDefault();
    const file = $('#markdown-file').files[0];
    const message = $('#markdown-message');
    if (!file) return;
    try {await apiPost('/api/manage/markdown',{filename:file.name,content:await file.text()});message.textContent='Upload succeeded. Awaiting build and publication.';showToast(message.textContent);await renderManageContent();}
    catch(error) {message.textContent=error.message;}
  }
  if (event.target.id === 'user-form') {
    event.preventDefault();
    const form = event.target;
    const fields = new FormData(form);
    try {await apiPost('/api/manage/users',{username:fields.get('username'),password:fields.get('password'),role:fields.get('role')});showToast('User created');await renderManageContent();}
    catch(error) {form.querySelector('.form-message').textContent=error.message;}
  }
});
