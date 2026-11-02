# Moneta 2.3.2 — Conto deposito, Insights sulle spese e account demo

## Compatibilità

Aggiornamento da Moneta 2.3.1, compatibile con i record personali e familiari esistenti. Nessuna migrazione distruttiva. Nessun nuovo servizio AWS o risorsa CloudFormation. Nessuna sincronizzazione esterna degli investimenti.

## Modifiche

1. **Conto deposito:** conto predefinito distinto da banca e contanti. I trasferimenti sono ammessi tra banca, contanti e deposito; nessuna entrata, spesa, scadenza o ricorrenza può essere registrata direttamente sul deposito. Saldo iniziale impostabile una sola volta con l'attenzione a non duplicare importi. Il deposito entra nella *liquidità totale* e nel patrimonio netto; `spendable` mostra invece la disponibilità di banca più contanti. Il debito della carta resta separato.
2. **Insights sulle spese:** ultime tre mensilità complete, categorie ordinate per importo, media mensile, quota percentuale, risparmio stimato e suggerimenti basati sui dati. Ignorati trasferimenti, storni e operazioni non effettive. Se i mesi osservati sono pochi si evita di presentare diagnosi definitive.
3. **Nuove categorie:** 26 per uscite e 12 per entrate. Le vecchie categorie rimangono accettate. Budget già impostati preservati.
4. **Scadenze e ricorrenze:** una sola voce di menu e un'unica pagina; le rispettive operazioni rimangono indipendenti nel backend.
5. **Home:** prima gli Insights, poi i grafici e più in basso le prossime scadenze.
6. **Investimenti:** nascosta la gestione delle posizioni nel frontend. **I record patrimoniali precedenti non vengono cancellati**, e le API storiche restano per compatibilità. Restano disponibili beni e debiti.
7. **Demo:** `scripts/seed_demo.py` con anteprima iniziale, identificazione dell'utente via pool Cognito, controllo archivio vuoto, conferma esplicita e scrittura atomica condizionale. Non viene eseguito dal deploy.
8. **PWA:** cache degli asset statici aggiornata a `moneta-static-v2.3.2`.

## Installazione

Da CloudShell, dopo avere conservato uno snapshot o un export sicuro dei dati importanti:

```bash
unzip moneta-2.3.2-conti-insights-demo.zip
cd moneta-2.2-completa
python3 scripts/security_check.py
python3 -m unittest discover -s tests -q
bash scripts/deploy.sh
```

Non è necessario eseguire lo script demo per gli altri utenti.

## Popolamento demo: operazione distinta e facoltativa

Prerequisito: creare **prima** l'account `demo-moneta@gmail.com` usando Gestione utenti; l'utente deve essere attivo. Se il suo archivio DynamoDB contiene già dati lo script si interrompe e non modifica niente.

```bash
# Anteprima, solo lettura
python3 scripts/seed_demo.py

# Solo dopo aver controllato account, tabella e quantità di record
python3 scripts/seed_demo.py --execute --confirm-email demo-moneta@gmail.com
```

Il caricamento usa `sub` trovato dal pool Cognito dello stack `moneta-backend` in Milano, mai la mail come PK; genera ~62 righe fittizie tra entrate, spese, scadenze, budget, carta, deposito e trasferimenti. Richiede permessi AWS di lettura Cognito/CloudFormation/DynamoDB e `dynamodb:TransactWriteItems` sulla tabella. Un problema IAM bloccherà l'operazione senza abilitare nuovi servizi.

**Non** eseguirlo per «azzerare» l'account demo già popolato. Non esiste una funzione automatica di cancellazione dei dati per tutelare le informazioni eventualmente già presenti.

## Verifiche post-deploy

- Apri "I miei conti" e configura un saldo iniziale deposito, controlla spendibile e liquidità totale.
- Trasferisci 20 € da banca a deposito: la liquidità totale deve restare identica; il saldo spendibile cala di 20 €.
- Prova ad aprire "Nuovo movimento": il deposito non deve comparire tra i metodi di pagamento.
- Verifica una spesa e una ricorrenza già esistenti, gli addebiti della carta e le date del calendario.
- Apri Insights dopo aver inserito uno o più mesi di movimenti e verifica categorie, storni e suggerimenti.
- Controlla l'account demo separatamente, senza condividere le credenziali del tuo account principale.
- Chiudi e riapri la PWA su telefono per aggiornare la cache.

## Limiti e cautela

- La liquidità include il deposito, ma Moneta **non conosce** eventuali vincoli o tempi di svincolo bancari: non equiparare il deposito a denaro spendibile subito.
- Le analisi sono basate sui dati registrati. Spese non inserite o categorie errate portano a consigli fuorvianti; non sono previsioni garantite.
- Il saldo iniziale del deposito non rappresenta reddito. I trasferimenti non sono spese.
- La demo non è inclusa nella normale migrazione né parte da un dataset reale: evita screenshots con informazioni private e usa solo l'account demo.
- Test AWS reali e collaudo smartphone reale vanno eseguiti dopo il deploy.
