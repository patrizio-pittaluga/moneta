"""Moneta 2.3: regression tests for separate cash, bank and deferred-credit ledgers.
These tests are local, require no real AWS credentials, and reuse the same FakeClient
used to verify multiuser operations.
"""
import datetime as dt
from decimal import Decimal
import unittest
from unittest.mock import patch
from test_multiuser import mod, FakeTable, FakeClient, request, body, ALICE, MARIO

class Accounts23Base:
    def setUp(self):
        self.db=FakeTable()
        mod.TABLE=self.db
        self.client=FakeClient(self.db)
        self.patch=patch.object(mod.boto3,'client',return_value=self.client)
        self.patch.start()
    def tearDown(self):self.patch.stop()
    def req(self,route,**kw):return request(route,**kw)
    def create_credit(self,**updates):
        item={'id':'cc-my-card','name':'Carta personale','closingDay':25,'chargeDay':10,
              'chargeMonthOffset':1,'settlementMode':'confirm'}
        item.update(updates)
        return self.req('POST /accounts',data=item)
    def expense(self,ident,amount=80,account='bank',date='2026-10-20'):
        return {'id':ident,'description':'Spesa','category':'Alimentari','type':'expense',
                'amount':amount,'date':date,'note':'','accountId':account}
    def summary(self):return body(self.req('GET /accounts'))

class Accounts23Tests(Accounts23Base,unittest.TestCase):
    def test_accounts_are_private_and_initially_zero(self):
        x=self.summary()
        self.assertEqual({a['id'] for a in x['accounts']},{'bank','cash'})
        self.assertEqual((x['bankBalance'],x['cashBalance'],x['creditDebt']),(0,0,0))
        self.assertEqual(body(self.req('GET /accounts',sub=MARIO))['accounts'],x['accounts'])
        self.assertEqual(self.req('GET /accounts',space='family')['statusCode'],400)

    def test_opening_balance_transfer_and_expenses(self):
        self.assertEqual(self.req('PUT /accounts/{id}',params={'id':'bank'},data={'name':'Conto corrente','openingBalance':1000})['statusCode'],200)
        self.assertEqual(self.req('PUT /accounts/{id}',params={'id':'cash'},data={'name':'Contanti','openingBalance':150})['statusCode'],200)
        self.assertEqual(self.req('POST /transactions',data=self.expense('bank-food',120))['statusCode'],201)
        self.assertEqual(self.req('POST /transactions',data=self.expense('cash-food',30,'cash'))['statusCode'],201)
        self.assertEqual(self.req('POST /transfers',data={'id':'atm-1','fromAccountId':'bank','toAccountId':'cash',
            'amount':100,'date':'2026-10-20','note':'Prelievo'})['statusCode'],201)
        x=self.summary()
        self.assertEqual((x['bankBalance'],x['cashBalance'],x['available'],x['netLiquidity']),(780,220,1000,1000))
        self.assertEqual(len(x['transfers']),1)
        self.assertEqual(len(body(self.req('GET /state'))['transactions']),2)  # no fake expenses from withdrawal
        self.assertEqual(self.req('POST /transfers',data={'id':'atm-1','fromAccountId':'bank','toAccountId':'cash','amount':100,'date':'2026-10-20'})['statusCode'],409)

    def test_dynamodb_decimal_card_settings_do_not_break_account_summary(self):
        """Reproduce Lambda Python 3.13: calendar.monthrange rejects Decimal month.

        DynamoDB actually returns Decimal for *all* numeric attributes, unlike
        the earlier FakeTable tests which kept Python int for put_item().
        """
        self.assertEqual(self.create_credit()['statusCode'],201)
        card=self.db.items[(f'USER#{ALICE}', 'ACCOUNT#cc-my-card')]['value']
        for name in ('openingCents','closingDay','chargeDay','chargeMonthOffset'):
            card[name]=Decimal(str(card[name]))
        self.assertEqual(self.req('POST /transactions',data=self.expense('decimal-cc',80,'cc-my-card'))['statusCode'],201)
        summary_response=self.req('GET /accounts')
        self.assertEqual(summary_response['statusCode'],200,summary_response)
        summary=body(summary_response)
        self.assertEqual(summary['creditDebt'],80)
        self.assertEqual(summary['cycles'][0]['dueDate'],'2026-11-10')
        self.assertEqual(type(next(a for a in summary['accounts'] if a['id']=='cc-my-card')['chargeMonthOffset']),int)
        # Saving a previously loaded card must also accept valid persisted Decimals.
        update=self.req('PUT /accounts/{id}',params={'id':'cc-my-card'},data={'name':'Carta aggiornata'})
        self.assertEqual(update['statusCode'],200,update)
        self.assertEqual(body(self.req('GET /accounts'))['cycles'][0]['amount'],80)

    def test_credit_cycle_last_day_with_decimal_fields(self):
        self.assertEqual(self.create_credit(closingDay=31,chargeDay=31)['statusCode'],201)
        card=self.db.items[(f'USER#{ALICE}', 'ACCOUNT#cc-my-card')]['value']
        for name in ('closingDay','chargeDay','chargeMonthOffset'):
            card[name]=Decimal(card[name])
        self.assertEqual(self.req('POST /transactions',data=self.expense('feb-cc',35,'cc-my-card','2026-02-28'))['statusCode'],201)
        self.assertEqual(body(self.req('GET /accounts'))['cycles'][0]['dueDate'],'2026-03-31')

    def test_credit_expense_updates_liability_not_bank_and_once_settled_reduces_bank(self):
        self.req('PUT /accounts/{id}',params={'id':'bank'},data={'name':'Conto corrente','openingBalance':2000})
        self.assertEqual(self.create_credit()['statusCode'],201)
        self.assertEqual(self.req('POST /transactions',data=self.expense('cc1',80,'cc-my-card'))['statusCode'],201)
        x=self.summary()
        self.assertEqual(x['bankBalance'],2000)
        self.assertEqual(x['creditDebt'],80)
        self.assertEqual(x['netLiquidity'],1920)
        self.assertEqual(x['cycles'][0]['cycle'],'2026-10')
        self.assertEqual(x['cycles'][0]['dueDate'],'2026-11-10')
        self.assertEqual(self.req('POST /cards/{id}/cycles/{cycle}/settle',
            params={'id':'cc-my-card','cycle':'2026-10'},data={'paidDate':'2026-10-28'})['statusCode'],201)
        paid=self.summary()
        self.assertEqual(paid['bankBalance'],1920)
        self.assertEqual(paid['creditDebt'],0)
        self.assertEqual(paid['netLiquidity'],1920)
        self.assertEqual(paid['cycles'][0]['status'],'paid')
        self.assertEqual(self.req('POST /cards/{id}/cycles/{cycle}/settle',
            params={'id':'cc-my-card','cycle':'2026-10'},data={'paidDate':'2026-10-28'})['statusCode'],400)
        self.assertEqual(len(body(self.req('GET /state'))['transactions']),1)  # settlement is not another expense

    def test_closed_cycle_does_not_allow_late_changes(self):
        self.create_credit()
        self.req('POST /transactions',data=self.expense('c1',20,'cc-my-card'))
        self.req('POST /cards/{id}/cycles/{cycle}/settle',params={'id':'cc-my-card','cycle':'2026-10'},data={'paidDate':'2026-10-28'})
        self.assertEqual(self.req('POST /transactions',data=self.expense('c2',30,'cc-my-card'))['statusCode'],409)
        self.assertEqual(self.req('PUT /transactions/{id}',params={'id':'c1'},data=self.expense('c1',25,'cc-my-card'))['statusCode'],409)
        self.assertEqual(self.req('DELETE /transactions/{id}',params={'id':'c1'})['statusCode'],409)
        self.assertEqual(self.req('POST /transactions',data=self.expense('c3',12,'cc-my-card','2026-10-26'))['statusCode'],201)
        self.assertEqual(self.summary()['cycles'][0]['cycle'],'2026-11')

    def test_card_dates_and_income_validation(self):
        bad=self.create_credit(closingDay=25,chargeDay=10,chargeMonthOffset=0)
        self.assertEqual(bad['statusCode'],400)
        self.assertEqual(self.create_credit()['statusCode'],201)
        self.assertEqual(self.req('POST /transactions',data={**self.expense('incorrect',10,'cc-my-card'),'type':'income','category':'Extra'})['statusCode'],400)
        self.assertEqual(self.req('POST /transactions',data=self.expense('fake-card',20,'cc-not-mine'))['statusCode'],400)
        self.assertEqual(self.req('POST /transactions',sub=MARIO,data=self.expense('someone-else',20,'cc-my-card'))['statusCode'],400)
        x=self.req('PUT /accounts/{id}',params={'id':'cc-my-card'},data={'name':'Carta nuova','closingDay':24,'chargeDay':10,'chargeMonthOffset':1,'settlementMode':'confirm'})
        self.assertEqual(x['statusCode'],200) # no recorded transactions yet
        self.req('POST /transactions',data=self.expense('c1',20,'cc-my-card'))
        x=self.req('PUT /accounts/{id}',params={'id':'cc-my-card'},data={'name':'Carta nuova','closingDay':25,'chargeDay':10,'chargeMonthOffset':1,'settlementMode':'confirm'})
        self.assertEqual(x['statusCode'],400)

    def test_legacy_transactions_are_bank_by_default(self):
        self.assertEqual(self.req('POST /transactions',data={k:v for k,v in self.expense('legacy',25).items() if k!='accountId'})['statusCode'],201)
        self.assertEqual(self.summary()['bankBalance'],-25)

    def test_recurring_first_credit_purchase_has_no_immediate_bank_debit(self):
        self.create_credit()
        x=self.req('POST /recurring',data={'id':'cc-month','description':'Abbonamento','type':'expense','category':'Svago',
            'amount':15,'startDate':'2026-10-20','note':'','frequency':'monthly','interval':1,
            'active':True,'accountId':'cc-my-card','recordFirstOccurrence':True})
        self.assertEqual(x['statusCode'],201,x)
        self.assertEqual(self.summary()['bankBalance'],0)
        self.assertEqual(self.summary()['creditDebt'],15)
        self.assertEqual(len(body(self.req('GET /state'))['transactions']),1)

    def test_cash_and_credit_are_not_visible_to_other_user(self):
        self.req('PUT /accounts/{id}',params={'id':'cash'},data={'name':'Contanti','openingBalance':900})
        self.create_credit()
        self.req('POST /transactions',data=self.expense('private',100,'cc-my-card'))
        data=body(self.req('GET /accounts',sub=MARIO))
        self.assertEqual(len(data['accounts']),2)
        self.assertEqual((data['cashBalance'],data['creditDebt']),(0,0))

    def test_card_months_clamp_correctly(self):
        card={'id':'card','closingDay':31,'chargeDay':31,'chargeMonthOffset':1}
        self.assertEqual(mod.card_cycle(card,'2026-02-28'),'2026-02')
        self.assertEqual(mod.card_due(card,'2026-02'),'2026-03-31')
        self.assertEqual(mod.card_cycle(card,'2026-03-31'),'2026-03')
        self.assertEqual(mod.card_cycle(card,'2026-04-30'),'2026-04')

if __name__=='__main__':unittest.main()

class SettlementAutomationTests(Accounts23Base,unittest.TestCase):
    def test_transfer_reversal_is_idempotent_and_restores_balances(self):
        self.req('PUT /accounts/{id}',params={'id':'bank'},data={'name':'Conto corrente','openingBalance':300})
        self.assertEqual(self.req('POST /transfers',data={'id':'withdraw','fromAccountId':'bank','toAccountId':'cash','amount':90,'date':'2026-10-15'})['statusCode'],201)
        self.assertEqual((self.summary()['bankBalance'],self.summary()['cashBalance']),(210,90))
        data=self.req('POST /transfers/{id}/reverse',params={'id':'withdraw'},data={'reason':'Prelievo registrato per sbaglio'})
        self.assertEqual(data['statusCode'],201,data)
        self.assertEqual((self.summary()['bankBalance'],self.summary()['cashBalance']),(300,0))
        self.assertEqual(self.req('POST /transfers/{id}/reverse',params={'id':'withdraw'},data={'reason':'Prelievo registrato per sbaglio'})['statusCode'],409)
        self.assertEqual(len(self.summary()['transfers']),2)
        self.assertTrue(next(x for x in self.summary()['transfers'] if x['id']=='withdraw')['reversed'])

    def test_card_auto_charge_falls_on_configured_day_without_second_expense(self):
        # Create a same-month billing cycle, then activate automatic settlement.
        data=self.create_credit(closingDay=20,chargeDay=27,chargeMonthOffset=0,settlementMode='automatic')
        self.assertEqual(data['statusCode'],201,data)
        self.assertEqual(self.req('POST /transactions',data=self.expense('c1',35,'cc-my-card','2026-10-10'))['statusCode'],201)
        self.assertEqual(self.summary()['creditDebt'],35)
        # The card's automatic schedule starts when activated, so backfill is opt-in.
        card=self.db.items[('USER#'+ALICE,'ACCOUNT#cc-my-card')]['value']
        card['autoFrom']='2026-10-01'
        def scan(**params):
            pref=params['ExpressionAttributeValues'].get(':p')
            return {'Items':[row for (_,sk),row in self.db.items.items() if sk.startswith(pref or 'ACCOUNT#')]}
        self.db.scan=scan
        self.assertEqual(mod.run_auto_card_settlements(dt.date(2026,10,28)),1)
        self.assertEqual(mod.run_auto_card_settlements(dt.date(2026,10,28)),0)
        x=self.summary()
        self.assertEqual((x['bankBalance'],x['creditDebt'],x['netLiquidity']),(-35,0,-35))
        self.assertEqual(x['cycles'][0]['paidDate'],'2026-10-27')
        self.assertEqual(len(body(self.req('GET /state'))['transactions']),1)

    def test_planned_cash_payment_is_recorded_only_on_confirmation(self):
        self.req('PUT /accounts/{id}',params={'id':'cash'},data={'name':'Contanti','openingBalance':100})
        planned={'id':'bill1','description':'Pagamento programmato','type':'expense','amount':25,'dueDate':'2026-10-21',
                 'category':'Casa','note':'','accountId':'cash'}
        self.assertEqual(self.req('POST /planned',data=planned)['statusCode'],201)
        self.assertEqual(self.summary()['cashBalance'],100)
        r=self.req('POST /planned/{id}/complete',params={'id':'bill1'},data={'paidDate':'2026-10-22'})
        self.assertEqual(r['statusCode'],200,r)
        self.assertEqual(self.summary()['cashBalance'],75)
        self.assertEqual(self.req('POST /planned/{id}/complete',params={'id':'bill1'},data={'paidDate':'2026-10-22'})['statusCode'],200)
        self.assertEqual(self.summary()['cashBalance'],75)

class SettlementRaceTests(Accounts23Base,unittest.TestCase):
    def test_settlement_aborts_if_new_charge_changes_the_cycle_before_commit(self):
        self.create_credit()
        self.req('POST /transactions',data=self.expense('first',80,'cc-my-card'))
        original=self.client.transact_write_items
        raced=[False]
        def race(TransactItems):
            if (not raced[0] and any(a.get('ConditionCheck',{}).get('ConditionExpression','').startswith('revision =') for a in TransactItems)):
                raced[0]=True
                key=('USER#'+ALICE,'CYCLE#cc-my-card#2026-10')
                self.db.items[key]['chargeCents']+=20*100
                self.db.items[key]['revision']+=1
                self.db.items[('USER#'+ALICE,'TX#race')]={'PK':'USER#'+ALICE,'SK':'TX#race','value':{
                    'id':'race','description':'Raced charge','type':'expense','category':'Alimentari',
                    'amountCents':2000,'date':'2026-10-21','note':'','accountId':'cc-my-card'}}
            return original(TransactItems=TransactItems)
        self.client.transact_write_items=race
        result=self.req('POST /cards/{id}/cycles/{cycle}/settle',params={'id':'cc-my-card','cycle':'2026-10'},data={'paidDate':'2026-10-28'})
        self.assertEqual(result['statusCode'],409,result)
        self.assertFalse(any(sk.startswith('SETTLE#') for (_,sk) in self.db.items))
        self.client.transact_write_items=original
        result=self.req('POST /cards/{id}/cycles/{cycle}/settle',params={'id':'cc-my-card','cycle':'2026-10'},data={'paidDate':'2026-10-28'})
        self.assertEqual(result['statusCode'],201,result)
        self.assertEqual(self.summary()['creditDebt'],0)

class CreditRecurringTests(Accounts23Base,unittest.TestCase):
    def test_recurring_credit_payments_extend_debt_without_touching_bank(self):
        self.create_credit()
        rule={'id':'insurance','description':'Abbonamento','type':'expense','category':'Svago',
              'amount':20,'startDate':'2026-10-20','note':'','frequency':'monthly','interval':1,
              'active':True,'accountId':'cc-my-card','recordFirstOccurrence':True}
        self.assertEqual(self.req('POST /recurring',data=rule)['statusCode'],201)
        self.assertEqual(mod.run_recurring(dt.date(2026,11,20))['created'],1)
        cycles=self.summary()['cycles']
        self.assertEqual({x['cycle'] for x in cycles},{'2026-10','2026-11'})
        self.assertEqual(len(body(self.req('GET /state'))['transactions']),2)
        self.assertEqual(self.summary()['bankBalance'],0)
        self.assertEqual(mod.run_recurring(dt.date(2026,11,20))['created'],0)
