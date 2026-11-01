const $ = (selector) => document.querySelector(selector);
const main = $('#main');
const state = { overview: null, cache: new Map(), navToken: 0, searchTimer: null, searchIndex: 0,
  me: null, pollInFlight: false, connectionLost: false };
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const shortId = (id) => `${id.slice(0, 9)}…${id.slice(-5)}`;
const href = (kind, id) => `#/${kind}/${encodeURIComponent(id)}`;
const label = {source_statement:'Source statement',inference:'Synthesis inference',conflict:'Unresolved conflict',single_source:'Single source',shared_origin:'Shared-origin sources',distinct_declared_origins:'Different declared sources',unknown:'Relationship unknown'};
const topicIcon = {imaging:'◉',optimization:'⌘',manufacturing:'◇',evaluation:'▥',other:'⋯'};

async function getJSON(url, cacheKey) {
  if (cacheKey && state.cache.has(cacheKey)) return state.cache.get(cacheKey);
  const response = await fetch(url, {headers:{Accept:'application/json'}});
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `Request failed (${response.status})`);
  }
  const data = await response.json();
  if (cacheKey) state.cache.set(cacheKey, data);
  return data;
}

async function apiPost(url, body) {
  const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.me?.csrf || ''},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {const error = new Error(data.error || `Request failed (${response.status})`); error.status = response.status; throw error;}
  return data;
}

function renderAuth() {
  const setup = !state.me.enabled;
  const remoteSetup = setup && !state.me.setup_allowed;
  $('#connection-label').textContent = setup ? 'Administrator setup required' : 'Sign in required';
  $('#side-release').textContent = 'Release hidden';
  setHeader('overview', setup ? 'Initial setup' : 'Sign in');
  $('#release-pill').hidden = true;
  $('#search-trigger').hidden = true;
  $('#user-button').hidden = true;
  $('[data-nav="manage"]').hidden = true;
  main.innerHTML = `<div class="auth-wrap"><div class="auth-card"><div class="eyebrow">ILT ATLAS · ${setup?'SETUP':'SIGN IN'}</div><h1>${remoteSetup?'Waiting for administrator setup':setup?'Create an administrator account':'Sign in to the knowledge compiler'}</h1><p>${remoteSetup?'Create the administrator account through the local entry point on the machine running the service, then refresh this page.':setup?'Create an administrator account first, then add reviewers and read-only users in System management.':'Sign in to view knowledge results. An administrator assigns review permissions.'}</p>${remoteSetup?'<button class="mini-action" type="button" id="retry-button">Refresh status</button>':`<form id="auth-form"><label class="form-field">Username<input name="username" autocomplete="username" minlength="3" required></label><label class="form-field">Password<input name="password" type="password" autocomplete="${setup?'new-password':'current-password'}" ${setup?'minlength="12"':''} required></label><button class="primary-action" type="submit">${setup?'Create account and continue':'Sign in'}</button><p class="form-message" role="alert"></p></form>`}</div></div>`;
}

function setHeader(section, title) {
  const names = {overview:'Overview',topics:'Topic map',sources:'Source catalog',coverage:'Coverage and gaps',semantics:'Semantic graph',manage:'System management'};
  const parent = names[section] || 'Overview';
  $('#breadcrumbs').innerHTML = `Knowledge compiler <span>/</span> ${esc(title || parent)}`;
  document.title = `${title || parent} · ILT Atlas`;
  document.querySelectorAll('[data-nav]').forEach((node) => node.classList.toggle('active', node.dataset.nav === section));
}

function pageIntro(kicker, title, description, action = '') {
  return `<div class="page-heading"><div><div class="eyebrow">${esc(kicker)}</div><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${action}</div>`;
}

function stat(labelText, number, foot) {
  return `<div class="stat-card"><div class="stat-label">${esc(labelText)}</div><div class="stat-number">${Number(number).toLocaleString('en-US')}</div><div class="stat-foot">${esc(foot)}</div></div>`;
}

function topicCard(topic) {
  return `<a class="topic-card" href="${href('topic', topic.id)}"><div class="card-top"><span class="topic-symbol">${topicIcon[topic.id] || '◌'}</span><span class="card-arrow">↗</span></div><h3>${esc(topic.title)}</h3><p>${esc(topic.summary)}</p><div class="card-meta"><span class="meta-chip"><strong>${topic.claims}</strong> claims</span><span class="meta-chip"><strong>${topic.evidence}</strong> evidence items</span><span class="meta-chip">${topic.gaps} gaps</span></div></a>`;
}

function sourceCard(source) {
  const sample = source.pdf_pages_selected ? `<span class="meta-chip">Sampled ${source.pdf_pages_selected.length} pages</span>` : '';
  return `<a class="source-card" href="${href('source',source.id)}"><span class="source-kind ${source.kind === 'PDF' ? 'pdf' : ''}">${esc(source.kind)}</span><h3>${esc(source.title)}</h3><p>${esc(source.origin_group)}</p><div class="card-meta"><span class="meta-chip">${source.evidence} evidence items</span><span class="meta-chip">${source.visual_pending} items pending review</span>${sample}</div></a>`;
}

function notice(text, link = '') {
  return `<div class="notice"><span class="notice-icon">△</span><div>${text}${link}</div></div>`;
}

function renderOverview() {
  const data = state.overview;
  setHeader('overview');
  const maxEvidence = Math.max(1, ...data.topics.map((t) => t.evidence));
  const sampledPages = data.sources.reduce((count, source) => count + (source.pdf_pages_selected?.length || 0), 0);
  const scopeNotice = sampledPages
    ? `<strong>Reading scope</strong> · Only the selected ${sampledPages} PDF pages were processed. Images, formula layout, and the scientific accuracy of all claims still require human review.`
    : '<strong>Reading scope</strong> · Images, formula layout, and the scientific accuracy of all claims still require human review.';
  main.innerHTML = `<section class="hero"><div class="hero-orbit" aria-hidden="true"></div><div class="eyebrow">RELEASED KNOWLEDGE · TEXT MODE</div><h1>Trace every claim<br>to its source</h1><p>Browse compiled computational lithography knowledge and trace claims to evidence, sources, and page numbers. Publication confirms structural checks; scientific claims still need domain review.</p><div class="hero-actions"><a class="primary-action" href="#/topics">Explore the topic map <span>↗</span></a><button class="mini-action" type="button" data-open-search>Quick search ⌕</button></div></section>
  <section class="stats-grid" aria-label="Knowledge base statistics">${stat('Original sources',data.stats.sources,'Counted by source ID')}${stat('Original evidence',data.stats.evidence,'Traceable to source and character offsets')}${stat('Topic claims',data.stats.claims,'Automatically synthesized; domain review required')}${stat('Pending visual review',data.stats.visual_pending,'Figures, layout, and formulas')}</section>
  ${notice(scopeNotice,' <a href="#/coverage">View coverage report →</a>')}
  <div class="section-heading"><div><h2>Knowledge topics</h2><p>Explore claims and original evidence by topic</p></div><a class="section-link" href="#/topics">All topics ↗</a></div>
  <section class="topic-grid">${data.topics.map(topicCard).join('')}</section>
  <section class="home-bottom"><div class="panel"><h3>Evidence by topic</h3><p class="panel-sub">An evidence item can belong to multiple topics</p>${data.topics.map((topic) => `<a class="bar-row" href="${href('topic',topic.id)}"><span>${esc(topic.title)}</span><span class="bar-track"><span class="bar-fill" style="width:${Math.max(3,Math.round(topic.evidence/maxEvidence*100))}%"></span></span><b>${topic.evidence}</b></a>`).join('')}</div><div class="panel"><h3>Source provenance</h3><p class="panel-sub">Chapter count does not imply independent studies</p>${data.origin_groups.map((group) => `<div class="origin-row"><span class="origin-name">${esc(group.name)}</span><span class="origin-count">${group.count} sources</span></div>`).join('')}<a class="section-link" href="#/sources">Browse source catalog →</a></div></section>`;
}

function renderTopics() {
  setHeader('topics');
  main.innerHTML = `${pageIntro('KNOWLEDGE MAP','Topic map','Each topic brings together compiled claims, conditions, evidence, and open questions.')}
  ${notice('Topic pages provide navigation and synthesis. Open each <strong>original evidence</strong> item. Shared-origin material is not counted as independent experiments.')}
  <div class="section-heading"><div><h2>All topics</h2><p>${state.overview.topics.length} topics · ${state.overview.stats.claims} claims</p></div></div><div class="topic-grid">${state.overview.topics.map(topicCard).join('')}</div>`;
}

function claimCard(claim, index) {
  const kindClass = claim.kind === 'conflict' ? 'red' : claim.kind === 'inference' ? 'gold' : 'mint';
  const conditions = Object.entries(claim.conditions || {}).map(([key,value]) => `<span class="meta-chip">${esc(key)}: ${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</span>`).join('');
  const review = claim.review;
  const flags = [...new Set([...(claim.auto_flags || []), ...(review?.flags || [])])];
  const reviewedAndClear = review?.decision === 'approved' && flags.length === 0;
  const reviewLabel = review?.decision === 'approved' && flags.length ? 'Approval has flags' : review ? decisionNames[review.decision] : 'Pending human review';
  return `<article class="claim-card"><div class="claim-heading"><span class="claim-number">CLAIM ${String(index+1).padStart(2,'0')} · ${esc(shortId(claim.id))}</span><span class="tag ${kindClass}">${esc(label[claim.kind] || claim.kind)}</span></div><h3>${esc(claim.text)}</h3><div class="review-tags"><span class="tag ${reviewedAndClear ? 'mint' : review?.decision === 'rejected' ? 'red' : 'gold'}">${esc(reviewLabel)}</span>${flags.map((flag) => `<span class="issue-tag ${flag}">${esc(flagNames[flag])}</span>`).join('')}</div><div class="claim-explain"><strong>Synthesis rationale</strong> · ${esc(claim.reasoning)}</div>${conditions ? `<div class="conditions">${conditions}</div>` : ''}<div class="evidence-links">${claim.evidence.map((id) => `<a class="evidence-chip" href="${href('evidence',id)}" title="Open original evidence ${esc(id)}">↗ ${esc(shortId(id))}</a>`).join('')}</div><div class="card-meta"><span class="meta-chip">Source relationship: ${esc(label[claim.independence] || claim.independence)}</span><span class="meta-chip">${claim.support_sources.length} source IDs</span></div></article>`;
}

async function renderTopic(id, token) {
  const data = await getJSON(`/api/topic/${encodeURIComponent(id)}`);
  if (token !== state.navToken) return;
  setHeader('topics', data.title);
  const agreements = data.agreements.length ? data.agreements.map((i) => `#${i+1}`).join(', ') : 'No reportable items';
  const disagreements = data.disagreements.length ? data.disagreements.map((i) => `#${i+1}`).join(', ') : 'No reportable items';
  main.innerHTML = `<div class="detail-hero"><div class="eyebrow">TOPIC · ${esc(data.group)}</div><h1>${esc(data.title)}</h1><p>${esc(data.summary)}</p><div class="detail-meta"><span class="tag mint">${data.claims.length} claims</span><span class="tag">${data.evidence_count}  related evidence items</span><span class="tag gold">${data.gaps.length}  knowledge gaps</span></div></div>
  ${notice('Claims are extracted and synthesized from source text. Source relationships are metadata and do not establish scientific truth; open evidence IDs to verify the original text.')}
  <div class="detail-layout"><div><div class="section-heading"><div><h2>Compiled claims</h2><p>Trace each item to its source document</p></div></div><div class="claims-list">${data.claims.length ? data.claims.map(claimCard).join('') : '<div class="empty-state"><h3>No claims yet</h3><p>No publishable text evidence is available.</p></div>'}</div></div><aside class="side-panel"><h3>Synthesis status</h3><div class="side-fact"><span>Shared support</span><strong>${esc(agreements)}</strong></div><div class="side-fact"><span>Unresolved conflict</span><strong>${esc(disagreements)}</strong></div><div class="divider"></div><h3>Knowledge gaps</h3><ul class="gap-list">${data.gaps.map((gap) => `<li>${esc(gap)}</li>`).join('')}</ul></aside></div>`;
}

let sourceFilter = 'all';
function renderSources() {
  setHeader('sources');
  const data = state.overview;
  const visible = data.sources.filter((source) => sourceFilter === 'all' || source.kind === sourceFilter);
  main.innerHTML = `${pageIntro('PROVENANCE','Source catalog','Inspect source groups, evidence counts, sampling scope, and visual review gaps.')}
  ${notice('Source groups distinguish shared-origin material. Each PDF record lists its sampled pages.')}
  <div class="section-heading"><div><h2>Collected materials</h2><p>${data.sources.length} files · ${data.origin_groups.length} declared source groups</p></div></div><div class="filter-row"><button class="filter-btn ${sourceFilter === 'all' ? 'active' : ''}" type="button" data-filter="all">All ${data.sources.length}</button><button class="filter-btn ${sourceFilter === 'Markdown' ? 'active' : ''}" type="button" data-filter="Markdown">Markdown ${data.sources.filter((s) => s.kind === 'Markdown').length}</button><button class="filter-btn ${sourceFilter === 'PDF' ? 'active' : ''}" type="button" data-filter="PDF">PDF ${data.sources.filter((s) => s.kind === 'PDF').length}</button></div><div class="source-grid">${visible.map(sourceCard).join('')}</div>`;
}

async function renderSource(id, token) {
  const data = await getJSON(`/api/source/${encodeURIComponent(id)}`,`source:${id}`);
  if (token !== state.navToken) return;
  setHeader('sources', data.title);
  const pdf = data.path.toLowerCase().endsWith('.pdf');
  const pages = data.pdf_pages_selected || [];
  main.innerHTML = `<div class="detail-hero"><div class="eyebrow">SOURCE · ${pdf ? 'PDF' : 'MARKDOWN'}</div><h1>${esc(data.title)}</h1><p>${esc(data.path)}</p><div class="detail-meta"><span class="tag mint">${data.evidence.length} evidence items</span><span class="tag gold">${data.visual_pending.length} items pending visual review</span><span class="tag">${esc(data.status)}</span></div></div>
  ${pdf && pages.length ? notice(`Only the following source PDF pages were extracted: <strong>${esc(pages.join(', '))}</strong>. Other pages were not used for synthesis or answers.`) : ''}
  <div class="detail-layout"><div><div class="panel"><h3>Source details</h3><div class="side-fact"><span>Source ID</span><strong>${esc(data.id)}</strong></div><div class="side-fact"><span>Declared source group</span><strong>${esc(data.origin_group || 'Unknown')}</strong></div><div class="side-fact"><span>Text spans processed</span><strong>${data.coverage_count}</strong></div><p class="hash-line">SHA256 · ${esc(data.sha256)}</p><a class="mini-action" href="${esc(data.original_url)}" target="_blank" rel="noopener">Open original file ↗</a>${pages.length ? `<div class="page-buttons">${pages.map((page) => `<span class="page-button">PDF ${page}</span>`).join('')}</div>` : ''}</div><div class="section-heading"><div><h2>Source evidence</h2><p>Open verbatim excerpts and locations</p></div></div><div class="data-list">${data.evidence.length ? data.evidence.map((item) => `<a class="data-row" href="${href('evidence',item.id)}"><span><strong>${esc(item.statement)}</strong><small>${esc(shortId(item.id))} · ${item.page ? `PDF page ${item.page}` : esc(item.section)}</small></span><span class="row-arrow">↗</span></a>`).join('') : '<div class="empty-state"><h3>No evidence yet</h3><p>This material may have been withdrawn or contain no extractable text.</p></div>'}</div></div><aside class="side-panel"><h3>Review reminder</h3><p class="panel-sub">${pdf ? 'Verify PDF images, table structure, and formula layout against the original.' : 'Verify imported HTML figures against the original.'}</p><div class="side-fact"><span>Items pending review</span><strong>${data.visual_pending.length}</strong></div><div class="divider"></div><a class="section-link" href="#/coverage">View coverage →</a></aside></div>`;
}

async function renderEvidence(id, token) {
  const data = await getJSON(`/api/evidence/${encodeURIComponent(id)}`,`evidence:${id}`);
  if (token !== state.navToken) return;
  setHeader('topics', 'Original evidence');
  const conditions = Object.entries(data.conditions || {}).map(([key,value]) => `<span class="meta-chip">${esc(key)}: ${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</span>`).join('');
  main.innerHTML = `<div class="detail-hero"><div class="eyebrow">ORIGINAL EVIDENCE</div><h1>Original evidence</h1><p>A verbatim span located in a source snapshot from the published release. It is a source statement, not a verified fact.</p><div class="detail-meta"><span class="tag mint">${esc(data.id)}</span><span class="tag">${data.page ? `PDF page ${data.page}` : `Line ${data.line}`}</span></div></div><div class="detail-layout"><div><div class="panel"><h3>Original excerpt</h3><p class="panel-sub">Character offsets ${data.start}–${data.end} · ${esc(data.section)}</p><blockquote class="quote-block">${esc(data.quote)}</blockquote></div><div class="section-heading"><div><h2>Extracted statement</h2><p>Compare the model summary against the excerpt above</p></div></div><div class="panel"><p style="margin:0;color:var(--text);line-height:1.9">${esc(data.statement)}</p>${conditions ? `<div class="conditions">${conditions}</div>` : ''}</div></div><aside class="side-panel"><h3>Source and location</h3><a class="section-link" href="${href('source',data.source.id)}">${esc(data.source.title)} ↗</a><div class="divider"></div><div class="side-fact"><span>Source ID</span><strong>${esc(data.source.id)}</strong></div><div class="side-fact"><span>Page</span><strong>${data.page ?? '—'}</strong></div><div class="side-fact"><span>Line</span><strong>${data.line}</strong></div><div class="side-fact"><span>Source relationship</span><strong>${esc(data.source.origin_group || 'Unknown')}</strong></div>${data.source.pdf_pages_selected ? `<div class="divider"></div><p class="panel-sub">Only ${data.source.pdf_pages_selected.length} PDF pages were sampled.</p>` : ''}</aside></div>`;
}

function renderCoverage() {
  const data = state.overview;
  setHeader('coverage');
  const pdfs = data.sources.filter((source) => source.pdf_pages_selected);
  const sampledPages = pdfs.reduce((count, source) => count + source.pdf_pages_selected.length, 0);
  main.innerHTML = `${pageIntro('SCOPE & GAPS','Coverage and gaps','Show processed text scope, visual review gaps, and source boundaries together.')}
  ${notice('<strong>Text mode</strong> · Figures, table layout, formula fidelity, and scientific claims have not passed visual or domain validation.')}
  <div class="stats-grid">${stat('Published sources',data.stats.sources,'Complete file snapshots')}${stat('Original evidence',data.stats.evidence,'Exact excerpts and locations')}${stat('Pending visual review',data.stats.visual_pending,'Tracked per source')}${stat('Sampled PDF pages',sampledPages,'Other pages not analyzed')}</div>
  ${pdfs.map((pdf) => `<div class="panel"><h3>${esc(pdf.title)} · Sampling scope</h3><p class="panel-sub">Original PDF page numbers, starting at 1</p><div class="page-buttons">${pdf.pdf_pages_selected.map((page) => `<span class="page-button">${page}</span>`).join('')}</div><p style="color:var(--muted);font-size:12px;margin:16px 0 0">Only the pages above were processed. Unlisted PDF pages were excluded from text extraction, synthesis, and answers.</p></div>`).join('')}
  <div class="section-heading"><div><h2>Coverage by source</h2><p>Open a source to view evidence and the original file</p></div></div><div class="coverage-list">${data.sources.map((source) => `<div class="coverage-row"><div><h3>${esc(source.title)}</h3><p>${esc(source.kind)} · ${source.evidence} evidence items${source.pdf_pages_selected ? ` · Sampled ${source.pdf_pages_selected.length} pages` : ''}</p></div><span class="count">${source.visual_pending} pending items</span><a class="mini-action" href="${href('source',source.id)}">View ↗</a></div>`).join('')}</div>`;
}

function routeParts() {
  const raw = location.hash.replace(/^#\/?/, '') || 'overview';
  return raw.split('/').map((part) => decodeURIComponent(part));
}

async function renderRoute() {
  const token = ++state.navToken;
  try {
    if (!state.me) state.me = await getJSON('/api/me');
    if (state.me.enabled && !state.me.user || !state.me.enabled) {renderAuth();return;}
    const [section, id] = routeParts();
    if (section === 'manage' && state.me.user.role === 'viewer') {location.hash = '#/overview';return;}
    if (state.me.quarantined_sources?.length || !state.me.has_release) {
      $('#connection-label').textContent = state.me.quarantined_sources?.length ? 'Knowledge access paused' : 'Awaiting first release';
      $('#side-release').textContent = state.me.quarantined_sources?.length ? 'Release paused' : 'No release';
      state.overview = null;
      state.cache.clear();
      $('#search-trigger').hidden = true;
      $('#release-pill').hidden = true;
      $('#user-button').hidden = false;
      $('#user-button').textContent = `${state.me.user.username} · Sign out`;
      $('[data-nav="manage"]').hidden = state.me.user.role === 'viewer';
      closeSearch();
      if (section === 'manage' && state.me.user.role !== 'viewer') await renderManage(token);
      else {
        setHeader('overview', state.me.has_release ? 'Knowledge access paused' : 'Awaiting first release');
        main.innerHTML = state.me.has_release
          ? `<div class="empty-state"><h3>Knowledge access is paused</h3><p>An administrator is correcting a source. Quarantined sources: ${esc(state.me.quarantined_sources.join(', '))}</p>${state.me.user.role !== 'viewer' ? '<a class="mini-action" href="#/manage">Open System management</a>' : ''}</div>`
          : `<div class="empty-state"><h3>No published release</h3><p>The first build requires candidate claim review before publication.</p>${state.me.user.role !== 'viewer' ? '<a class="mini-action" href="#/manage">Open System management</a>' : ''}</div>`;
      }
      return;
    }
    $('[data-nav="manage"]').hidden = state.me.user.role === 'viewer';
    $('#connection-label').textContent = 'Connected to a local published release';
    $('#user-button').hidden = false;
    $('#user-button').textContent = `${state.me.user.username} · Sign out`;
    $('#search-trigger').hidden = false;
    $('#release-pill').hidden = false;
    if (!state.overview) state.overview = await getJSON('/api/overview');
    if (token !== state.navToken) return;
    $('#release-pill').innerHTML = `<span class="status-pulse"></span> ${esc(state.overview.release.slice(0,8))}`;
    $('#side-release').textContent = `RELEASE ${state.overview.release.slice(0,12)}`;
    if (section === 'overview') renderOverview();
    else if (section === 'topics') renderTopics();
    else if (section === 'topic' && id) await renderTopic(id, token);
    else if (section === 'sources') renderSources();
    else if (section === 'source' && id) await renderSource(id, token);
    else if (section === 'evidence' && id) await renderEvidence(id, token);
    else if (section === 'coverage') renderCoverage();
    else if (section === 'semantics') await renderOntologyPublic(token);
    else if (section === 'manage') await renderManage(token);
    else {location.hash = '#/overview';return;}
    state.connectionLost = false;
    if (token === state.navToken) window.scrollTo({top:0,behavior:'instant'});
  } catch (error) {
    if (token !== state.navToken) return;
    main.innerHTML = `<div class="empty-state"><h3>The published release is temporarily unavailable</h3><p>${esc(error.message)}</p><button class="mini-action" type="button" id="retry-button" style="margin-top:18px">Retry</button></div>`;
  }
}

function openSearch() {
  if (!state.me?.user || state.connectionLost) return;
  $('#search-modal').hidden = false;
  $('#search-input').focus();
  document.body.style.overflow = 'hidden';
}
function closeSearch() {
  $('#search-modal').hidden = true;
  document.body.style.overflow = '';
}
async function doSearch() {
  const query = $('#search-input').value.trim();
  if (!query) {$('#search-results').innerHTML = '<div class="search-empty">Enter keywords to find published content</div>';return;}
  try {
    const data = await getJSON(`/api/search?q=${encodeURIComponent(query)}`);
    if ($('#search-input').value.trim() !== query) return;
    const results = [];
    if (data.topics.length) {
      results.push('<div class="search-section-label">Topics</div>');
      data.topics.forEach((item) => results.push(`<a class="search-result" href="${href('topic',item.topic)}"><span><strong>${esc(item.title)}</strong><small>${esc(item.summary)}</small></span><span class="row-arrow">↗</span></a>`));
    }
    if (data.evidence.length) {
      results.push('<div class="search-section-label">Original evidence</div>');
      data.evidence.forEach((item) => results.push(`<a class="search-result" href="${href('evidence',item.id)}"><span><strong>${esc(item.statement)}</strong><small>${esc(item.source_title)}${item.page ? ` · PDF ${item.page}` : ''}</small></span><span class="row-arrow">↗</span></a>`));
    }
    if (data.sources.length) {
      results.push('<div class="search-section-label">Sources</div>');
      data.sources.forEach((item) => results.push(`<a class="search-result" href="${href('source',item.id)}"><span><strong>${esc(item.title)}</strong></span><span class="row-arrow">↗</span></a>`));
    }
    $('#search-results').innerHTML = results.length ? results.join('') : '<div class="search-empty">No matches. Try another term.</div>';
    state.searchIndex = 0;
  } catch {$('#search-results').innerHTML = '<div class="search-empty">Search is temporarily unavailable</div>';}
}
function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {toast.hidden = true;}, 4000);
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-open-search],#search-trigger')) openSearch();
  if (event.target.closest('[data-close-search],#search-close')) closeSearch();
  const filter = event.target.closest('[data-filter]');
  if (filter) {sourceFilter = filter.dataset.filter; renderSources();}
  if (event.target.closest('#retry-button')) {state.overview = null;state.me = null;renderRoute();}
  if (event.target.closest('#search-results a')) closeSearch();
  if (event.target.closest('#user-button')) apiPost('/api/logout',{}).then(() => {state.me=null;state.overview=null;state.cache.clear();location.hash='#/overview';renderRoute();}).catch((error) => showToast(error.message));
});
document.addEventListener('submit', async (event) => {
  if (event.target.id !== 'auth-form') return;
  event.preventDefault();
  const form = event.target;
  const values = new FormData(form);
  try {
    await apiPost(state.me.enabled?'/api/login':'/api/setup',{username:values.get('username'),password:values.get('password')});
    state.me = await getJSON('/api/me');
    renderRoute();
  } catch(error) {form.querySelector('.form-message').textContent=error.message;}
});
$('#search-input').addEventListener('input', () => {clearTimeout(state.searchTimer);state.searchTimer = setTimeout(doSearch, 120);});
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {event.preventDefault();openSearch();}
  if (event.key === 'Escape') closeSearch();
  if (!$('#search-modal').hidden && ['ArrowDown','ArrowUp','Enter'].includes(event.key)) {
    const items = [...$('#search-results').querySelectorAll('a.search-result')];
    if (!items.length) return;
    event.preventDefault();
    if (event.key === 'ArrowDown') state.searchIndex = (state.searchIndex + 1) % items.length;
    if (event.key === 'ArrowUp') state.searchIndex = (state.searchIndex - 1 + items.length) % items.length;
   items.forEach((item,index) => item.classList.toggle('selected',index === state.searchIndex));
   items[state.searchIndex].scrollIntoView({block:'nearest'});
    if (event.key === 'Enter') {items[state.searchIndex].click();closeSearch();}
  }
});
window.addEventListener('hashchange', renderRoute);
setInterval(async () => {
  if (!state.me?.user || state.pollInFlight) return;
  state.pollInFlight = true;
  try {
    const latestMe = await getJSON('/api/me');
    const accountChanged = latestMe.user?.username !== state.me.user?.username ||
      latestMe.user?.role !== state.me.user?.role || latestMe.enabled !== state.me.enabled;
    if (state.connectionLost || accountChanged ||
        JSON.stringify(latestMe.quarantined_sources) !== JSON.stringify(state.me.quarantined_sources) ||
        latestMe.has_release !== state.me.has_release || latestMe.has_candidate !== state.me.has_candidate) {
      state.connectionLost = false;state.me = latestMe;state.cache.clear();state.overview=null;
      await renderRoute();return;
    }
    if (!state.overview) return;
    const status = await getJSON('/api/status');
    if (status.release !== state.overview.release) {
      state.cache.clear();state.overview = null;
      await renderRoute();showToast('A new knowledge release was found. The page has been updated.');
    }
  } catch {
    state.connectionLost = true;state.cache.clear();state.overview = null;
    $('#search-trigger').hidden = true;$('#release-pill').hidden = true;
    $('#connection-label').textContent = 'Connection unavailable';
    $('#side-release').textContent = 'Release unavailable';
    closeSearch();setHeader('overview', 'Connection unavailable');
    main.innerHTML = '<div class="empty-state"><h3>Connection unavailable</h3><p>Published knowledge is hidden until the service responds again. This page will retry automatically.</p></div>';
  } finally {state.pollInFlight = false;}
}, 10000);
renderRoute();
