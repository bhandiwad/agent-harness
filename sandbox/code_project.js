// Runs only inside the disposable code container; no service credentials are present.
process.env.NODE_PATH='/opt/packages/node_modules';
require('node:module').Module._initPaths();
const fs = require('node:fs');
const path = require('node:path');
const payload = JSON.parse(fs.readFileSync('/workspace/project.json','utf8'));
const root = '/workspace/project';
fs.mkdirSync(root);
for (const [name, value] of Object.entries(payload.project.files)) {
  const target=path.join(root,name); fs.mkdirSync(path.dirname(target),{recursive:true}); fs.writeFileSync(target,value);
}
const pause=Symbol('pause'); let index=0; let logs=''; const files={};
const effect=request=>{
  const prior=payload.replay[index++];
  if(!prior)throw {pause,request};
  const stable=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.keys(v).sort().reduce((a,k)=>(a[k]=v[k],a),{}):v);
  if(stable(request)!==stable(prior.request))throw Error('An earlier call changed while resuming. Keep calls deterministic.');
  return prior.result;
};
const ctx={generate:prompt=>effect({kind:'model',prompt}),tool:(name,args)=>effect({kind:'tool',name,arguments:args}),writeFile:(name,content)=>files[name]=content};
console.log=(...values)=>{logs=(logs+values.map(v=>typeof v==='string'?v:JSON.stringify(v)).join(' ')+'\n').slice(-4000);};
console.error=console.log;
(async()=>{
  let result;
  try {
    const {run}=require(path.join(root,payload.project.entrypoint));
    const answer=await run(payload.input,ctx);
    if(typeof answer!=='string'||!answer.trim())throw Error('run(input, ctx) must return a nonempty string.');
    result={done:true,answer,files};
  }catch(error){
    if(error&&error.pause===pause)result={done:false,request:error.request};
    else {result={error:String(error?.message||error).slice(0,2000)};logs=(logs+'\n'+String(error?.stack||error)).slice(-4000);}
  }
  let raw=Buffer.from(JSON.stringify({...result,logs}));
  if(raw.length>600000)raw=Buffer.from(JSON.stringify({error:'Combined code outputs exceed 600 KB. Split the task into smaller batches.'}));
  if(raw.length>30000)raw=Buffer.from(JSON.stringify({encoding:'zlib-base64-v1',data:require('node:zlib').deflateSync(raw).toString('base64')}));
  if(raw.length>32768)raw=Buffer.from(JSON.stringify({error:'Compressed outputs exceed the 32 KB transport limit. Split the task into smaller batches.'}));
  fs.writeFileSync('/workspace/result.json',raw);
})();
