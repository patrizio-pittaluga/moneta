// Test comportamentale isolato: nessuna chiamata AWS o movimento reale.
'use strict';
const fs=require('fs');const assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'..','frontend','app.js'),'utf8');
const start=source.indexOf("async function api(path,method='GET',obj){");
const end=source.indexOf('async function reload()',start);
assert(start>0 && end>start);
const implementation=source.slice(start,end);
async function scenario(method,statuses,expectedCalls,expectedSuccess){
 let calls=0;
 const fetch=async (_url, opts)=>{
  calls++;
  assert.strictEqual(opts.method,method);
  const status=statuses[Math.min(calls-1,statuses.length-1)];
  return {status,ok:status>=200&&status<300,headers:{get:()=>null},json:async()=>status===429?{error:'Too Many Requests'}:{result:'ok'}};
 };
 const make=new Function('fetch','jwtExpired','token','sessionStorage','config','scopedPath','setTimeout','Math',implementation+'\nreturn api;');
 const fn=make(fetch,()=>false,'test-token',{removeItem:()=>{}},{apiUrl:'https://example.invalid'},v=>v,fn=>fn(),Math);
 let result,failed=false;
 try{result=await fn('/transactions/id',method,method==='GET'?undefined:{amount:10});}
 catch(e){failed=true;assert(/Troppe richieste contemporanee/.test(e.message));}
 assert.strictEqual(calls,expectedCalls,method+' call count');
 assert.strictEqual(!failed,expectedSuccess,method+' expected success');
 if(expectedSuccess)assert.deepStrictEqual(result,{result:'ok'});
}
(async()=>{
 await scenario('GET',[429,200],2,true);
 await scenario('PUT',[429,200],2,true);
 await scenario('POST',[429,200],1,false);
 await scenario('DELETE',[429,200],1,false);
 await scenario('GET',[429,429,429],3,false);
 console.log('OK: retry 429 GET/PUT; nessun retry POST/DELETE; esaurimento limitato.');
})().catch(e=>{console.error(e);process.exit(1);});
