"""Nuovi filtri solo frontend e categorie accettate dal backend senza migrazione."""
from pathlib import Path
import unittest
from test_accounts23 import Accounts23Base
from test_multiuser import body

ROOT=Path(__file__).resolve().parents[1]

class Filters236UI(unittest.TestCase):
    def test_controls_and_accessibility(self):
        markup=(ROOT/'frontend/index.html').read_text()
        for x in ('txFilterPeriod','txFilterFrom','txFilterTo','txFilterCategory','txFilterType',
                  'txFilterAccount','txFilterRecurrence','txFilterQuery','txFilterMin',
                  'txFilterMax','txFilterReset','txFilterFeedback'):
            self.assertIn('id="'+x+'"',markup)
        self.assertIn('aria-live="polite"',markup)

    def test_filters_do_not_invoke_network(self):
        source=(ROOT/'frontend/app.js').read_text()
        pure=source.split('const movementFilters=',1)[1].split('function recurringTable(){',1)[0]
        self.assertIn('function filterMovementRows(',pure)
        self.assertIn('function renderMovements()',pure)
        self.assertNotIn('api(',pure)
        self.assertNotIn('fetch(',pure)

    def test_new_expense_categories_on_frontend_and_lambda(self):
        js=(ROOT/'frontend/app.js').read_text()
        py=(ROOT/'lambda/index.py').read_text()
        for name in ('Investimenti','Risparmio'):
            self.assertIn('"'+name+'"',js)
            self.assertIn("'"+name+"'",py)

    def test_pwa_refresh_uses_new_version_without_api_cache(self):
        sw=(ROOT/'frontend/sw.js').read_text()
        self.assertIn('moneta-static-v2.3.6',sw)
        self.assertNotIn("'/transactions'",sw)

class Categories236Backend(Accounts23Base,unittest.TestCase):
    def test_both_categories_are_valid_on_create_and_update(self):
        for i,cat in enumerate(('Investimenti','Risparmio')):
            payload={**self.expense('new-236-'+str(i),25+i),'category':cat}
            r=self.req('POST /transactions',data=payload)
            self.assertEqual(r['statusCode'],201,r)
            r=self.req('PUT /transactions/{id}',params={'id':payload['id']},data={**payload,'category':'Calcetto'})
            self.assertEqual(r['statusCode'],200,r)
            r=self.req('PUT /transactions/{id}',params={'id':payload['id']},data=payload)
            self.assertEqual(r['statusCode'],200,r)
        categories={t['category'] for t in body(self.req('GET /state'))['transactions']}
        self.assertEqual(categories,{'Investimenti','Risparmio'})

    def test_category_change_does_not_change_bank_balance(self):
        self.req('PUT /accounts/{id}',params={'id':'bank'},data={'name':'Conto corrente','openingBalance':500})
        payload={**self.expense('savings-236',75),'category':'Risparmio'}
        self.assertEqual(self.req('POST /transactions',data=payload)['statusCode'],201)
        before=self.summary()['bankBalance']
        self.assertEqual(self.req('PUT /transactions/{id}',params={'id':'savings-236'},data={**payload,'category':'Investimenti'})['statusCode'],200)
        self.assertEqual(self.summary()['bankBalance'],before)

if __name__=='__main__': unittest.main()
