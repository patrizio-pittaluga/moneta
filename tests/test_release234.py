"""Moneta 2.3.4: throttle, privacy della home, nuova categoria Calcetto."""
from pathlib import Path
import unittest
from test_templates import read_template
from test_accounts23 import Accounts23Base
from test_multiuser import body

ROOT=Path(__file__).resolve().parents[1]

class UiAndThrottle234(unittest.TestCase):
    def test_http_api_limit_allows_normal_reload_but_is_still_bounded(self):
        stage=read_template('backend.yml')['Resources']['ApiStage']['Properties']['DefaultRouteSettings']
        self.assertEqual(stage['ThrottlingRateLimit'],8)
        self.assertEqual(stage['ThrottlingBurstLimit'],15)
        self.assertLessEqual(stage['ThrottlingRateLimit'],10)
        self.assertLessEqual(stage['ThrottlingBurstLimit'],20)

    def test_429_retries_safe_requests_not_transaction_creating_post(self):
        js=(ROOT/'frontend/app.js').read_text()
        self.assertIn("const retryable=method==='GET'||method==='PUT';",js)
        self.assertIn('r.status===429&&retryable&&attempt<2',js)
        self.assertIn("r.headers?.get?.('Retry-After')",js)
        self.assertIn('Troppe richieste contemporanee (429)',js)
        self.assertIn('Operazione salvata, ma l’aggiornamento della schermata non è riuscito.',js)

    def test_home_excludes_deposit_and_shows_only_bank_cash_details(self):
        js=(ROOT/'frontend/app.js').read_text()
        html=(ROOT/'frontend/index.html').read_text()
        self.assertIn("displayBalance('totalBalance',!activeSpace&&accountsData?accountsData.spendable:balance(valid))",js)
        self.assertIn("displayPrivateValue('netLiquidity',!activeSpace&&accountsData?(accountsData.spendable-accountsData.creditDebt):balance(valid))",js)
        self.assertIn('DISPONIBILITÀ OPERATIVA',html)
        self.assertIn('esclude il conto deposito',html)
        self.assertNotIn('id="homeDepositBalance"',html)
        self.assertIn('Conto deposito',html) # menu I miei conti resta disponibile
        self.assertIn('id="homeBankBalance"',html)
        self.assertIn('id="homeCashBalance"',html)

    def test_calcetto_present_on_client_and_server(self):
        self.assertIn('"Svago", "Calcetto", "Investimenti", "Risparmio", "Viaggi"',(ROOT/'frontend/app.js').read_text())
        self.assertIn("'Svago', 'Calcetto', 'Investimenti', 'Risparmio', 'Viaggi'",(ROOT/'lambda/index.py').read_text())
        self.assertIn('moneta-static-v2.3.6',(ROOT/'frontend/sw.js').read_text())

class Accounting234(Accounts23Base,unittest.TestCase):
    def test_deposit_stays_out_of_operational_balance_even_when_it_moves(self):
        for account,value in [('bank',1000),('cash',120),('deposit',4200)]:
            response=self.req('PUT /accounts/{id}',params={'id':account},data={'name':account,'openingBalance':value})
            self.assertEqual(response['statusCode'],200,response)
        s=self.summary()
        self.assertEqual((s['spendable'],s['available'],s['depositBalance']),(1120,5320,4200))
        x=self.req('POST /transfers',data={'id':'dep-to-bank','fromAccountId':'deposit','toAccountId':'bank',
                                         'date':'2026-10-20','amount':200,'note':'Giroconto'})
        self.assertEqual(x['statusCode'],201,x)
        s=self.summary()
        self.assertEqual((s['spendable'],s['available'],s['depositBalance']),(1320,5320,4000))

    def test_calcetto_accepted_for_new_and_updated_expenses(self):
        item=self.expense('sport-football',25,'cash')
        item['category']='Calcetto'
        new=self.req('POST /transactions',data=item)
        self.assertEqual(new['statusCode'],201,new)
        updated=self.req('PUT /transactions/{id}',params={'id':'sport-football'},data={**item,'amount':30})
        self.assertEqual(updated['statusCode'],200,updated)
        txs=body(self.req('GET /state'))['transactions']
        self.assertEqual(txs[0]['category'],'Calcetto')
        self.assertEqual(self.summary()['cashBalance'],-30)

if __name__=='__main__':unittest.main()
