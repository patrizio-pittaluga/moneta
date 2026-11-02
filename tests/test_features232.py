"""Moneta 2.3.2 regression: deposito e analisi dei consumi."""
import datetime as dt
import unittest
from test_accounts23 import Accounts23Base
from test_multiuser import mod, body, ALICE, MARIO
from scripts.seed_demo import build_demo

class Deposit232(Accounts23Base, unittest.TestCase):
    def test_deposit_is_liquid_but_not_spendable(self):
        self.assertEqual(self.req('PUT /accounts/{id}',params={'id':'bank'},data={'name':'Conto corrente','openingBalance':1000})['statusCode'],200)
        self.assertEqual(self.req('PUT /accounts/{id}',params={'id':'cash'},data={'name':'Contanti','openingBalance':100})['statusCode'],200)
        self.assertEqual(self.req('PUT /accounts/{id}',params={'id':'deposit'},data={'name':'Conto deposito','openingBalance':3000})['statusCode'],200)
        s=self.summary()
        self.assertEqual(s['depositBalance'],3000)
        self.assertEqual(s['available'],4100)
        self.assertEqual(s['spendable'],1100)
        self.assertEqual(s['netLiquidity'],4100)
        self.assertEqual(body(self.req('GET /accounts',sub=MARIO))['depositBalance'],0)
        self.assertEqual(self.req('GET /accounts',space='family')['statusCode'],400)

    def test_deposit_only_transfer_and_no_double_expense(self):
        self.req('PUT /accounts/{id}',params={'id':'deposit'},data={'name':'Conto deposito','openingBalance':500})
        self.assertEqual(self.req('POST /transactions',data=self.expense('bad-expense',20,'deposit'))['statusCode'],400)
        bad_income={**self.expense('bad-income',20,'deposit'),'type':'income','category':'Interessi'}
        self.assertEqual(self.req('POST /transactions',data=bad_income)['statusCode'],400)
        transfer={'id':'deposit-transfer','fromAccountId':'deposit','toAccountId':'bank',
                  'date':'2026-10-20','amount':200,'note':'Giroconto'}
        self.assertEqual(self.req('POST /transfers',data=transfer)['statusCode'],201)
        s=self.summary()
        self.assertEqual((s['depositBalance'],s['bankBalance'],s['available'],s['spendable']),(300,200,500,200))
        self.assertEqual(body(self.req('GET /state'))['transactions'],[])
        # Reverse transfer restores balances and is visible only in the same user partition.
        self.assertEqual(self.req('POST /transfers/{id}/reverse',params={'id':'deposit-transfer'},data={'reason':'Trasferimento di prova'})['statusCode'],201)
        self.assertEqual(self.summary()['depositBalance'],500)
        self.assertEqual(self.req('POST /transfers',data=transfer)['statusCode'],409)
        self.assertEqual(body(self.req('GET /accounts',sub=MARIO))['transfers'],[])

    def test_deposit_cannot_be_selected_for_recurring_or_planned(self):
        recurring={'id':'deposit-rec','description':'Addebito non consentito','type':'expense',
                   'category':'Bollette','amount':20,'startDate':'2026-11-20','frequency':'monthly',
                   'interval':1,'accountId':'deposit'}
        self.assertEqual(self.req('POST /recurring',data=recurring)['statusCode'],400)
        planned={'id':'deposit-plan','description':'Non consentito','type':'expense','category':'Bollette',
                 'amount':20,'date':'2026-11-20','accountId':'deposit'}
        self.assertEqual(self.req('POST /planned',data=planned)['statusCode'],400)

    def test_new_categories_are_accepted(self):
        self.assertEqual(self.req('POST /transactions',data={**self.expense('dining',30),
                        'category':'Ristoranti'})['statusCode'],201)
        self.assertEqual(self.req('POST /transactions',data={**self.expense('bonus',120),
                        'type':'income','category':'Bonus e premi'})['statusCode'],201)

class Insights232(unittest.TestCase):
    def test_spending_insights_exclude_transfers_and_corrections(self):
        today=dt.date(2026,11,2)
        rows=[]
        for month in ('2026-08','2026-09','2026-10'):
            rows.extend([
                {'date':month+'-02','category':'Stipendio','type':'income','amount':2000},
                {'date':month+'-05','category':'Alimentari','type':'expense','amount':320},
                {'date':month+'-08','category':'Ristoranti','type':'expense','amount':180},
                {'date':month+'-12','category':'Shopping','type':'expense','amount':90},
                {'date':month+'-13','category':'Ristoranti','type':'expense','amount':100,'isTransfer':True},
                {'date':month+'-14','category':'Shopping','type':'expense','amount':100,'isCorrection':True},
            ])
        result=mod.spending_diagnostics(rows,today)
        self.assertEqual(result['monthlyExpenses'],590)
        self.assertEqual(result['monthlyIncome'],2000)
        self.assertEqual(result['monthlySavings'],1410)
        self.assertEqual(result['monthlyDiscretionary'],270)
        self.assertEqual(result['categories'][0]['name'],'Alimentari')
        self.assertEqual(result['categories'][1]['name'],'Ristoranti')
        self.assertTrue(any('15%' in s['title'] for s in result['suggestions']))

    def test_no_false_accuracy_for_sparse_history(self):
        result=mod.spending_diagnostics([{'date':'2026-10-01','category':'Alimentari',
            'type':'expense','amount':12}],dt.date(2026,11,2))
        self.assertTrue(any('Completa' in n['title'] for n in result['suggestions']))

    def test_seed_is_fictional_target_partition_and_consistent(self):
        items=build_demo('fake-sub-test',dt.date(2026,11,2))
        self.assertTrue(35<len(items)<=100)
        self.assertEqual(len(set(row['SK'] for row in items)),len(items))
        self.assertTrue(all(row['PK']=='USER#fake-sub-test' for row in items))
        self.assertTrue(any(row['SK']=='ACCOUNT#deposit' for row in items))
        self.assertTrue(any(row['SK'].startswith('REC#') for row in items))
        self.assertTrue(any(row['SK']=='DEMO#SEED_V1' for row in items))
        cc=[r for r in items if r['SK'].startswith('TX#') and r['value'].get('accountId')=='cc-demo']
        self.assertEqual(len(cc),1)
        cycle=[r for r in items if r['SK'].startswith('CYCLE#')][0]
        self.assertEqual(cycle['chargeCents'],cc[0]['value']['amountCents'])

if __name__=='__main__':unittest.main()
