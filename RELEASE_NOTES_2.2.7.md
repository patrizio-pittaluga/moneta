# Moneta 2.2.7 — Security Hardening

**Data:** 16 ottobre 2026  
**Origine:** ZIP `moneta-2.2.6-saldo-privato.zip`  
**Stato:** verificato localmente, **non installato su AWS**.

## Cosa cambia

1. **Separazione dei privilegi:** la Lambda di bilancio `moneta-backend-api` conserva lettura/scrittura DynamoDB, lookup di inviti e job ricorrenti, ma perde `ce:GetCostAndUsage` e le API Cognito di amministrazione.
2. **Gestione utenti isolata:** `moneta-backend-admin-users` usa un ruolo limitato alle azioni Cognito sul solo User Pool Moneta. Non ha accesso a DynamoDB né Cost Explorer.
3. **Costi isolati:** `moneta-backend-costs` può leggere Cost Explorer, verificare gli amministratori e usare `GetItem`/`PutItem` DynamoDB soltanto per la partition key `SYSTEM#AWS_COSTS` (condizione `dynamodb:LeadingKeys`), non i bilanci degli utenti.
4. **Autorizzazione live:** ogni richiesta alle rotte `/admin/users` o `/costs` deve avere un access token Cognito valido e un'appartenenza corrente al gruppo `Admins`, verificata via Cognito. La sola claim `cognito:groups` del JWT non basta.
5. **Header CloudFront:** HSTS (senza includeSubDomains/preload), X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy, Content-Security-Policy incluso `frame-ancestors 'none'`, tramite ResponseHeadersPolicy CloudFront.
6. **Preflight di deploy:** `scripts/security_check.py` (scansione basilare di segreti + invarianti infrastrutturali), validazione template CloudFormation con l'API esistente di AWS, ZIP separati per i tre handler, suite di regression test.
7. **Audit applicativo mirato:** logga l'azione amministrativa, l'ID Cognito dell'attore e un hash abbreviato del destinatario; non logga email o password. Retention CloudWatch 7 giorni invariata.

**Servizi AWS:** rimangono S3, CloudFront, Lambda, API Gateway, Cognito, IAM, DynamoDB, EventBridge Scheduler e CloudWatch già utilizzati. Nessun Security Hub, AWS Config, GuardDuty, Inspector o servizio terzo. Sono aggiunte **due funzioni Lambda**, **due ruoli IAM**, **due log group CloudWatch**, **due integrazioni API** e **una policy CloudFront**. Sono risorse dei servizi già presenti, ma invocazioni e log possono comportare costi marginali variabili. Il budget di 3 USD/mese resta un *avviso*, non un limite di spesa.

**Dati:** nessuna modifica allo schema della tabella, ai movimenti o agli account Cognito esistenti. Nessuna attivazione di PITR o backup ricorrenti.

## Installazione sicura

> Consigliato: backup dei dati **prima** di ogni aggiornamento dell'infrastruttura. I backup manuali DynamoDB possono avere un costo: verifica preventivamente. Il deploy non abilita automaticamente servizi di backup.

1. Carica `moneta-2.2.7-security-hardening.zip` in AWS CloudShell; verifica `aws sts get-caller-identity`.
2. `unzip moneta-2.2.7-security-hardening.zip && cd moneta-2.2-completa`
3. `python3 scripts/security_check.py`
4. `python3 scripts/sync_lambda.py`
5. `python3 -m unittest discover -s tests -q`
6. `bash scripts/deploy.sh`

Il deploy valida entrambi i template CloudFormation **prima** delle modifiche, usa gli stack **esistenti** `moneta-frontend` (us-east-1) e `moneta-backend` (eu-south-1), carica tre ZIP in S3 privato ed esegue CloudFormation con `CAPABILITY_IAM`.

## Checklist dopo il deploy

- [ ] CloudFormation: entrambi gli stack `UPDATE_COMPLETE`, nessuna risorsa da ricreare; User Pool e tabella mantengono l'ID.
- [ ] Login Cognito + MFA; accesso da web e PWA.
- [ ] Account normale: movimenti, fondo comune, Insights, ricorrenze e inviti funzionanti; API `/admin/users` e `/costs` negate.
- [ ] Account Admin: elenco utenti, creazione utente di prova, password temporanea e gestione abilitazione; dashboard costi senza accesso ai dati privati degli altri.
- [ ] Rimozione di un utente test dal gruppo `Admins`: con il vecchio access token, i nuovi endpoint amministrativi devono restituire `403`.
- [ ] CloudFront: `curl -I https://moneta.ilsitodipatrizio.it/` mostra gli header `strict-transport-security`, `x-frame-options`, `x-content-type-options`, `content-security-policy`.
- [ ] Ricorrenza: movimento creato una sola volta per scadenza, nessuna doppia contabilizzazione.
- [ ] Controllare AWS Billing e CloudWatch per errori. Non creare o modificare utenti reali come test.

## Limiti e rischi residui

- La **revoca live** riguarda le **operazioni amministrative e la dashboard costi**. Un access token già emesso può essere ancora valido per gli endpoint finanziari ordinari fino alla scadenza JWT (configurazione attuale 30 minuti), salvo controlli aggiuntivi futuri.
- Il ruolo della Lambda finanziaria mantiene DynamoDB `Scan` perché esegue il job ricorrente per tutti gli account. Un eventuale passo successivo sarebbe separare *anche* il job scheduler con un ruolo dedicato: non incluso in questa release.
- I log CloudWatch continuano a essere conservati per soli **7 giorni**. Nessun audit trail immutabile o sistema di detection aggiuntivo è stato attivato.
- L'assenza di segreti rilevati dallo scanner non copre cronologia Git, variabili d'ambiente su AWS, né tutti i formati possibili di token.
- Non è stato effettuato alcun penetration test né test AWS in un account reale. L'API `validate-template` eseguita dal deploy è una validazione sintattica, non un test dell'autorizzazione IAM runtime.
- Per rollback, conserva `moneta-2.2.6-saldo-privato.zip` e verifica prima l'impatto di ripristinare i privilegi precedenti. Non eliminare stack, User Pool o tabella.
