/* Engineer-facing projection of the existing ontology review workflow. */
let engineerData = null;
let engineerFilter = 'all';
let engineerQuery = '';
let engineerSelected = null;
let engineerDetailToken = 0;
let engineerEvidenceToken = 0;

function engineerNode(id) {
  return engineerData?.nodes[id] || {label:id, class:'Unknown', evidence:[]};
}

function engineerIsA(actual, expected) {
  const classes = engineerData.schema.classes;
  while (actual && classes[actual]) {
    if (actual === expected) return true;
    actual = classes[actual].parent;
  }
  return false;
}

function engineerReview(nodeId) {
  if (!nodeId.startsWith('claim:')) return 'Controlled domain concept';
  const id = nodeId.slice(6);
  const item = manageData.items.find(row => row.id === id);
  if (!item) return 'Current review not found';
  if (item.review?.decision === 'approved' && !item.review.flags.length && !item.auto_flags.length) return 'Claim approved';
  if (item.review?.decision === 'approved') return 'Approval has risks or is invalid';
  if (item.review?.decision === 'rejected') return 'Claim rejected';
  return 'Claim pending review';
}

function engineerEndpoint(id, role) {
  const node = engineerNode(id);
  return `<div class="engineer-endpoint"><small>${role} · ${esc(node.class)} · ${esc(engineerReview(id))}</small><strong>${esc(node.label)}</strong>${id.startsWith('claim:')?`<a href="${href('topic',node.topic)}">View topic and claim ↗</a>`:''}</div>`;
}

function engineerEvidenceCard(row, side, selected = false) {
  const source = row.source || {};
  const place = row.page ? `PDF page ${row.page}` : row.section || 'Original location';
  return `<article class="engineer-evidence"><div class="engineer-evidence-head"><span>${esc(side)} · ${esc(source.title || 'Original source')} · ${esc(place)}</span><a href="${href('evidence',row.id)}">View original evidence ↗</a></div><p>${esc(row.statement)}</p><blockquote>${esc(row.quote || '')}</blockquote>${selected?'<small>This relation cites this evidence</small>':''}</article>`;
}

async function engineerLoadEvidence(ids) {
  return Promise.all(ids.map(async id => {
    try { return await getJSON(`/api/evidence/${encodeURIComponent(id)}`, `evidence:${id}`); }
    catch (error) { return {id, error:error.message}; }
  }));
}

function engineerQueue() {
  const list = $('#engineer-relation-list');
  if (!list || !engineerData) return;
  const blocked = new Map(engineerData.preview.blocked.map(row => [row.id,row.reason]));
  const visible = engineerData.relations.filter(row =>
    (engineerFilter === 'all' || row.status === engineerFilter ||
      (engineerFilter === 'blocked' && blocked.has(row.id))) &&
    (!engineerQuery || `${engineerNode(row.subject).label} ${engineerNode(row.object).label} ${row.predicate} ${row.note}`.toLowerCase().includes(engineerQuery)));
  list.innerHTML = visible.map(row => `<button class="engineer-queue-item ${engineerSelected===row.id?'active':''}" type="button" data-engineer-relation="${esc(row.id)}"><span class="engineer-queue-top"><small>${esc(row.status==='proposed'?'Pending review':row.status==='approved'?'Approved':'Rejected')}</small>${blocked.has(row.id)?'<small class="engineer-warning">Dependencies invalid</small>':''}</span><strong>${esc(engineerNode(row.subject).label)}</strong><span>${esc(engineerData.schema.properties[row.predicate]?.label || row.predicate)} → ${esc(engineerNode(row.object).label)}</span></button>`).join('') || '<div class="empty-state"><h3>No relations match this filter</h3><p>Change the status filter or propose a relation below.</p></div>';
  if (!visible.some(row => row.id === engineerSelected)) engineerSelected = visible[0]?.id || null;
  list.querySelector(`[data-engineer-relation="${engineerSelected}"]`)?.classList.add('active');
  engineerDetail();
}

async function engineerDetail() {
  const panel = $('#engineer-relation-detail');
  if (!panel || !engineerData) return;
  const row = engineerData.relations.find(item => item.id === engineerSelected);
  const token = ++engineerDetailToken;
  if (!row) {
    panel.innerHTML = '<div class="empty-state"><h3>Select a relation to review</h3><p>The proposal, evidence, and review decision appear here.</p></div>';
    return;
  }
  const blocked = engineerData.preview.blocked.find(item => item.id === row.id);
  panel.innerHTML = '<div class="page-loading"><span class="loading-orbit"></span><p>Loading original evidence...</p></div>';
  const evidence = await engineerLoadEvidence(row.evidence_ids);
  if (token !== engineerDetailToken || !panel.isConnected || manageTab !== 'ontology') return;
  const first = new Set(engineerNode(row.subject).evidence || []);
  const second = new Set(engineerNode(row.object).evidence || []);
  const cards = evidence.map(item => item.error
    ? `<p class="issue-tag conflict">Evidence ${esc(item.id)} could not be loaded: ${esc(item.error)}</p>`
    : engineerEvidenceCard(item, first.has(item.id) && second.has(item.id) ? 'Both endpoints' : first.has(item.id) ? 'Subject' : 'Object', true));
  const canDecide = row.status !== 'rejected' && !state.me.quarantined_sources?.length;
  panel.innerHTML = `<div class="engineer-detail-head"><div><span class="eyebrow">RELATION REVIEW</span><h3>Relation review</h3></div><span class="tag ${row.status==='approved'?'mint':row.status==='rejected'?'red':'gold'}">${esc(row.status==='proposed'?'Pending review':row.status==='approved'?'Approved':'Rejected')}</span></div>
    ${blocked?`<p class="issue-tag conflict">Dependencies invalid: ${esc(blocked.reason)}.Revalidate before publishing.</p>`:''}
    <div class="engineer-endpoints">${engineerEndpoint(row.subject,'Subject')}<div class="engineer-predicate">${esc(engineerData.schema.properties[row.predicate]?.label || row.predicate)}<small>${esc(row.predicate)}</small></div>${engineerEndpoint(row.object,'Object')}</div>
    <div class="engineer-rationale"><strong>Proposal rationale and scope</strong><p>${esc(row.note)}</p>${row.decision_note?`<strong>Existing review note</strong><p>${esc(row.decision_note)}</p>`:''}</div>
    <div class="section-heading"><div><h3>Original evidence</h3><p>${evidence.length} items · These show that cited text exists; the relation still needs domain judgment.</p></div></div><div class="engineer-evidence-list">${cards.join('')}</div>
    ${canDecide?`<form id="engineer-decision-form" data-id="${esc(row.id)}" data-version="${row.version}"><h3>Review decision</h3><p class="panel-sub">A proposer cannot approve their own relation. Approval does not publish a semantic snapshot.</p><div class="engineer-decision-fields"><label class="form-field">Decision<select name="decision">${blocked?'':'<option value="approved">Approve relation</option>'}<option value="rejected">Reject or revoke approval</option></select></label><label class="form-field">Rationale and unresolved checks<textarea name="note" rows="3" minlength="3" maxlength="4000" required></textarea></label></div><button class="primary-action" type="submit">Save review decision</button><p class="form-message" role="status"></p></form>`:row.status!=='rejected'?'<p class="issue-tag conflict">Source quarantine prevents complete evidence review; review actions are paused.</p>':''}`;
}

function engineerProposalOptions() {
  const form = $('#engineer-propose-form');
  if (!form || !engineerData) return;
  const prop = form.querySelector('[name="predicate"]').value;
  const definition = engineerData.schema.properties[prop];
  const subject = form.querySelector('[name="subject"]');
  const object = form.querySelector('[name="object"]');
  const oldSubject = subject.value, oldObject = object.value;
  const subjectQuery = form.querySelector('[name="subject_search"]').value.trim().toLowerCase();
  const objectQuery = form.querySelector('[name="object_search"]').value.trim().toLowerCase();
  const options = (expected, query, omit) => Object.entries(engineerData.nodes)
    .filter(([id,node]) => id !== omit && engineerIsA(node.class,expected) &&
      (!query || `${node.label} ${id}`.toLowerCase().includes(query)))
    .map(([id,node]) => `<option value="${esc(id)}">${esc(node.label.slice(0,95))} · ${esc(node.class)}</option>`).join('');
  subject.innerHTML = options(definition.domain,subjectQuery,null);
  if (subject.querySelector(`option[value="${CSS.escape(oldSubject)}"]`)) subject.value = oldSubject;
  object.innerHTML = options(definition.range,objectQuery,subject.value);
  if (object.querySelector(`option[value="${CSS.escape(oldObject)}"]`)) object.value = oldObject;
  engineerProposalEvidence();
}

async function engineerProposalEvidence() {
  const form = $('#engineer-propose-form');
  const panel = $('#engineer-proposal-evidence');
  if (!form || !panel || !engineerData) return;
  const token = ++engineerEvidenceToken;
  const subject = form.querySelector('[name="subject"]').value;
  const object = form.querySelector('[name="object"]').value;
  const first = new Set(engineerNode(subject).evidence || []);
  const second = new Set(engineerNode(object).evidence || []);
  const ids = [...new Set([...first,...second])];
  panel.innerHTML = ids.length ? '<p class="panel-sub">Loading available evidence...</p>' : '<p class="panel-sub">Select both endpoints, then choose original evidence.</p>';
  if (!ids.length) return;
  const rows = await engineerLoadEvidence(ids);
  if (token !== engineerEvidenceToken || !panel.isConnected) return;
  panel.innerHTML = `<p class="panel-sub">Select the original text supporting this relation. For two claims, choose at least one item from each endpoint. Maximum 12 items.</p><div class="engineer-evidence-choices">${rows.map(row => row.error
    ? `<p class="issue-tag conflict">${esc(row.id)} could not be loaded: ${esc(row.error)}</p>`
    : `<label class="engineer-evidence-choice"><input type="checkbox" name="evidence_id" value="${esc(row.id)}"><span><strong>${esc(first.has(row.id)&&second.has(row.id)?'Both endpoints':first.has(row.id)?'Subject':'Object')} · ${esc(row.source?.title || 'Original source')} · ${esc(row.page?`Page ${row.page}`:row.section || 'Original location')}</strong><small>${esc(row.quote || row.statement)}</small></span></label>`).join('')}</div>`;
}

async function renderEngineerWorkbench(panel) {
  const data = await getJSON('/api/manage/ontology');
  if (!panel.isConnected || manageTab !== 'ontology') return;
  engineerData = data;
  const counts = Object.fromEntries(['proposed','approved','rejected'].map(status =>
    [status,data.relations.filter(row => row.status === status).length]));
  const publishedLabel = {published:'Published snapshot',stale:'Snapshot invalid',unpublished:'Not published'}[data.published.status] || data.published.status;
  const publishHint = !counts.approved ? 'No approved relations yet. Complete independent review first.'
    : !data.ready ? 'The semantic release requirements are not met. Resolve claim approvals, invalid dependencies, or conflicts.'
    : data.published.status === 'unpublished' ? `Ready to publish ${counts.approved} approved relations; ${counts.proposed} pending proposals will be excluded from the snapshot.`
    : `There are currently ${counts.approved} approved relations. Readers still see the previous snapshot; publish again to include later approvals. ${counts.proposed} pending proposals will be excluded from the snapshot.`;
  panel.innerHTML = `${notice('The engineer workbench handles relations for the current release and ontology version. Claim approval, relation approval, and snapshot publication are separate decisions. Open each original excerpt for verification.')}
    <div class="stats-grid">${stat('Pending relations',counts.proposed,'Review evidence item by item')}${stat('Approved relations',counts.approved,'Independently reviewed')}${stat('Invalid dependencies',data.preview.blocked.length,'Revalidation required')}${stat('Approved claims',data.preview.stats.approved_claims,`Additionally, ${data.preview.stats.omitted_claims} excluded`)}</div>
    <div class="panel engineer-publish-panel"><div><h3>Current status</h3><p class="panel-sub">Knowledge release ${esc(data.release.slice(0,12))} · Ontology ${esc(data.schema_hash.slice(0,12))} · Reader graph: ${esc(publishedLabel)}${data.published.snapshot?` ${esc(data.published.snapshot.slice(0,12))}`:''}. ${publishHint}</p></div>${state.me.user.role==='admin'?`<button id="engineer-publish" class="primary-action" type="button" ${data.ready&&counts.approved&&!state.me.quarantined_sources?.length?'':'disabled'}>Publish semantic snapshot</button>`:''}</div>
    <div class="section-heading"><div><h2>Relation review</h2><p>Filter by status and select a relation to compare endpoints with original evidence.</p></div></div>
    <div class="engineer-workbench"><aside class="engineer-queue"><label class="form-field">Find a relation<input id="engineer-relation-search" type="search" placeholder="Claim, concept, or relation" value="${esc(engineerQuery)}"></label><div class="engineer-filters">${[['proposed','Pending',counts.proposed],['blocked','Invalid',data.preview.blocked.length],['approved','Approved',counts.approved],['rejected','Rejected',counts.rejected],['all','All',data.relations.length]].map(([key,label,count])=>`<button type="button" data-engineer-filter="${key}" class="${engineerFilter===key?'active':''}">${label} ${count}</button>`).join('')}</div><div id="engineer-relation-list"></div></aside><section class="engineer-detail" id="engineer-relation-detail" aria-live="polite"></section></div>
    <details class="panel engineer-proposal" id="engineer-proposal"><summary>Propose a new relation</summary><p class="panel-sub">Choose two claims or controlled concepts from the current release, then select original evidence. Another reviewer must approve the proposal.</p><form id="engineer-propose-form"><div class="engineer-proposal-grid"><label class="form-field">Relation property<select name="predicate">${Object.entries(data.schema.properties).filter(([,row])=>row.mode==='curated').map(([id,row])=>`<option value="${esc(id)}">${esc(row.label)} · ${esc(id)} (${esc(row.domain)} → ${esc(row.range)})</option>`).join('')}</select></label><label class="form-field">Subject search<input name="subject_search" type="search" placeholder="Search claims"></label><label class="form-field">Subject<select name="subject" required></select></label><label class="form-field">Object search<input name="object_search" type="search" placeholder="Search claims or concepts"></label><label class="form-field">Object<select name="object" required></select></label></div><div id="engineer-proposal-evidence"></div><label class="form-field">Rationale and scope for this relation<textarea name="note" rows="3" minlength="3" maxlength="4000" required></textarea></label><button class="primary-action" type="submit">Save pending proposal</button><p class="form-message" role="status"></p></form></details>`;
  engineerQueue();
  engineerProposalOptions();
}

document.addEventListener('click', async event => {
  const filter = event.target.closest('[data-engineer-filter]');
  if (filter) {
    engineerFilter = filter.dataset.engineerFilter;
    document.querySelectorAll('[data-engineer-filter]').forEach(button => button.classList.toggle('active',button===filter));
    engineerQueue();
  }
  const relation = event.target.closest('[data-engineer-relation]');
  if (relation) {
    engineerSelected = relation.dataset.engineerRelation;
    document.querySelectorAll('[data-engineer-relation]').forEach(button => button.classList.toggle('active',button===relation));
    engineerDetail();
  }
  if (event.target.id === 'engineer-publish') {
    event.target.disabled = true;
    try {const result=await apiPost('/api/manage/ontology/publish',{});showToast(`Published ${result.relations} reviewed relations`);await renderManageContent();}
    catch (error) {showToast(error.message);event.target.disabled=false;}
  }
});

document.addEventListener('input', event => {
  if (event.target.id === 'engineer-relation-search') {
    engineerQuery = event.target.value.trim().toLowerCase();
    engineerQueue();
  }
  if (event.target.matches('#engineer-propose-form [name="subject_search"],#engineer-propose-form [name="object_search"]')) engineerProposalOptions();
});

document.addEventListener('change', event => {
  if (event.target.matches('#engineer-propose-form [name="predicate"]')) engineerProposalOptions();
  if (event.target.matches('#engineer-propose-form [name="subject"]')) engineerProposalOptions();
  if (event.target.matches('#engineer-propose-form [name="object"]')) engineerProposalEvidence();
});

document.addEventListener('submit', async event => {
  if (event.target.id === 'engineer-decision-form') {
    event.preventDefault();
    const form=event.target, button=form.querySelector('button[type="submit"]');button.disabled=true;
    const decision=form.querySelector('[name="decision"]').value;
    try {
      await apiPost('/api/manage/ontology/decision',{id:form.dataset.id,version:Number(form.dataset.version),decision,note:form.querySelector('[name="note"]').value});
      engineerFilter=decision;showToast('Review decision saved. Publish a new reader snapshot separately.');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message==='A relation proposer cannot approve their own proposal'?'A proposer cannot approve their own relation. Ask another reviewer to review it.':error.message;button.disabled=false;}
  }
  if (event.target.id === 'engineer-propose-form') {
    event.preventDefault();
    const form=event.target, button=form.querySelector('button[type="submit"]');button.disabled=true;
    const evidence_ids=[...form.querySelectorAll('[name="evidence_id"]:checked')].map(input=>input.value);
    try {
      const result=await apiPost('/api/manage/ontology/propose',{subject:form.querySelector('[name="subject"]').value,predicate:form.querySelector('[name="predicate"]').value,object:form.querySelector('[name="object"]').value,evidence_ids,note:form.querySelector('[name="note"]').value});
      engineerFilter='proposed';engineerSelected=result.id;showToast('Relation proposal saved. Awaiting independent review.');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
});
