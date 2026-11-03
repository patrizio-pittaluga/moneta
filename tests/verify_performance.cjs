'use strict';
// Test comportamentali del controller frontend. Non richiede DOM, AWS né rete.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'..','frontend','app.js'),'utf8');
const start=source.indexOf('function transactionAffectsAccounts(before,after){');
const end=source.indexOf('function download(',start);
assert(start>=0&&end>start,'helper delle mutazioni non trovato');
const implementation=source.slice(start,end);
function setup({scope='',view='movements',initial={id:'tx-1',type:'expense',date:'2026-11-01',amount:25,category:'Altro',accountId:'bank'}}={}){
 const trace=[];let calls=0;
 const ctx={scope,view,state:{transactions:[initial],budgets:{},recurring:[],planned:[]},accountsData:{bankBalance:100,cashBalance:0}};
 const api=async uri=>{calls++;trace.push(uri);if(uri==='/accounts')return {bankBalance:75,cashBalance:0};throw Error('Unexpected API '+uri);};
 const fn=new Function('ctx','api','render','invalidateInsights','loadInsights','loadFund','error','clearError','DEFAULT_BUDGETS',
  `let state=ctx.state,activeSpace=ctx.scope,accountsData=ctx.accountsData,view=ctx.view,busy=false;\n${implementation}\nreturn {apply: async (rec,opts)=>{await applyTransactionChange(rec,opts);return {state,accountsData}}, affects:transactionAffectsAccounts, upsert:upsertLocalRecord, mutateTransaction, mutateRecurring, mutateFavorite, mutateBudget, getState:()=>({state,accountsData})};`);
 const notices=[];
 const handlers=fn(ctx,api,()=>trace.push('render'),()=>trace.push('invalidate'),async()=>trace.push('insights'),async()=>trace.push('fund'),msg=>notices.push(msg),()=>{},{});
 return {trace,notices,handlers,get calls(){return calls;}};
}
(async()=>{
 const newCategory={id:'tx-1',type:'expense',date:'2026-11-01',amount:25,category:'Calcetto',accountId:'bank'};
 const old={...newCategory,category:'Altro'};
 {
  const c=setup();const {state,accountsData}=await c.handlers.apply(newCategory,{previous:old});
  assert.equal(c.calls,0,'Solo categoria: nessuna GET');
  assert.equal(state.transactions.length,1);assert.equal(state.transactions[0].category,'Calcetto');
  assert.equal(accountsData.bankBalance,100,'Saldo esistente non cambiato');
  assert.deepEqual(c.trace,['invalidate','render']);
 }
 {
  const c=setup();const newAmount={...newCategory,amount:30};
  const {accountsData}=await c.handlers.apply(newAmount,{previous:old});
  assert.deepEqual(c.trace,['/accounts','invalidate','render']);
  assert.equal(accountsData.bankBalance,75,'Saldo autorevole dal server');
 }
 {
  const c=setup();await c.handlers.apply(null,{deletedId:'tx-1',previous:old});
  assert.equal(c.calls,1);assert.deepEqual(c.handlers.getState().state.transactions,[]);
 }
 {
  const c=setup({scope:'s-casa'});await c.handlers.apply(newCategory,{previous:old});
  assert.equal(c.calls,0,'Fondo condiviso: non leggere i conti personali');
 }
 {
  const c=setup({view:'home'});await c.handlers.apply(newCategory,{previous:old});
  assert.equal(c.calls,0,'Home: solo categoria, non ricaricare i conti');
  assert(c.trace.includes('insights'),'Home: Insights aggiornati');
 }
 {
  const c=setup();await c.handlers.mutateTransaction(async()=>newCategory,{previous:old});
  assert.equal(c.handlers.getState().state.transactions[0].category,'Calcetto');
  assert.equal(c.calls,0);
 }
 {
  const c=setup();await c.handlers.mutateTransaction(async()=>{throw Error('Errore PUT')},{previous:old});
  assert.equal(c.notices[0],'Errore PUT');
  assert.equal(c.handlers.getState().state.transactions[0].category,'Altro','Nessun update ottimistico se PUT fallisce');
 }
 {
  const c=setup();assert.throws(()=>c.handlers.upsert([],{category:'Calcetto'}),/Risposta incompleta/);
  assert.equal(c.handlers.affects(old,newCategory),false);
  assert.equal(c.handlers.affects(old,{...newCategory,accountId:'cash'}),true);
 }
 console.log('OK: edit categoria 1 chiamata totale; edit importo 2; delete 2; no saldo ottimistico; bilanci segregati.');
})().catch(e=>{console.error(e);process.exit(1);});
