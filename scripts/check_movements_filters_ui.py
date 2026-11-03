#!/usr/bin/env python3
"""Browser locale senza backend: filtri cliccabili + overflow, nessun dato reale/AWS."""
from pathlib import Path
import re
from shutil import which
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]/'frontend'
html=(root/'index.html').read_text()
# Il test inietta un controller isolato: rimuove la CSP solo in memoria, mai dai file distribuiti.
html=re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]+>\s*','',html)
css=(root/'styles.css').read_text()
js=(root/'app.js').read_text()
html=html.replace('<link rel="stylesheet" href="styles.css">','<style>'+css+'</style>')
html=html.replace('<script src="app.js" defer></script>','')
assert 'start();\n})();' in js
js=js.replace('start();\n})();', '''window.__monetaTest = {
 init(rows){
  state.transactions=rows;
  accountsData={accounts:[{id:'bank',name:'Conto corrente',kind:'bank'},
  {id:'cash',name:'Contanti',kind:'cash'},
  {id:'credit-test',name:'Carta test',kind:'credit'}],cycles:[]};
  bindMovementFilters();renderMovements();
 },
 scope(value){activeSpace=value;movementFilters.account='';renderMovements();}
};
})();''')
rows=[
 {'id':'a','date':'2026-11-03','description':'Calcetto mercoledì','category':'Calcetto','type':'expense','amount':12,'accountId':'cash'},
 {'id':'b','date':'2026-10-16','description':'Ristorante','category':'Ristoranti','type':'expense','amount':40,'accountId':'bank'},
 {'id':'c','date':'2026-11-02','description':'PAC ETF','category':'Investimenti','type':'expense','amount':100,'accountId':'credit-test'},
 {'id':'d','date':'2026-11-01','description':'Versamento risparmio','category':'Risparmio','type':'expense','amount':150,'accountId':'bank'},
]
with sync_playwright() as playwright:
    browser=playwright.chromium.launch(headless=True,executable_path=which('chromium'))
    for width in (320,360,375,390,430,768,1024):
        page=browser.new_page(viewport={'width':width,'height':840},is_mobile=width<761,has_touch=width<761)
        errors=[]
        page.on('pageerror',lambda ex:errors.append(str(ex)))
        page.set_content(html)
        page.add_script_tag(content=js)
        page.evaluate('''rows=>{
           document.getElementById('authView').classList.add('hidden');
           document.getElementById('appView').classList.remove('hidden');
           document.querySelectorAll('.view').forEach(x=>x.classList.toggle('hidden',x.id!=='view-movements'));
           window.__monetaTest.init(rows);
        }''',rows)
        # Test fisico overflow
        scroll=page.evaluate('document.documentElement.scrollWidth')
        assert scroll<=width+1,(width,scroll)
        page.locator('#txFilterPeriod').select_option('all')
        assert page.locator('#transactionRows tbody tr').count()==4,(width,'all')
        page.locator('#txFilterCategory').select_option('Calcetto')
        assert page.locator('#transactionRows tbody tr').count()==1,(width,'category')
        page.locator('#txFilterReset').click()
        page.locator('#txFilterPeriod').select_option('custom')
        page.locator('#txFilterFrom').fill('2026-10-01')
        page.locator('#txFilterTo').fill('2026-10-31')
        assert page.locator('#transactionRows tbody tr').count()==1,(width,'date')
        page.locator('#txFilterReset').click()
        page.locator('#txFilterPeriod').select_option('all')
        page.locator('#txFilterQuery').fill('PAC ETF')
        assert page.locator('#transactionRows tbody tr').count()==1,(width,'search')
        page.locator('#txFilterQuery').fill('')
        page.locator('#txFilterAdvanced summary').click()
        page.locator('#txFilterAccount').select_option('credit-test')
        assert page.locator('#transactionRows tbody tr').count()==1,(width,'account')
        assert not errors,errors
        print(f'UI {width}px: filtri, date, conto, ricerca e overflow OK')
        page.close()
    browser.close()
