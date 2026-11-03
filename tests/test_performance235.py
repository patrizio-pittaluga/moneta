"""Guard rail statici sulla riduzione delle chiamate e privacy cache frontend."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / 'frontend/app.js').read_text(encoding='utf-8')

class Performance235Tests(unittest.TestCase):
    def test_category_edit_does_not_force_full_refresh(self):
        self.assertIn('mutateTransaction(async()=>{const result=await api(editId?', JS)
        self.assertIn('state.transactions=upsertLocalRecord(state.transactions,record)', JS)
        self.assertIn('transactionAffectsAccounts(previous,record)', JS)
        self.assertIn("const data=await api('/accounts')", JS)
        self.assertIn('function transactionAffectsAccounts(before,after)', JS)

    def test_other_simple_mutations_use_response(self):
        self.assertIn('async function mutateFavorite(', JS)
        self.assertIn('async function mutateBudget(', JS)
        self.assertIn('async function mutateRecurring(', JS)
        self.assertIn('function upsertLocalRecord(rows,record)', JS)

    def test_full_refresh_available_for_manual_reconciliation(self):
        self.assertIn("$('refreshButton').onclick=reload", JS)
        self.assertIn("await Promise.all([api('/state'),api('/favorites')", JS)
        self.assertIn("if(scope!==activeSpace)return", JS)

    def test_cached_insights_remain_private_and_not_persisted(self):
        self.assertIn('let insightsDirty=true', JS)
        self.assertIn('insightsVersion++', JS)
        self.assertIn('if(insightsRequest)return insightsRequest;', JS)
        sw = (ROOT / 'frontend/sw.js').read_text()
        self.assertIn('moneta-static-v2.3.6',sw)
        self.assertNotIn("'/accounts'", sw)
        self.assertNotIn("'/state'", sw)
        self.assertNotIn("'/insights'", sw)

if __name__=='__main__': unittest.main()
