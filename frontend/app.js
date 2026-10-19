/* EvidenceFlow UI: live API contracts with an explicitly labeled local demo mode. */
const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  'plus-square':'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M12 8v8M8 12h8"/>',
  'file-search':'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h7"/><path d="M14 2v6h6"/><circle cx="17" cy="17" r="3"/><path d="m19.2 19.2 2.3 2.3"/>',
  shield:'<path d="m12 22 1.8-.8C19 18.9 21 15 21 10V5l-9-3-9 3v5c0 5 2 8.9 7.2 11.2L12 22Z"/><path d="m9 12 2 2 4-4"/>',
  layers:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m7 14 5 3 5-3M7 10l5 3 5-3m-5-5-5 3 5 3 5-3-5-3Z"/>',
  chevrons:'<path d="m7 10 5-5 5 5M7 14l5 5 5-5"/>',
  panel:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 3v18"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',
  'arrow-left':'<path d="M19 12H5m6 6-6-6 6-6"/>',
  refresh:'<path d="M20 11a8 8 0 0 0-14-5L4 8m0-5v5h5M4 13a8 8 0 0 0 14 5l2-2m0 5v-5h-5"/>',
  download:'<path d="M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  files:'<path d="M8 3h9l4 4v13H8zM8 7H4v14h13"/><path d="M17 3v4h4"/>',
  'check-circle':'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  alert:'<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v4m0 4h.01"/>',
  search:'<circle cx="10.5" cy="10.5" r="7"/><path d="m16 16 5 5"/>',
  upload:'<path d="M12 16V3m-4 4 4-4 4 4M4 17v4h16v-4"/>',
  eye:'<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="3"/>',
  link:'<path d="M10 13a5 5 0 0 0 7 .5l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7-.5l-3 3a5 5 0 0 0 7 7l2-2"/>',
  document:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
  settings:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3m-3-7-2 2M7 17l-2 2m14 0-2-2M7 7 5 5"/>',
  close:'<path d="M18 6 6 18M6 6l12 12"/>'
};
function icon(name,cls='') {return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${icons[name]||icons.document}</svg>`}
function hydrateIcons(scope=document){scope.querySelectorAll('[data-icon]').forEach(x=>{x.innerHTML=icon(x.dataset.icon)})}
function esc(t){return String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const state={nav:'dashboard',jobs:[],jobId:'',results:[],totalResults:0,selectedDoc:'',status:'ALL',search:'',offset:0,limit:25,mode:'paste',draft:'',jobLabel:'',uploadedIds:[],csvFile:null,backendMode:'demo',apiToken:'',proofKey:'international_birth_date',loading:false,activeExport:null};
const app=document.getElementById('app');
const API='/api/v1';
const BULK_PART_BYTES=8*1024*1024;
async function api(path,options={}){const h={...(options.headers||{})};if(state.apiToken)h.Authorization='Bearer '+state.apiToken;const res=await fetch(API+path,{...options,headers:h});if(!res.ok){let e;try{e=await res.json()}catch{};throw Error(typeof e?.detail==='string'?e.detail:`API error ${res.status}`)}return res.json()}
function toast(msg,error=false){let t=document.getElementById('toast');t.textContent=msg;t.className=`toast visible ${error?'error-toast':''}`;clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.classList.remove('visible'),3800)}
function initials(s){return (s||'Job').slice(0,2).toUpperCase()}
function n(v){return Number(v||0).toLocaleString('en-US')}
function time(s){if(!s)return '—';return new Date(s).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function badge(st){let map={COMPLETED:['Completed','success'],COMPLETED_WITH_EXCEPTIONS:['With exceptions','review'],PROCESSING:['Processing','processing'],QUEUED:['Queued','muted'],REVIEW_REQUIRED:['Needs review','review'],NOT_FOUND:['Not found','failed'],FAILED:['Failed','failed']};let [label,klass]=map[st]||[st||'Unknown','muted'];return `<span class="badge ${klass}"><span class="dot"></span>${esc(label)}</span>`}
function banner(){if(state.backendMode==='live')return `<div class="demo-banner live-banner">${icon('shield')}<div><strong>Connected pipeline.</strong> SQS background jobs, Snowflake, S3, and JnJ Azure endpoints are supported. Corporate authentication, fine-grained entitlements, deployment controls and load tests must be verified before regulated production use.</div></div>`;return `<div class="demo-banner">${icon('alert')}<div><strong>Demo mode — source screenshots only.</strong> Example evidence is illustrative, not verified from an original PDF. Jobs, results and retries are persisted locally; Snowflake, S3, OCR and JnJ Azure AI are not contacted.</div></div>`}
function pageHead(kicker,title,subtitle,buttons=''){return `<div class="page-head"><div><div class="eyebrow">${esc(kicker)}</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="head-actions">${buttons}</div></div>`}
function renderNav(){document.querySelectorAll('[data-nav]').forEach(x=>x.classList.toggle('active',x.dataset.nav===state.nav));document.getElementById('page-breadcrumb').textContent=({dashboard:'Overview',submit:'New extraction',results:'Job results',review:'Review queue'})[state.nav];let c=state.jobs.reduce((a,j)=>a+j.counts.review,0);document.getElementById('nav-review-count').textContent=n(c)}
function totals(){return state.jobs.reduce((o,j)=>{o.jobs++;for(let k of ['total','completed','review','failed','queued','processing'])o[k]+=j.counts[k];return o},{jobs:0,total:0,completed:0,review:0,failed:0,queued:0,processing:0})}
function progress(j){return `<div class="job-row-progress"><div class="progress"><div class="progress-bar" style="width:${Math.min(100,j.progress_percent||0)}%"></div></div><span>${(j.progress_percent||0).toFixed(0)}%</span></div>`}
function jobsTable(list){if(!list.length)return empty('No jobs yet','Submit IDs to create a traceable extraction job.','files');return `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Created</th><th>Documents</th><th>Progress</th><th>Status</th><th></th></tr></thead><tbody>${list.map(j=>`<tr><td><button class="row-link" data-action="pick-job" data-id="${esc(j.job_id)}">${esc(j.label)}</button><small class="job-id">${esc(j.job_id)}</small></td><td>${esc(time(j.created_at))}</td><td><strong>${n(j.counts.total)}</strong></td><td>${progress(j)}</td><td>${badge(j.status)}</td><td><button class="arrow-button" title="View job" data-action="pick-job" data-id="${esc(j.job_id)}">${icon('arrow','sm')}</button></td></tr>`).join('')}</tbody></table></div>`}
function empty(title,desc,name){return `<div class="empty-state"><div class="empty-icon">${icon(name)}</div><h3>${esc(title)}</h3><p>${esc(desc)}</p></div>`}
function renderDashboard(){let t=totals();return `${pageHead('DOCUMENT OPERATIONS','EvidenceFlow','Proof-backed document extraction: from a single ID to a managed batch.',`<button class="btn" data-action="refresh">${icon('refresh')} Refresh</button><button class="btn primary" data-nav="submit">${icon('plus')} New extraction</button>`)}${banner()}
<div class="stats-grid">
${stat('Total documents',n(t.total),'Across all jobs','files','')}
${stat('Completed',n(t.completed),'Structured results','check-circle','green')}
${stat('Needs review',n(t.review),'Evidence needs validation','shield','amber')}
${stat('Unavailable / failed',n(t.failed),'Missing or failed','alert','red')}
</div>
<div class="content-grid"><section class="panel"><div class="panel-header"><div><h2 class="panel-title">Recent extraction jobs</h2><p class="panel-desc">Job-level status and incremental progress</p></div><button class="text-action" data-nav="results">View results ${icon('arrow','sm')}</button></div>${jobsTable(state.jobs.slice(0,8))}<div class="panel-footer"><span>${n(t.jobs)} job${t.jobs===1?'':'s'} in demo workspace</span><span>Refreshes automatically</span></div></section>
<section class="panel pipeline-panel"><h2 class="panel-title">How the pipeline works</h2><div class="step-list">
${step('Snowflake metadata resolution','IDs matched to source documents and S3 URIs','layers')}
${step('Parallel PDF processing','Bounded workers fetch PDFs and extract page text','files')}
${step('Evidence-backed extraction','Dates, products, and summaries with page references','file-search')}
${step('Validation and delivery','Review unclear fields, then export structured results','shield',true)}
</div><div class="panel-small-card">${icon('shield')}<div><strong>Accuracy before automation</strong><p>Unverifiable values must remain unresolved. Each accepted critical field must have verifiable source evidence.</p></div></div></section></div>`}
function stat(label,value,meta,ic,style){return `<div class="stat-card"><div class="stat-top">${label}<span class="stat-icon ${style}">${icon(ic)}</span></div><div class="stat-value">${value}</div><div class="stat-meta">${meta}</div></div>`}
function step(title,desc,ic,last=false){return `<div class="step"><div class="step-left"><div class="step-round ${last?'green':''}">${icon(ic)}</div>${!last?'<div class="step-line"></div>':''}</div><div><strong>${title}</strong><p>${desc}</p></div></div>`}
function draftIds(){if(state.mode==='csv')return state.uploadedIds;return state.draft.split(/[\s,;]+/).map(s=>s.trim().replace(/^['"]|['"]$/g,'')).filter(Boolean).filter(x=>!/^r_object_id$/i.test(x))}
function submitPreview(){let ids=draftIds(),unique=new Set(ids),invalid=[...unique].filter(x=>!(/^[A-Za-z0-9_-]{1,128}$/.test(x)));return `<span class="preview-count">${n(unique.size)}</span> unique IDs · ${n(ids.length-unique.size)} duplicate(s) · ${n(invalid.length)} invalid`}
function renderSubmit(){return `${pageHead('CREATE NEW JOB','New extraction job','Paste IDs or upload a CSV. One job can grow from a single ID to a large batch.')}${banner()}<div class="job-create-grid"><section class="panel form-card"><div class="form-section"><label class="form-label" for="job-label">Job name</label><input id="job-label" class="input" maxlength="100" style="width:100%" value="${esc(state.jobLabel)}" placeholder="e.g. October PBRER batch"></div><div class="form-section"><div class="form-label">Choose submission method</div><div class="segment"><button data-action="mode" data-mode="paste" class="${state.mode==='paste'?'active':''}">${icon('document','sm')} Paste IDs</button><button data-action="mode" data-mode="csv" class="${state.mode==='csv'?'active':''}">${icon('upload','sm')} Upload CSV</button></div>
${state.mode==='paste'?`<label class="form-label" for="ids-input">R_OBJECT_ID values</label><textarea class="textarea" id="ids-input" spellcheck="false" placeholder="0901bacc84b20d46&#10;0901bacc8283f1de&#10;0901bacc802bcfa8">${esc(state.draft)}</textarea><p class="form-hint">Separate IDs with newlines, commas, or spaces. Duplicates are removed before submission.</p><button class="btn secondary vertical-gap" data-action="sample">${icon('plus')} Fill sample IDs</button>`:
`<label class="file-drop" for="csv-upload">${icon('upload')}<strong>Choose a CSV or TXT file</strong><p>First column or a column named R_OBJECT_ID</p><input id="csv-upload" type="file" accept=".csv,.txt,text/csv,text/plain"></label><p class="form-hint" id="csv-name">${state.uploadedIds.length?`${n(state.uploadedIds.length)} IDs previewed`:'No file selected'}</p><p class="form-hint">CSV is uploaded as a file and streamed into the job store in bounded chunks. Up to 2 million rows and 100 MB are supported per upload in this starter.</p>`}
</div><div class="submit-footer"><div class="submit-preview" id="submit-count">${state.mode==='csv'&&state.csvFile ? esc(state.csvFile.name+' · '+(state.csvFile.size/1024/1024).toFixed(2)+' MB') : submitPreview()}</div><button class="btn primary" data-action="submit" ${state.loading?'disabled':''}>${icon('arrow')} ${state.loading?'Creating...':'Create extraction job'}</button></div></section>
<section><section class="panel form-card"><h2 class="panel-title">What happens next?</h2><div class="info-list"><div class="info-item"><span class="info-number">1</span><div><strong>Create a durable job</strong><p>Each submitted ID becomes a tracked document in the job.</p></div></div><div class="info-item"><span class="info-number">2</span><div><strong>Resolve and process</strong><p>Workers resolve Snowflake metadata in batches; SQS distributes checkpointed processing stages.</p></div></div><div class="info-item"><span class="info-number">3</span><div><strong>Inspect evidence</strong><p>Extracted fields include page-level context and review state.</p></div></div><div class="info-item"><span class="info-number">4</span><div><strong>Export results</strong><p>Download a CSV or JSON; bulk exports will be generated asynchronously.</p></div></div></div></section><section class="panel quality-panel"><div class="subtle-label">Production API</div><code class="code-mini">POST /api/v1/jobs<br>GET /api/v1/jobs/{job_id}<br>GET /api/v1/jobs/{job_id}/results</code><p>API and workers share one codebase. SQLite is used only for the local demo; live parallel workers use PostgreSQL.</p></section></section></div>`}
function selectedJob(){return state.jobs.find(j=>j.job_id===state.jobId)||state.jobs[0]}
function renderResults(){let j=selectedJob();if(!j)return `${pageHead('DOCUMENT RESULTS','Extraction results','Inspect output and source evidence.')}${banner()}${empty('No job selected','Create a job to start exploring results.','files')}`;return `${pageHead('DOCUMENT RESULTS','Extraction results',`Explore extracted documents in ${j.label}.`,`<button class="btn" data-action="cancel">${icon('close')} Cancel job</button><button class="btn" data-action="retry">${icon('refresh')} Retry failures</button><button class="btn" data-action="export" data-format="csv">${icon('download')} CSV</button><button class="btn primary" data-action="export" data-format="json">${icon('download')} JSON</button>`)}${banner()}
<section class="panel"><div class="results-toolbar"><div class="inline-controls"><label class="subtle-label" for="job-picker">Job</label><select class="select" id="job-picker">${state.jobs.map(x=>`<option value="${esc(x.job_id)}" ${x.job_id===j.job_id?'selected':''}>${esc(x.label)} (${n(x.counts.total)})</option>`).join('')}</select></div><div class="inline-controls"><span class="subtle-label">Progress</span><div style="min-width:170px">${progress(j)}</div>${badge(j.status)}</div></div>
<div class="results-toolbar"><div class="filters"><div class="searchbox">${icon('search')}<input class="input search-input" id="result-search" placeholder="Search document ID" value="${esc(state.search)}" aria-label="Search document ID"></div><select id="status-filter" class="select" aria-label="Filter status">${[['ALL','All statuses'],['COMPLETED','Completed'],['REVIEW_REQUIRED','Needs review'],['NOT_FOUND','Not found'],['PROCESSING','Processing'],['QUEUED','Queued'],['FAILED','Failed']].map(([k,v])=>`<option value="${k}" ${state.status===k?'selected':''}>${v}</option>`).join('')}</select></div><button class="btn" data-action="apply-filters">${icon('search')} Apply</button></div>
${resultsTable()}<div class="panel-footer"><span>${n(state.totalResults)} matching documents · ${n(state.offset+1)}–${n(Math.min(state.offset+state.limit,state.totalResults))}</span><div class="pagination"><button data-action="page" data-dir="prev" ${state.offset===0?'disabled':''}>Previous</button><button data-action="page" data-dir="next" ${state.offset+state.limit>=state.totalResults?'disabled':''}>Next</button></div></div></section>
${state.selectedDoc?`<div class="vertical-gap">${documentInspector()}</div>`:''}`}
function resultsTable(){if(!state.results.length)return empty('No matching documents','Try a different filter or submit a new extraction job.','search');return `<div class="table-wrap"><table><thead><tr><th>Document ID</th><th>Type</th><th>Product</th><th>IBD</th><th>Processing state</th><th></th></tr></thead><tbody>${state.results.map(d=>`<tr><td><button class="row-link job-id" data-action="document" data-id="${esc(d.r_object_id)}">${esc(d.r_object_id)}</button></td><td>${d.artifact_name?'PBRER':'—'}</td><td>${esc(d.fields?.product?.display||'—')}</td><td>${esc(d.fields?.international_birth_date?.display||'—')}</td><td>${badge(d.status)}</td><td><button class="arrow-button" title="Inspect evidence" data-action="document" data-id="${esc(d.r_object_id)}">${icon('eye','sm')}</button></td></tr>`).join('')}</tbody></table></div>`}
function documentInspector(){
 let d=state.results.find(x=>x.r_object_id===state.selectedDoc);if(!d)return '';
 let fields=d.fields||{},hasProof=Object.keys(fields).length>0;
 let selected=fields[state.proofKey]||Object.values(fields)[0];
 return `<div class="inspector-layout"><section class="panel"><div class="panel-header"><div><div class="subtle-label">PROOF-CARRYING DOCUMENT</div><h2 class="panel-title" style="margin-top:7px">${esc(d.r_object_id)}</h2></div>${badge(d.status)}</div>
 ${hasProof?`<div class="field-grid">${field('Document date','document_date',fields.document_date)}${field('International Birth Date','international_birth_date',fields.international_birth_date)}${field('Product','product',fields.product)}${field('Active ingredient','active_ingredient',fields.active_ingredient)}${field('Manufacturer','manufacturer',fields.manufacturer)}${field('Document number','document_number',fields.document_number)}</div><div class="panel-header" style="border-top:1px solid var(--line)"><h2 class="panel-title">Grounded summary</h2><span class="badge muted">AI draft · validate against source</span></div><div class="summary-body">${esc(d.summary||'Summary not available — review required.')}</div>`:
 `<div class="review-notice">${icon('alert')} No accepted fields yet. If this is a live job, inspect its status or retry a failed document. Demo mode does not access real PDFs.</div><div style="height:65px"></div>`}
 <div class="inspector-note">${icon('shield','sm')} ${esc(d.note||d.error||'Critical values require source evidence. Document completion does not equal regulatory approval.')}</div></section>
 <section class="panel"><div class="viewer-top"><strong style="font-size:12px">Evidence detail</strong><span class="badge muted">${selected?.page?`Source page ${esc(selected.page)}`:'Not available'}</span></div>
 ${hasProof?`<div class="viewer-canvas"><div class="paper proof-paper"><div class="paper-top">SOURCE PROOF · ${esc(d.r_object_id)}</div><div class="proof-tag">${esc(selected?.method||'EXTRACTED SOURCE')}</div><div class="proof-quote">“${esc(selected?.excerpt||'No supporting excerpt available')}”</div><div class="paper-footer">PAGE ${esc(selected?.page||'—')} · ${esc(selected?.validation||'UNVERIFIED')}</div></div></div><div class="viewer-warning">${icon('shield','sm')} ${esc(state.backendMode==='demo'?'Screenshot-transcribed evidence, not independently PDF-verified.':'Exact extracted source text. Open the original versioned PDF using the authorized source link below.')}</div><button class="btn" data-action="open-pdf" style="margin:12px">${icon('eye','sm')} Open exact source PDF (page ${esc(selected?.page||1)})</button>`:
 `<div class="empty-state">${icon('document')}<h3>No evidence available</h3><p>Unavailable or unresolved documents cannot be automatically accepted.</p></div>`}</section></div>`;
}
function field(label,key,f){if(!f)return `<div class="field-card"><div class="field-title">${esc(label)}</div><div class="field-val muted-text">Not found</div><div class="field-proof">${icon('alert')} Requires source evidence</div></div>`;
 const test=f.validation==='VERIFIED';return `<div class="field-card"><div class="field-title">${esc(label)}</div><div class="field-val">${esc(f.display||f.value)}</div><div class="field-proof" style="color:${test?'#17875f':'#9b6b27'}">${icon(test?'check-circle':'alert')} ${esc(test?'Source verified':f.validation||'Unverified')} · p.${esc(f.page)}</div><button data-action="proof" data-field="${esc(key)}">Inspect source proof ${icon('arrow','sm')}</button></div>`;
}
function renderReview(){let review=state.jobs.reduce((a,j)=>a.concat(j.counts.review? [{...j}]:[]),[]),revs=state.results.filter(d=>d.status==='REVIEW_REQUIRED');return `${pageHead('QUALITY CONTROL','Review queue','Documents needing evidence checks before structured fields can be trusted.',`<button class="btn" data-action="refresh">${icon('refresh')} Refresh queue</button><button class="btn primary" data-nav="results">${icon('file-search')} Browse results</button>`)}${banner()}
<section class="panel"><div class="panel-header"><div><h2 class="panel-title">Unresolved document extractions</h2><p class="panel-desc">No automatic approval without underlying PDF evidence</p></div><span class="badge review">${n(totals().review)} awaiting review</span></div><div class="review-notice">Production implementation will support field-level corrections, reviewer identity, timestamps, and an immutable decision audit trail. The prototype only displays unresolved documents.</div><div style="height:15px"></div>${review.length?jobsTable(review):empty('Nothing to review','No jobs currently contain review-required documents.','shield')}</section>
<section class="panel quality-panel"><h2 class="panel-title">Quality policy</h2><div class="help-grid vertical-gap"><div class="help-card"><strong>Verified evidence</strong><p>Every critical date or product must include page provenance and matching source content.</p></div><div class="help-card"><strong>Ambiguous language</strong><p>“IBD” must be resolved as International Birth Date only in the appropriate document context.</p></div></div></section>`}
function render(){renderNav();app.innerHTML=({dashboard:renderDashboard,submit:renderSubmit,results:renderResults,review:renderReview})[state.nav]();}
async function loadJobs(){state.jobs=await api('/jobs');if(!state.jobId&&state.jobs.length)state.jobId=state.jobs[0].job_id}
async function loadResults(){if(!state.jobId){state.results=[];state.totalResults=0;return}let q=new URLSearchParams({offset:state.offset,limit:state.limit,search:state.search,status:state.status});let r=await api(`/jobs/${encodeURIComponent(state.jobId)}/results?${q}`);state.results=r.items;state.totalResults=r.total;if(state.selectedDoc&&!state.results.some(d=>d.r_object_id===state.selectedDoc))state.selectedDoc='';}
async function go(nav){state.nav=nav;if(nav==='results'||nav==='review')await loadResults();render()}
async function refresh(){await loadJobs();if(state.nav==='results'||state.nav==='review')await loadResults();render()}
function parseCsv(text){let lines=text.split(/\r?\n/).filter(Boolean);if(!lines.length)return[];let first=lines[0].split(',').map(x=>x.trim().replace(/^"|"$/g,''));let ix=first.findIndex(s=>s.toLowerCase()==='r_object_id');let hasHeader=ix!==-1;if(!hasHeader)ix=0;return lines.slice(hasHeader?1:0).map(line=>{let row=line.split(',');return(row[ix]||'').trim().replace(/^"|"$/g,'')}).filter(Boolean)}
async function resumableUpload(file,label){
 const signature=`evidenceflow-upload:${file.name}:${file.size}:${file.lastModified}`;
 let saved;try{saved=JSON.parse(localStorage.getItem(signature)||'null')}catch{}
 let setup=saved;
 if(setup){
   try{await api(`/jobs/${setup.job_id}/uploads/${setup.upload_id}`)}catch{setup=null}
 }
 if(!setup){
   setup=await api(`/uploads/init?label=${encodeURIComponent(label)}`,{method:'POST'});
   localStorage.setItem(signature,JSON.stringify(setup));
 }
 const uploaded=await api(`/jobs/${setup.job_id}/uploads/${setup.upload_id}`);
 const map=new Map((uploaded.parts||[]).map(p=>[p.PartNumber,p.ETag]));
 const total=Math.ceil(file.size/BULK_PART_BYTES);
 if(uploaded.state==='COMPLETING'){
   await api(`/jobs/${setup.job_id}/uploads/${setup.upload_id}/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parts:uploaded.parts})});
   localStorage.removeItem(signature);return setup;
 }
 if(uploaded.state==='READY'||uploaded.state==='INGESTED'){localStorage.removeItem(signature);return setup;}
 for(let part=1;part<=total;part++){
   if(map.has(part))continue;
   const sign=await api(`/jobs/${setup.job_id}/uploads/${setup.upload_id}/parts/${part}`);
   const chunk=file.slice((part-1)*BULK_PART_BYTES,Math.min(part*BULK_PART_BYTES,file.size));
   let response=await fetch(sign.url,{method:'PUT',body:chunk});
   if(!response.ok)throw Error(`Part ${part} failed (HTTP ${response.status}). Resume by reselecting the file.`);
   const etag=response.headers.get('ETag');
   if(!etag)throw Error('S3 CORS must expose ETag response header for resumable multipart upload.');
   map.set(part,etag);
   if(part===1||part%4===0)toast(`Uploaded ${part}/${total} parts; safe to resume if interrupted.`);
 }
 const parts=Array.from(map.entries()).map(([PartNumber,ETag])=>({PartNumber,ETag})).sort((a,b)=>a.PartNumber-b.PartNumber);
 await api(`/jobs/${setup.job_id}/uploads/${setup.upload_id}/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parts})});
 localStorage.removeItem(signature);
 return setup;
}

async function startAsyncExport(format){
 const jobId=state.jobId;
 const r=await api(`/jobs/${encodeURIComponent(jobId)}/exports?format=${format}`,{method:'POST'});
 state.activeExport={jobId,exportId:r.export_id,format};
 toast(`Background ${format.toUpperCase()} export queued. Keep working while it runs.`);
}
async function pollAsyncExport(){
 const e=state.activeExport;
 if(!e)return;
 let r=await api(`/jobs/${encodeURIComponent(e.jobId)}/exports/${e.exportId}`);
 if(r.status==='COMPLETED'&&r.url){state.activeExport=null;toast('Export is ready; downloading.');window.location.assign(r.url)}
 else if(r.status==='FAILED'){state.activeExport=null;toast('Export failed. Try again or contact support.',true)}
}
async function submit(){if(state.loading)return;if(state.mode==='csv'&&state.csvFile){state.loading=true;try{let j;if(state.backendMode==='live'&&state.csvFile.size>8*1024*1024){j=await resumableUpload(state.csvFile,state.jobLabel||'Bulk extraction job');}else{const f=new FormData();f.append('file',state.csvFile);f.append('label',state.jobLabel||'Bulk extraction job');j=await api('/jobs/upload',{method:'POST',body:f});}state.jobId=j.job_id;state.selectedDoc='';state.offset=0;state.search='';state.status='ALL';await refresh();state.nav='results';await loadResults();render();toast(j.upload_id?'Multipart upload complete. Background ID ingestion started.':`File accepted. ${n(j.counts.total)} unique IDs queued; ${n(j.invalid_rows_skipped||0)} invalid rows skipped.`)}catch(e){toast(e.message,true)}finally{state.loading=false}return}let ids=[...new Set(draftIds())];if(!ids.length){toast('Enter at least one document ID.',true);return}if(ids.length>5000){toast('Use CSV upload above 5,000 IDs.',true);return}if(ids.some(x=>!(/^[A-Za-z0-9_-]{1,128}$/.test(x)))){toast('Invalid ID found. Allowed: letters, numbers, hyphens, underscores.',true);return}state.loading=true;try{let j=await api('/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,label:state.jobLabel||'New extraction job'})});state.jobId=j.job_id;state.selectedDoc='';state.offset=0;state.search='';state.status='ALL';await refresh();state.nav='results';await loadResults();render();toast(`Job created with ${n(ids.length)} unique IDs.`)}catch(e){toast(e.message,true)}finally{state.loading=false}}

document.addEventListener('click',async e=>{let nav=e.target.closest('[data-nav]');if(nav){await go(nav.dataset.nav);return}let a=e.target.closest('[data-action]');if(!a)return;let act=a.dataset.action;try{
 if(act==='refresh'){await refresh();toast('Data refreshed.')} 
 if(act==='pick-job'){state.jobId=a.dataset.id;state.status='ALL';state.offset=0;state.selectedDoc='';await go('results')}
 if(act==='document'){state.selectedDoc=a.dataset.id;state.proofKey='international_birth_date';render();document.querySelector('.inspector-layout')?.scrollIntoView({behavior:'smooth',block:'start'})}
 if(act==='proof'){state.proofKey=a.dataset.field;render();document.querySelector('.inspector-layout')?.scrollIntoView({behavior:'smooth',block:'start'})}
 if(act==='mode'){if(state.mode==='paste'){state.draft=document.getElementById('ids-input')?.value||state.draft};state.mode=a.dataset.mode;render()}
 if(act==='sample'){state.draft='0901bacc84b20d46\n0901bacc8283f1de\n0901bacc802bcfa8';render();toast('Reference IDs inserted.')}
 if(act==='submit'){state.jobLabel=document.getElementById('job-label')?.value||'';if(state.mode==='paste')state.draft=document.getElementById('ids-input')?.value||'';await submit()}
 if(act==='apply-filters'){state.search=document.getElementById('result-search')?.value.trim()||'';state.status=document.getElementById('status-filter')?.value||'ALL';state.offset=0;await loadResults();render()}
 if(act==='page'){state.offset=Math.max(0,state.offset+(a.dataset.dir==='next'?state.limit:-state.limit));await loadResults();render()}
 if(act==='cancel'){if(!window.confirm('Cancel pending work for this job? Completed results remain available.'))return;await api(`/jobs/${encodeURIComponent(state.jobId)}/cancel`,{method:'POST'});await refresh();toast('Job cancelled. Already completed results were preserved.')}
 if(act==='open-pdf'){const docId=state.selectedDoc;if(!docId||state.backendMode!=='live'){toast('Original PDF is unavailable in demo mode.',true);return;}let proof=state.results.find(x=>x.r_object_id===docId)?.fields?.[state.proofKey];let url=await api(`/jobs/${encodeURIComponent(state.jobId)}/documents/${encodeURIComponent(docId)}/source-url`);window.open(url.url+'#page='+(proof?.page||1),'_blank','noopener,noreferrer');return;}
 if(act==='retry'){let r=await api(`/jobs/${encodeURIComponent(state.jobId)}/retry`,{method:'POST'});toast(`${n(r.requeued)} failed documents requeued.`);await refresh()}
 if(act==='export'){let selected=selectedJob();if(state.backendMode==='live'&&selected?.counts.total>50000){await startAsyncExport(a.dataset.format);return;}let url=`${API}/jobs/${encodeURIComponent(state.jobId)}/export?format=${a.dataset.format}`;let r=await fetch(url,{headers:state.apiToken?{Authorization:'Bearer '+state.apiToken}:{}});if(!r.ok)throw Error('Export failed');const blob=await r.blob();const tmp=URL.createObjectURL(blob);const link=document.createElement('a');link.href=tmp;link.download=`${state.jobId}.${a.dataset.format}`;link.click();setTimeout(()=>URL.revokeObjectURL(tmp),60000)}
}catch(err){toast(err.message,true)}});
document.addEventListener('change',async e=>{
 if(e.target.id==='job-picker'){state.jobId=e.target.value;state.selectedDoc='';state.offset=0;state.search='';state.status='ALL';await loadResults();render()}
 if(e.target.id==='csv-upload'){const f=e.target.files?.[0];if(!f)return;try{if(f.size>(state.backendMode==='live'?1024*1024*1024:100*1024*1024))throw Error('Maximum supported upload is 100 MB in local demo; use enterprise multipart upload for larger files.');state.csvFile=f;state.uploadedIds=f.size<1024*1024?parseCsv(await f.text()):[];render();toast('Ready to submit '+f.name)}catch(err){toast(err.message,true)}}
});
document.addEventListener('input',e=>{if(e.target.id==='ids-input'){state.draft=e.target.value;let c=document.getElementById('submit-count');if(c)c.innerHTML=submitPreview()}if(e.target.id==='job-label')state.jobLabel=e.target.value});
async function init(){hydrateIcons();try{let h=await api('/health');state.backendMode=h.mode||'demo';if(state.backendMode==='live'){state.apiToken=typeof window.evidenceflowGetAccessToken==='function'?await window.evidenceflowGetAccessToken():''}await loadJobs();await loadResults();render()}catch(e){app.innerHTML=`<div class="empty-state"><h3>Cannot connect to EvidenceFlow API</h3><p>${esc(e.message)}. Start the included FastAPI server; opening the HTML file directly is not sufficient.</p></div>`}setInterval(async()=>{try{await pollAsyncExport();if(state.nav==='submit')return;await refresh()}catch{}},8000)}
init();
