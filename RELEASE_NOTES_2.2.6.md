# Moneta 2.2.6 — Privacy del saldo

- Il saldo totale della dashboard e quello del Fondo comune sono mascherati di default con `••••••`.
- Pulsante con icona dell'occhio accanto al saldo; un tocco mostra/nasconde entrambi i saldi.
- Il saldo ritorna nascosto alla successiva apertura/ricarica. La preferenza non viene salvata nel browser.
- Nessuna modifica Lambda, Cognito, DynamoDB o CloudFormation. Non è una misura di sicurezza contro l'accesso ai dati: sono solo oscurati visivamente.
- Le altre cifre (indicatori mensili, grafici, movimenti, Insights) restano visibili. Il mascheramento non equivale a una modalità privacy globale.

## Installazione

Usare il normale `bash scripts/deploy.sh` da CloudShell. Se la PWA sembra invariata, chiuderla completamente e riaprirla per far attivare la nuova cache `moneta-static-v2.2.6`.
