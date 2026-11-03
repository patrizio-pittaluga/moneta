"""Regressioni UI e MFA di Moneta 2.3.3."""
from pathlib import Path
import unittest
from test_templates import read_template

ROOT=Path(__file__).resolve().parents[1]

class Release233Tests(unittest.TestCase):
    def test_default_disables_mfa_without_replacing_pool(self):
        cf=read_template('backend.yml')
        param=cf['Parameters']['UserPoolMfaMode']
        self.assertEqual(param['Default'],'OFF')
        self.assertEqual(param['AllowedValues'],['OFF','ON'])
        self.assertEqual(cf['Resources']['UserPool']['DeletionPolicy'],'Retain')
        self.assertEqual(cf['Resources']['UserPool']['Properties']['MfaConfiguration'],{'!Ref':'UserPoolMfaMode'})
        self.assertEqual(cf['Conditions']['MfaEnabled'],{'!Equals':[{'!Ref':'UserPoolMfaMode'},'ON']})
        self.assertEqual(cf['Resources']['UserPool']['Properties']['EnabledMfas'],{'!If':['MfaEnabled',['SOFTWARE_TOKEN_MFA'],{'!Ref':'AWS::NoValue'}]})
        script=(ROOT/'scripts/deploy.sh').read_text()
        self.assertIn('"UserPoolMfaMode=$MONETA_MFA_MODE"',script)
        self.assertIn('MONETA_MFA_MODE="${MONETA_MFA_MODE:-OFF}"',script)
        self.assertIn('MONETA_MFA_MODE deve essere OFF oppure ON',script)

    def test_home_total_retained_and_bank_cash_only_breakdown(self):
        html=(ROOT/'frontend/index.html').read_text()
        js=(ROOT/'frontend/app.js').read_text()
        self.assertIn('id="homeAccountBreakdown"',html)
        self.assertIn('id="homeBankBalance"',html)
        self.assertIn('id="homeCashBalance"',html)
        self.assertNotIn('id="homeDepositBalance"',html)
        self.assertIn('accountsData.spendable:balance(valid)',js)
        self.assertIn("displayPrivateValue('homeBankBalance',accountsData.bankBalance)",js)
        self.assertIn("displayPrivateValue('homeCashBalance',accountsData.cashBalance)",js)
        self.assertIn("classList.toggle('hidden',!showPersonalAccounts)",js)
        self.assertIn('let balancesVisible=false;',js)
        self.assertIn('node.textContent=balancesVisible?node.dataset.privateValue:BALANCE_MASK;',js)
        self.assertIn('moneta-static-v2.3.5',(ROOT/'frontend/sw.js').read_text())

if __name__=='__main__':unittest.main()
