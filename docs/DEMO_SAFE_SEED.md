# Demo Moneta: utilizzo sicuro

1. Crea `demo-moneta@gmail.com` dal menu amministrativo Gestione utenti. Abilita l'MFA dell'account quando lo utilizzi e non condividere credenziali del tuo account principale.
2. Esegui `python3 scripts/seed_demo.py` in CloudShell. L'anteprima **non scrive** alcun record.
3. Controlla che l'utente di destinazione e il nome tabella siano corretti.
4. Per inserire il dataset fittizio in modo atomico esegui:

   ```bash
   python3 scripts/seed_demo.py --execute --confirm-email demo-moneta@gmail.com
   ```

5. Se l'archivio dell'account demo non è vuoto lo script rifiuta l'operazione. Non modifica mai le partizioni degli altri utenti, non crea il Cognito user e non rimuove dati preesistenti.
6. Il dataset contiene movimenti esemplificativi dei tre mesi completi precedenti, saldo deposito, prelievo, trasferimento a deposito, spesa con carta, budget, ricorrenza e una scadenza futura.
7. La demo serve per esposizioni e test. Evita di usarla per monitorare le tue finanze reali.
