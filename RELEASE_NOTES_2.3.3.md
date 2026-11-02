# Moneta 2.3.3 — Home: saldo banca/contanti e accesso senza MFA

## Funzioni
- Il totale complessivo in Panoramica rimane **banca + contanti + deposito**. Il conto deposito non appare come voce separata nella Home: lo trovi nella schermata **I miei conti**.
- Sotto il totale si vedono i valori distinti **conto corrente / carta di debito** e **contanti**, mascherati all'apertura insieme al totale. Il pulsante a forma di occhio agisce su tutti gli importi. Nel bilancio condiviso questi due saldi personali non compaiono.
- Il parametro CloudFormation `UserPoolMfaMode` vale `OFF` di default e rimuove `EnabledMfas` dal pool Cognito. L'accesso diventa username/password senza TOTP, restano invariati il flusso OAuth PKCE, la password policy di 14 caratteri e i controlli JWT delle API.
- La scelta è **globale per tutto il pool**, compresi gli amministratori. Disattivando MFA si riduce sostanzialmente la protezione contro furto di password e phishing.

## Deploy

```bash
unzip moneta-2.3.3-home-accesso.zip
cd moneta-2.2-completa
python3 scripts/security_check.py
python3 -m unittest discover -s tests -q
aws cloudformation validate-template --region eu-south-1 --template-body file://cloudformation/backend.yml
bash scripts/deploy.sh
```

MFA viene disabilitata **solo dopo** il deploy dello stack backend; non occorre modificare i record DynamoDB o ricreare utenti Cognito.

Per ripristinare in futuro TOTP obbligatorio (su **tutti** gli utenti):

```bash
MONETA_MFA_MODE=ON bash scripts/deploy.sh
```

La riattivazione può richiedere la configurazione dell'autenticatore per utenti che non ne hanno uno registrato.

## Collaudo
1. Apri Panoramica sul bilancio personale. Verifica che il totale non cambi rispetto alla 2.3.2.
2. Verifica la somma di banca+contanti+deposito in I miei conti e il dettaglio banca/contanti della Home.
3. Ricarica la pagina: tutti i saldi devono risultare nascosti. Premi l'occhio, verifica che sia coerente anche nella schermata I miei conti.
4. Seleziona un bilancio familiare: i dettagli dei conti personali devono sparire.
5. Esci da Moneta e accedi con un account normale e uno Admin: se la configurazione è OFF, Cognito non deve chiedere il codice TOTP. Verifica la gestione utenti con un account Admin.
6. Nessuna modifica a DynamoDB, nessun nuovo servizio AWS; CloudFormation deve aggiornare **lo stesso User Pool**.
