'use strict';
// Test del filtro reale del frontend, senza account, DOM, rete o servizi AWS.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const src=fs.readFileSync(path.join(__dirname,'..','frontend','app.js'),'utf8');
const a=src.indexOf('const movementFilters='),b=src.indexOf('function recurringTable(){',a);
assert(a>=0&&b>a,'Modulo filtri non trovato');
const block=src.slice(a,b);
const now='2026-11-03';
const month=new Date(2026,10,1);
const monthKey=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
const txSort=(a,b)=>b.date.localeCompare(a.date)||b.id.localeCompare(a.id);
const {filterMovementRows, movementPeriodBounds}=new Function('month','monthKey','localDay','txSort',block+'\nreturn {filterMovementRows,movementPeriodBounds};')(month,monthKey,()=>now,txSort);
const rows=[
 {id:'1',date:'2026-10-29',type:'expense',category:'Calcetto',description:'Partita',amount:15,accountId:'cash'},
 {id:'2',date:'2026-11-01',type:'expense',category:'Calcetto',description:'Iscrizione campionato',amount:75,accountId:'bank',note:'Amici'},
 {id:'3',date:'2026-11-02',type:'expense',category:'Risparmio',description:'Accantonamento',amount:100,accountId:'bank',recurringId:'r1'},
 {id:'4',date:'2026-11-03',type:'income',category:'Stipendio',description:'Azienda',amount:2000,accountId:'bank'},
 {id:'5',date:'2026-11-03',type:'expense',category:'Investimenti',description:'ETF all-world',amount:100,accountId:'cc-1'},
 {id:'6',date:'2026-11-03',type:'expense',category:'Altro',description:'Storno prova',amount:6,accountId:'bank',isReversed:true},
 {id:'7',date:'2026-09-01',type:'expense',category:'Casa',description:'Affitto',amount:600,accountId:'bank'},
];
const run=(f={},scope='')=>filterMovementRows(rows,{period:'all',category:'',type:'',account:'',recurrence:'',query:'',from:'',to:'',min:'',max:'',...f},scope,now);
const ids=(f={},scope='')=>run(f,scope).rows.map(t=>t.id);
assert.deepEqual(ids({period:'month'}),['6','5','4','3','2']);
assert.deepEqual(ids({period:'previous'}),['1']);
assert.deepEqual(ids({period:'90days'}),['6','5','4','3','2','1','7']);
assert.deepEqual(ids({period:'year'}),['6','5','4','3','2','1','7']);
assert.deepEqual(ids({category:'Calcetto'}),['2','1']);
assert.deepEqual(ids({category:'Investimenti'}),['5']);
assert.deepEqual(ids({category:'Risparmio'}),['3']);
assert.deepEqual(ids({type:'income'}),['4']);
assert.deepEqual(ids({type:'expense',category:'Calcetto',period:'month'}),['2']);
assert.deepEqual(ids({account:'cash'}),['1']);
assert.deepEqual(ids({account:'cc-1'}),['5']);
assert.deepEqual(ids({recurrence:'recurring'}),['3']);
assert.deepEqual(ids({recurrence:'single'}),['6','5','4','2','1','7']);
assert.deepEqual(ids({recurrence:'reversed'}),['6']);
assert.deepEqual(ids({recurrence:'active'}),['5','4','3','2','1','7']);
assert.deepEqual(ids({query:'canton'}),['3']); // accantonamento
assert.deepEqual(ids({query:'amici'}),['2']); // nota
assert.deepEqual(ids({min:'75',max:'100'}),['5','3','2']);
assert.deepEqual(ids({period:'custom',from:'2026-10-30',to:'2026-11-02'}),['3','2']);
assert.deepEqual(ids({period:'custom',from:'2026-11-03'}),['6','5','4']);
assert.deepEqual(ids({period:'custom',to:'2026-10-29'}),['1','7']);
assert.match(run({period:'custom'}).error,/Seleziona almeno/);
assert.match(run({period:'custom',from:'2026-11-05',to:'2026-11-01'}).error,/data iniziale/);
assert.match(run({min:'130',max:'100'}).error,/importi/);
const shared=[{id:'s1',date:'2026-11-03',type:'expense',category:'Alimentari',description:'Spesa familiare',amount:35,paymentSource:'fund'},
{id:'s2',date:'2026-11-02',type:'expense',category:'Alimentari',description:'Anticipo',amount:40,paymentSource:'personal'}];
const original=rows.slice();
assert.deepEqual(filterMovementRows(shared,{period:'all',account:'personal'},'space-1',now).rows.map(x=>x.id),['s2']);
assert.deepEqual(rows,original,'Non modificare gli array originali');
assert.deepEqual(movementPeriodBounds({period:'month'},now),{from:'2026-11-01',to:'2026-11-30'});
console.log('OK: filtri combinabili, date arbitrarie, importi, carta/contanti, ricorrenze, privacy del fondo e date errate.');
