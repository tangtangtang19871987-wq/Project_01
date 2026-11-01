/* Published ontology explorer and separate reviewed relation workspace. */
let ontologyPublicData = null;

function ontologyNodeLabel(nodes, id) {
  return nodes[id]?.label || id;
}

function ontologyFactCard(fact, nodes) {
  const title = fact.predicate === 'rdf:type' ? 'Type' : fact.predicate;
  const object = fact.predicate === 'rdf:type' ? fact.object : ontologyNodeLabel(nodes, fact.object);
  const mode = fact.rule === 'approved_relation' ? 'Human approved' : fact.rule.startsWith('rdfs:') ? 'Rule derived' : 'Compiled structure';
  return `<div class="ontology-fact"><div class="ontology-fact-main"><strong>${esc(ontologyNodeLabel(nodes,fact.subject))}</strong><span>${esc(title)} →</span><strong>${esc(object)}</strong></div><small>${esc(mode)} · ${esc(fact.rule)} · ${esc(shortId(fact.id))}</small>${fact.premises.length?`<small>Premises: ${fact.premises.map(esc).join(', ')}</small>`:''}${fact.evidence_ids.length?`<div class="evidence-links">${fact.evidence_ids.map(id=>`<a class="evidence-chip" href="${href('evidence',id)}">${esc(shortId(id))} ↗</a>`).join('')}</div>`:''}</div>`;
}

function renderOntologyNeighborhood(nodeId) {
  const panel = $('#ontology-neighborhood');
  if (!panel || !ontologyPublicData?.graph) return;
  const graph = ontologyPublicData.graph;
  const facts = graph.facts.filter(fact => fact.predicate !== 'rdf:type' &&
    (fact.subject === nodeId || fact.object === nodeId));
  panel.innerHTML = `<h3>${esc(ontologyNodeLabel(graph.nodes,nodeId))}</h3><p class="panel-sub">${esc(graph.nodes[nodeId]?.class || '')} · ${facts.length} direct or inferred relations</p><div class="ontology-facts">${facts.slice(0,80).map(fact=>ontologyFactCard(fact,graph.nodes)).join('') || '<p class="panel-sub">No relations yet.</p>'}</div>${facts.length>80?'<p class="panel-sub">Showing the first 80 only. Select a more specific node.</p>':''}`;
}

async function renderOntologyPublic(token) {
  setHeader('semantics');
  const data = await getJSON('/api/ontology');
  if (token !== state.navToken) return;
  ontologyPublicData = data;
  if (data.status !== 'published') {
    main.innerHTML = `${pageIntro('SEMANTIC LAYER','Semantic graph','Use a controlled ontology to organize types and relations in published knowledge.')}
      <div class="empty-state"><h3>${data.status==='stale'?'Semantic snapshot suspended':'No semantic snapshot yet'}</h3><p>${esc(data.reason || 'An administrator must review relations and publish a semantic snapshot.')}</p>${state.me.user.role!=='viewer'?'<a class="mini-action" href="#/manage">Open System management</a>':''}</div>`;
    return;
  }
  const graph = data.graph;
  const semanticFacts = graph.facts.filter(fact => fact.rule === 'approved_relation' ||
    (fact.rule === 'rdfs:subPropertyOf' && fact.predicate === 'relatedTo'));
  const options = Object.entries(graph.nodes).filter(([id])=>id.startsWith('claim:') || id.startsWith('entity:'));
  main.innerHTML = `${pageIntro('SEMANTIC LAYER','Semantic graph','Browse reviewed relations and explicit rule deductions; each relation traces to original evidence.','<a class="mini-action" href="/api/ontology.nt" download="iltkb-ontology.nt">Export RDF ↗</a>')}
    ${notice('This limited RDFS subset applies subclass, subproperty, domain, and range rules, plus OWL disjoint-class checks. A rule deduction does not establish scientific truth.')}
    <p class="panel-sub">This snapshot includes ${graph.stats.approved_claims} valid approved claims; ${graph.stats.omitted_claims} unapproved or invalid claims were excluded from the semantic graph.</p>
    <div class="stats-grid">${stat('Graph nodes',graph.stats.nodes,'Claims, evidence, sources, and concepts')}${stat('Approved relations',graph.stats.asserted_relations,'Human reviewed and published')}${stat('Rule deductions',graph.stats.derived_facts,'Rules and premises shown')}${stat('Consistency issues',graph.issues.length,'Disjoint-type checks')}</div>
    <div class="panel"><h3>Browse relations</h3><p class="panel-sub">Select a claim or domain concept to see connected relations. Structural relations come from the compiler; semantic relations require human approval.</p><select id="ontology-node-select" aria-label="Select a graph node">${options.map(([id,node])=>`<option value="${esc(id)}">${esc(node.label.slice(0,85))} · ${esc(node.class)}</option>`).join('')}</select><div id="ontology-neighborhood" style="margin-top:18px"></div></div>
    <div class="section-heading"><div><h2>Approved semantic relations and direct deductions</h2><p>${semanticFacts.length} relations · snapshot ${esc(data.snapshot.slice(0,12))}</p></div></div>
    <div class="ontology-facts">${semanticFacts.map(fact=>ontologyFactCard(fact,graph.nodes)).join('') || '<div class="empty-state">This snapshot contains only structural source and evidence relations.</div>'}</div>
    <div class="panel" style="margin-top:20px"><h3>Ontology vocabulary</h3><div class="ontology-vocab">${Object.entries(data.schema.classes).map(([id,row])=>`<div><strong>${esc(row.label)}</strong><small>${esc(id)}${row.parent?` → ${esc(row.parent)}`:''}</small></div>`).join('')}</div></div>`;
  if (options.length) renderOntologyNeighborhood(options[0][0]);
}

async function renderOntologyManage(panel) {
  const data = await getJSON('/api/manage/ontology');
  if (!panel.isConnected || manageTab !== 'ontology') return;
  const nodes = data.nodes;
  const claimOptions = Object.entries(nodes).filter(([id])=>id.startsWith('claim:'));
  const targetOptions = Object.entries(nodes);
  const properties = Object.entries(data.schema.properties).filter(([,row])=>row.mode==='curated');
  const options = (rows) => rows.map(([id,node])=>`<option value="${esc(id)}">${esc(node.label.slice(0,80))} · ${esc(node.class)} · ${esc(shortId(id))}</option>`).join('');
  const statusNames = {proposed:'Pending',approved:'Approved',rejected:'Rejected'};
  panel.innerHTML = `${notice('This controlled ontology pilot uses RDFS for type and property deductions and OWL disjointness checks to report contradictions. Proposers cannot approve their own relations; administrators publish snapshots separately.')}
    <div class="stats-grid">${stat('Ontology classes',Object.keys(data.schema.classes).length,'Can be expanded gradually')}${stat('Properties',Object.keys(data.schema.properties).length,'Structural and curated semantics')}${stat('Approved relations',data.relations.filter(row=>row.status==='approved').length,'Current release and schema')}${stat('Previous-version records',data.stale_count,'Automatically isolated')}</div>
    <div class="panel"><h3>Ontology and version status</h3><p class="panel-sub">release ${esc(data.release.slice(0,12))} · schema ${esc(data.schema_hash.slice(0,12))} · reader snapshot ${esc(data.published.status)}. Edit schema/ontology.json to add classes, properties, disjointness, or concepts. A schema hash change invalidates old relations, which then need review.</p><p class="panel-sub">Current graph: ${data.preview.stats.nodes} nodes, ${data.preview.stats.facts} facts, ${data.preview.stats.derived_facts} rule deductions; includes only ${data.preview.stats.approved_claims} valid approved claims and omits ${data.preview.stats.omitted_claims} claims.</p>${data.preview.stats.approved_claims? '':'<p class="issue-tag conflict">Approve at least one current claim before publishing a semantic snapshot.</p>'}${data.preview.blocked.map(row=>`<p class="issue-tag conflict">Relation ${esc(shortId(row.id))} is invalid: ${esc(row.reason)}</p>`).join('')}${data.preview.issues.map(row=>`<p class="issue-tag conflict">Disjoint types: ${esc(row.node)} · ${esc(row.classes.join(' / '))}</p>`).join('')}
      ${state.me.user.role==='admin'?`<button id="ontology-publish" class="primary-action" type="button" ${data.ready && !state.me.quarantined_sources?.length?'':'disabled'}>Publish semantic snapshot</button>`:''}</div>
    <div class="panel" style="margin-top:18px"><h3>Propose a relation</h3><p class="panel-sub">Use nodes from the current release. Select at least one original evidence item for each claim; the relation still requires human judgment.</p><form id="ontology-propose-form"><label class="form-field">Subject claim<select name="subject" required>${options(claimOptions)}</select></label><label class="form-field">Relation property<select name="predicate" required>${properties.map(([id,row])=>`<option value="${esc(id)}">${esc(row.label)} · ${esc(id)} (${esc(row.domain)} → ${esc(row.range)})</option>`).join('')}</select></label><label class="form-field">Object claim or concept<select name="object" required>${options(targetOptions)}</select></label><label class="form-field">Original evidence IDs (comma separated)<input name="evidence_ids" placeholder="E... , E..." required></label><label class="form-field">Rationale and scope for this relation<textarea name="note" rows="3" minlength="3" maxlength="4000" required></textarea></label><button class="primary-action" type="submit">Save pending proposal</button><p class="form-message" role="status"></p></form></div>
    <div class="section-heading"><div><h2>Relation proposals</h2><p>${data.relations.length} items · bound to the current release and schema</p></div></div>
    <div class="review-list">${data.relations.map(row=>`<article class="review-card"><div class="review-card-head"><span class="claim-number">${esc(shortId(row.id))}</span><span class="tag ${row.status==='approved'?'mint':row.status==='rejected'?'red':'gold'}">${esc(statusNames[row.status])}</span></div><h3>${esc(ontologyNodeLabel(nodes,row.subject))} <span class="ontology-arrow">${esc(data.schema.properties[row.predicate]?.label || row.predicate)} →</span> ${esc(ontologyNodeLabel(nodes,row.object))}</h3><p class="panel-sub">Proposal rationale: ${esc(row.note)}</p><p class="panel-sub">Original evidence: ${row.evidence_ids.map(esc).join(', ')}</p>${row.decision_note?`<p class="panel-sub">Review note: ${esc(row.decision_note)}</p>`:''}${row.status!=='rejected'?`<form class="ontology-decision-form" data-id="${esc(row.id)}" data-version="${row.version}"><label class="form-field">Decision<select name="decision"><option value="approved">Approve</option><option value="rejected">Reject or revoke approval</option></select></label><label class="form-field">Review note<textarea name="note" rows="2" minlength="3" maxlength="4000" required></textarea></label><button class="mini-action" type="submit">Save decision</button><p class="form-message" role="status"></p></form>`:''}</article>`).join('') || '<div class="empty-state">No relation proposals yet. Review the ontology vocabulary and claim evidence first.</div>'}</div>`;
}

document.addEventListener('change', event => {
  if (event.target.id === 'ontology-node-select') renderOntologyNeighborhood(event.target.value);
});

document.addEventListener('click', async event => {
  if (event.target.id !== 'ontology-publish') return;
  const button = event.target;button.disabled = true;
  try {const result = await apiPost('/api/manage/ontology/publish',{});showToast(`Semantic snapshot published: ${result.relations} approved relations`);await renderManageContent();}
  catch(error) {showToast(error.message);button.disabled=false;}
});

document.addEventListener('submit', async event => {
  if (event.target.id === 'ontology-propose-form') {
    event.preventDefault();
    const form = event.target;const button = form.querySelector('button[type="submit"]');button.disabled = true;
    try {
      await apiPost('/api/manage/ontology/propose',{subject:form.elements.subject.value,
        predicate:form.elements.predicate.value,object:form.elements.object.value,
        evidence_ids:form.elements.evidence_ids.value.split(',').map(value=>value.trim()).filter(Boolean),
        note:form.elements.note.value});
      showToast('Relation proposal saved but not published');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
  if (event.target.matches('.ontology-decision-form')) {
    event.preventDefault();
    const form = event.target;const button = form.querySelector('button[type="submit"]');button.disabled = true;
    try {
      await apiPost('/api/manage/ontology/decision',{id:form.dataset.id,version:Number(form.dataset.version),
        decision:form.elements.decision.value,note:form.elements.note.value});
      showToast('Decision saved. The reader graph updates after an administrator publishes a snapshot.');await renderManageContent();
    } catch(error) {form.querySelector('.form-message').textContent=error.message;button.disabled=false;}
  }
});
