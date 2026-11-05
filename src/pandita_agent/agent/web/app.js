/* Credentials and retry envelopes stay in memory. Untrusted content uses textContent. */
const $ = id => document.getElementById(id);
let session = null, busy = false, timer = null, pendingCommand = null, watcher = null;
const terminal = new Set(['idle','completed','failed','cancelled','interrupted','expired']);
async function api(path, body) {
  const response = await fetch(path, {method:body ? 'POST':'GET',cache:'no-store',headers:{Authorization:'Bearer '+$('token').value,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  const data = await response.json();
  if(!response.ok){const error=new Error(data.detail || 'Request failed.');error.status=response.status;throw error;}
  return data;
}
async function action(work) {
  if(busy)return; busy=true; clearTimeout(timer); $('error').textContent=''; $('processing').textContent='Working…';
  for(const button of document.querySelectorAll('button'))button.disabled=true;
  try {await work();} catch(error) {$('error').textContent=error.message;}
  finally {busy=false;$('processing').textContent='';for(const button of document.querySelectorAll('button'))button.disabled=false;renderControls();poll();}
}
function poll(){
  clearTimeout(timer);
  if(!session||terminal.has(session.state.status)){watcher?.controller.abort();watcher=null;return;}
  if(watcher?.id===session.id)return;
  watcher?.controller.abort();
  const watch={id:session.id,controller:new AbortController()};watcher=watch;
  watchEvents(watch).catch(error=>{if(error.name!=='AbortError')$('error').textContent='Live stream interrupted. Reconnecting…';}).finally(()=>{
    if(watcher===watch){watcher=null;timer=setTimeout(poll,1500);}
  });
}
async function watchEvents(watch){
  const response=await fetch(`/v1/sessions/${watch.id}/stream?after=${session.revision}`,{headers:{Authorization:'Bearer '+$('token').value},cache:'no-store',signal:watch.controller.signal});
  if(!response.ok)throw new Error('Stream access unavailable.');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
  while(true){
    const {value,done}=await reader.read();if(done)return;
    buffer+=decoder.decode(value,{stream:true});
    if(buffer.length>262144)throw new Error('Stream frame too large.');
    let boundary;
    while((boundary=buffer.indexOf('\n\n'))>=0){
      const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
      const payload=block.split('\n').filter(line=>line.startsWith('data: ')).map(line=>line.slice(6)).join('\n');
      if(!payload)continue;
      if(block.includes('event: access.closed')){watcher=null;watch.controller.abort();$('error').textContent='Session access expired or changed. Connect again.';return;}
      const event=JSON.parse(payload);
      if(watcher!==watch||session.id!==watch.id)return;
      if(event.revision<=session.revision)continue;
      if(event.kind==='model.delta'){
        const run=session.state.run;
        if(!run.stream||run.stream.step!==event.data.step)run.stream={text:'',status:'generating',step:event.data.step};
        run.stream.text+=event.data.delta;session.revision=event.revision;show();
      }else{
        await load(watch.id);
        if(terminal.has(session.state.status))return;
      }
    }
  }
}
function renderControls(){
  const status=session?.state.status, allowed=session?.actions||{};
  $('send').disabled=busy||!allowed.turn||!!pendingCommand||!(terminal.has(status)||status==='waiting_input');
  $('cancel').disabled=busy||!allowed.cancel||!session||!!pendingCommand||terminal.has(status)||['outcome_unknown','reconciling'].includes(status);
  $('save-memory').disabled=busy||!allowed.memory||!!pendingCommand||!terminal.has(status);
  $('upload').disabled=busy||!allowed.upload||!!pendingCommand||!terminal.has(status);
  $('approve').disabled=$('reject').disabled=busy||!allowed.approve||!!pendingCommand;
  $('reconcile').disabled=busy||!allowed.reconcile||!!pendingCommand;
  for(const button of document.querySelectorAll('[data-download]'))button.disabled=busy||!allowed.download;
  $('retry').hidden=!pendingCommand;
  $('new').disabled=busy||!!pendingCommand||!$('agent').value;
}
function show(){
  $('conversation').hidden=false; $('session-id').value=session.id;
  const state=session.state, run=state.run;
  $('status').textContent=`${state.status.replaceAll('_',' ')} · ${session.id} · revision ${session.revision}`;
  const messages=state.history.flatMap(turn=>turn.messages);
  if(run&&!run.archived)messages.push(...run.frames[0].messages);
  $('messages').replaceChildren();
  for(const message of messages){
    let content=message.content, label=message.runtime_feedback?'Completion check: ':message.completion==='rejected'?'Agent draft — check failed: ':message.role==='user'?'You: ':'Agent: ';
    if(message.role==='tool'){
      try{const result=JSON.parse(content);if(!Object.hasOwn(result,'answer'))continue;content=result.answer;label='You: ';}catch{continue;}
    }
    if(!content)continue;
    const article=document.createElement('article');article.textContent=label+content;$('messages').append(article);
  }
  if(run?.stream?.text){const draft=document.createElement('article');draft.className='muted';draft.textContent=(terminal.has(state.status)?'Incomplete draft: ':'Generating — provisional: ')+run.stream.text;$('messages').append(draft);}
  const waiting=['waiting_input','waiting_approval','outcome_unknown'].includes(state.status);
  $('pending').hidden=!waiting; $('review').hidden=state.status!=='waiting_approval';
  $('reconcile').hidden=state.status!=='outcome_unknown';
  $('action').textContent=waiting?(state.status==='waiting_input'?run.pending.arguments.question:JSON.stringify(run.pending,null,2)):'';
  if(document.activeElement!==$('memory'))$('memory').value=JSON.stringify(state.memory,null,2);
  $('files').replaceChildren();
  for(const [path,file] of Object.entries(state.files||{})){
    const row=document.createElement('div'),button=document.createElement('button');
    row.append(document.createTextNode(`${path} · ${file.bytes} bytes · version ${file.version} `));
    button.textContent='Download';button.dataset.download='true';button.disabled=!session.actions?.download;button.onclick=()=>action(()=>download(path));row.append(button);$('files').append(row);
  }
  renderControls();
}
async function load(id){
  session=await api('/v1/sessions/'+encodeURIComponent(id));show();
  const collected=[];let cursor=0;
  for(let page=0;page<10;page++){
    const result=await api('/v1/sessions/'+id+'/events?after='+cursor);collected.push(...result.events);
    if(result.events.length<100)break;cursor=result.events.at(-1).revision;
  }
  $('trace').textContent=collected.map(e=>`${e.revision} ${e.kind} ${JSON.stringify(e.data)}`).join('\n');
}
async function list(){
  const result=await api('/v1/sessions');$('sessions').replaceChildren(new Option('Select a conversation',''));
  for(const s of result.sessions)$('sessions').add(new Option(`${s.title} — ${s.status}`,s.id));
}
async function submitPending(){
  const command=pendingCommand;
  if(!command)return;
  try{
    const result=await api(command.path,command.body);
    pendingCommand=null;
    if(command.clearMessage)$('message').value='';
    command.resultId=result.id;
  }catch(error){
    // A dropped response is ambiguous. Retain precisely the same ID and payload.
    if(error.status&&error.status<500){pendingCommand=null;}
    else error.message+=' Retry the same command to resolve its outcome.';
    throw error;
  }
  await load(command.resultId);
}
async function command(kind,fields={}){
  if(!session)throw new Error('Open a conversation first.');
  if(pendingCommand)throw new Error('Resolve the previous command with Retry first.');
  pendingCommand={path:`/v1/sessions/${session.id}/${kind}`,body:{command_id:crypto.randomUUID(),expected_revision:session.revision,...fields},clearMessage:kind==='turns'};
  await submitPending();
}
async function download(path){
  const response=await fetch(`/v1/sessions/${session.id}/files/${path.split('/').map(encodeURIComponent).join('/')}`,{headers:{Authorization:'Bearer '+$('token').value},cache:'no-store'});
  if(!response.ok)throw new Error('Unable to download the artifact with this account.');
  const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=path.split('/').at(-1);link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('connect').onclick=()=>action(async()=>{watcher?.controller.abort();watcher=null;const result=await api('/v1/agents');$('agent').replaceChildren();for(const agent of result.agents)$('agent').add(new Option(agent.id,agent.id));await list();if(session)await load(session.id);$('connection').textContent=' Connected';});
$('new').onclick=()=>action(async()=>{pendingCommand={path:'/v1/sessions',body:{command_id:crypto.randomUUID(),agent_id:$('agent').value,title:'New conversation'}};await submitPending();await list();});
$('retry').onclick=()=>action(submitPending);
$('refresh').onclick=()=>action(list);
$('sessions').onchange=()=>{if($('sessions').value)action(()=>load($('sessions').value));};
$('open').onclick=()=>action(()=>load($('session-id').value.trim()));
$('send').onclick=()=>action(async()=>{const message=$('message').value.trim();if(message)await command('turns',{text:message});});
$('cancel').onclick=()=>action(()=>command('cancel'));
$('save-memory').onclick=()=>action(()=>command('memory',{facts:JSON.parse($('memory').value)}));
$('upload').onclick=()=>action(async()=>{const file=$('file').files[0];if(!file)throw new Error('Choose a UTF-8 text file.');if(file.size>32768)throw new Error('File exceeds 32 KiB.');const path=$('file-path').value.trim()||file.name;await command('files',{path,content:new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer()),expected_digest:session.state.files?.[path]?.digest??null});});
for(const [id,approve] of [['approve',true],['reject',false]])$(id).onclick=()=>action(()=>command('decisions',{action_digest:session.state.run.pending.digest,approve,reason:$('reason').value}));
$('reconcile').onclick=()=>action(()=>command('reconcile'));
renderControls();
