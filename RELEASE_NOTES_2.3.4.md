# Moneta 2.3.4 — 429, Calcetto e saldo operativo (2 novembre 2026)

## Problema 429

**Diagnosi statica:** `cloudformation/backend.yml`, risorsa `ApiStage`, era impostata a `ThrottlingRateLimit: 2` e `ThrottlingBurstLimit: 5`. Il client fa richieste in parallelo a `/state`, `/favorites`, `/accounts` durante gli aggiornamenti e poi carica gli Insights. API Gateway può rifiutare con 429 prima di invocare Lambda; l'assenza di errori nella Lambda è quindi coerente con il problema. Non possiamo confermare la causa esclusiva senza leggere i log/metrica dello stage AWS reale.

**Modifiche:**
- `ApiStage`: 8 richieste/secondo e burst 15. I limiti rimangono presenti per contenere richieste accidentali e traffico inatteso.
- Frontend: massimo 2 retry aggiuntivi con backoff e jitter soltanto per GET e PUT, in risposta a HTTP 429. Il `Retry-After` viene rispettato, con attesa massima di 2,5 secondi per tentativo.
- Nessun retry automatico su POST e DELETE, che potrebbero non essere idempotenti.
- Quando il salvataggio va a buon fine ma non il refresh successivo, il messaggio precisa che l'operazione **potrebbe essere già salvata**: non ripetere prima di aver controllato lo storico.

**Costi:** nessun nuovo tipo di servizio AWS, database o risorsa a pagamento. Più richieste accettate possono comunque aumentare i consumi a misura; AWS Budgets è un avviso e non un blocco dei costi.

## Nuova categoria

- `Calcetto` è disponibile tra le categorie **Uscita**, anche quando modifichi movimenti esistenti.
- Aggiunta alle categorie validate dalla Lambda: operazioni, ricorrenze e budget della categoria rimangono compatibili.
- Nessuna modifica ai movimenti storici e nessuna modifica agli importi.

## Home

- Il numero grande mostra **disponibilità operativa = conto corrente + contanti**, escluso conto deposito e debiti ancora da addebitare sulla carta di credito.
- Il dettaglio Banca/Contanti e il totale al netto del debito carta sono coerenti con questa vista, senza deposito.
- Il conto deposito rimane visibile solo in **I miei conti**, dove la liquidità complessiva continua a includerlo.
- I saldi rimangono mascherati per impostazione predefinita.
- Lo spazio condiviso continua a usare il proprio saldo del fondo, senza rivelare saldi personali.

## Installazione

1. Fai un backup DynamoDB e conserva lo ZIP precedente.
2. Estrai il pacchetto ZIP nella CloudShell.
3. Dalla cartella `moneta-2.2-completa` esegui:

```bash
python3 scripts/security_check.py
python3 -m unittest discover -s tests -q
node tests/verify_429.cjs # opzionale, se Node.js è installato
aws cloudformation validate-template --region eu-south-1 --template-body file://cloudformation/backend.yml
bash scripts/deploy.sh
```

4. Chiudi e riapri la PWA (eventualmente esci e rientra) per applicare il nuovo `sw.js` versione 2.3.4.
5. Modifica una spesa usando la categoria `Calcetto`, verifica salvataggio e Home; controlla il saldo del deposito in **I miei conti**.
6. Se i 429 persistono, controlla in AWS API Gateway lo stage `$default` e la metrica `4xx`, oltre alla metrica Lambda `Throttles`: potrebbe essere coinvolto un limite più alto a livello account o una limitazione Lambda.

## Test eseguiti localmente

- 174 test Python passati.
- Test JS di backoff 429 (GET/PUT ritentati, POST/DELETE no) passato.
- `node --check frontend/app.js` passato.
- `security_check.py` senza segnalazioni.
- Chromium responsive: 320, 360, 375, 390, 430, 768 px senza overflow orizzontali, sulle principali schermate simulate.

Non è stato effettuato deploy né verifica sullo stage AWS dell'utente.
