# Moneta 2.3.0 — Conti, contanti e carte di credito

Base: Moneta 2.2.7.1, Security Hardening; aggiornamento senza ricreare tabella, Cognito o spazi condivisi.

## Novità

- **I miei conti**: saldo separato per Conto corrente e Contanti, entrambi privati. Saldo iniziale configurabile (non è un'entrata statistica).
- **Nuovo movimento** e inserimento rapido: conto di pagamento (Banca, Contanti oppure carta a saldo). Le entrate non possono avere come destinazione una carta di credito.
- **Prelievi/versamenti**: trasferimenti banca ↔ contanti, senza spesa/entrata; storno tracciato e non cancellazione silenziosa.
- **Carte di credito a saldo differito**: una o più carte, chiusura estratto conto, giorno di addebito, stesso mese oppure mese successivo, modalità con conferma o automatica.
- **Estratti conto** raggruppati per ciclo; addebito sul conto corrente senza nuovo movimento di consumo. Una volta saldato il ciclo, le spese che ne fanno parte non sono più modificabili né eliminabili.
- **Calendario**: gli addebiti previsti delle carte sono mostrati come promemoria e non conteggiati tra le spese due volte.
- **Ricorrenze e scadenze**: il conto scelto viene mantenuto per le registrazioni future e al momento della conferma della scadenza. Le regole legacy restano sul conto corrente di default.
- **Panoramica**: saldo disponibile = banca + contanti; saldo al netto del debito carta visibile separatamente. Valori mascherati finché non viene premuto l'occhio.
- **Insights**: liquidità netta include banca, contanti e debito carta.

## Compatibilità dei dati

- Nessuna migrazione distruttiva: i vecchi `TX#` senza accountId vengono attribuiti implicitamente a `bank`.
- Le regole ricorrenti e le scadenze già esistenti senza accountId restano compatibili.
- Contanti iniziali = 0 fino alla configurazione. Non inserire due volte saldi di partenza già registrati in precedenti movimenti.
- I movimenti dei bilanci condivisi continuano a usare le regole di fondi/rimborsi esistenti; le nuove scelte dei conti personali sono per ora disponibili nel *bilancio personale*.
- Se in Moneta Insights avevi inserito contanti come attività `cash_outside_ledger`, valuta la rimozione/rettifica di quella attività per non contarli di nuovo dopo aver valorizzato il conto Contanti.

## Funzionamento della carta

- Una spesa da 80 € con carta riduce la liquidità netta e compare nelle statistiche del giorno della spesa, ma non riduce subito il conto corrente.
- Alla conferma dell'estratto conto da 80 €, Moneta riduce di 80 € il conto corrente e azzera il debito relativo, senza creare una seconda spesa.
- Per "Automatico" il job giornaliero già in AWS applica gli addebiti dalla data configurata in avanti. Non effettua operazioni bancarie reali: registra soltanto le operazioni in Moneta.
- La modalità iniziale è **con conferma manuale**: Moneta non presume che la banca abbia già effettuato l'addebito.
- Giorni 29-31 vengono adattati all'ultimo giorno disponibile del mese. Festività e slittamenti bancari non vengono previsti automaticamente.
- Dopo la prima spesa su una carta, i giorni del suo ciclo sono bloccati: per cambiare calendario occorre aggiungere una nuova carta.

## API JWT aggiunte (tutte per utente)

- `GET /accounts`
- `POST /accounts`
- `PUT /accounts/{id}`
- `POST /transfers`
- `POST /transfers/{id}/reverse`
- `POST /cards/{id}/cycles/{cycle}/settle`

## Limiti attuali

- Non esiste integrazione bancaria né conferma effettiva del pagamento presso la banca.
- Solo carte di credito **a saldo**; niente revolving, rateizzazioni o contabilizzazione specifica di cashback/storni del circuito.
- I rimborsi di spese già registrate con carta richiedono una futura procedura dedicata: non simularli con un accredito manuale su carta.
- Non esistono trasferimenti diretti di liquidità fra utenti.
- I prezzi degli investimenti sono ancora manuali, come concordato.
- Nessun browser end-to-end è stato collaudato nell'ambiente di preparazione; API e UI vanno verificati dopo il deploy.

## AWS e costi

Non vengono introdotti nuovi servizi AWS, nuove istanze o nuove tabelle. Si riutilizzano CloudFront, S3, API Gateway, Lambda, DynamoDB e lo scheduler già esistente. Il template CloudFormation aggiunge sei rotte JWT alla HTTP API e mantiene ruoli amministrativi separati. L'elaborazione giornaliera delle carte può aumentare leggermente le letture della tabella DynamoDB e le invocazioni Lambda: controllare il costo reale dal menu Costi AWS.

## Deploy

Il file ZIP si estrae nella cartella `moneta-2.2-completa` per mantenere compatibili gli script esistenti:

```bash
unzip moneta-2.3.0-conti-carte-contanti.zip
cd moneta-2.2-completa
python3 scripts/security_check.py
python3 scripts/sync_lambda.py
python3 -m unittest discover -s tests -q
aws cloudformation validate-template --region eu-south-1 --template-body file://cloudformation/backend.yml
bash scripts/deploy.sh
```

**Prima del deploy:** salva una copia del database e del precedente pacchetto Moneta 2.2.7.1. Un'esportazione della tabella contiene dati finanziari e deve rimanere privata. Durante l'aggiornamento non registrare nuove operazioni nell'app. Dopo il deploy controlla i saldi storici e le rotte in CloudShell. Lo scheduler in modalità automatica non si attiva su nuove carte finché non selezioni esplicitamente quella modalità.
