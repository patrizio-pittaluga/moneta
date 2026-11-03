# Moneta 2.3.5 — Performance Optimization

**Base:** Moneta 2.3.4. **Ambito:** frontend (`app.js` e cache PWA), test e documentazione. **Nessuna modifica a DynamoDB, Lambda, Cognito, API Gateway, CloudFormation o IAM.**

## Problema risolto

In 2.3.4 molte scritture chiamavano `mutate() → reload()`, che rileggeva `/state`, `/favorites`, `/accounts` e `/insights`, anche per aggiornare semplicemente una categoria. Su mobile questo moltiplicava le chiamate API, aumentando latenza e probabilità di ricevere HTTP 429.

## Ottimizzazioni

- **Modifica di sola categoria, note o descrizione:** la risposta `PUT /transactions/{id}` viene incorporata nello storico già aperto senza altre richieste. **1 chiamata totale**, contro le 5 tipiche del flusso precedente.
- **Modifica di importo, conto, data o tipo:** `PUT` + `GET /accounts`, usando il saldo calcolato da DynamoDB. **2 chiamate** dalla pagina Movimenti; se si sta mostrando la Home o Insights si aggiunge l'aggiornamento degli indicatori.
- **Nuovo movimento singolo (anche Quick Add):** `POST /transactions` + `GET /accounts` nel bilancio personale; i preferiti, se richiesti, aggiungono la propria scrittura. Una scrittura non viene ripetuta automaticamente in caso di errore del refresh.
- **Eliminazione movimento:** `DELETE` + `GET /accounts`, evitando la rilettura di preferiti e Insights se non mostrati.
- **Giroconti, carte di credito, saldo iniziale:** scrittura + sola lettura `GET /accounts` (non vengono ricaricati movimenti e preferiti).
- **Preferiti, budget, ricorrenze e scadenze da programmare:** UI aggiornata dalla risposta della singola API (o rimozione locale dopo `204`), senza reload generale. I budget invalidano gli Insights.
- **Ricorrenza creata da Nuovo movimento con prima rata:** dopo la `POST`, rilegge appositamente `/state` e `/accounts`, perché il backend genera atomicamente anche il primo movimento. È un'eccezione intenzionale.
- **Insights:** caricati quando vengono mostrati e riutilizzati fino alla successiva modifica pertinente. Nessuna cache persistente di dati finanziari nel service worker o in localStorage.
- **Refresh completo:** resta disponibile nel pulsante **Aggiorna**, al login e al cambio di bilancio, anche in caso di risultati non sincronizzati.
- **Cache PWA:** versione aggiornata a `moneta-static-v2.3.5`.

Il risparmio di chiamate si riferisce a flussi specifici, **non** alla fattura AWS complessiva. Alcune azioni più articolate continuano a fare un refresh completo per preservare coerenza finanziaria. Non è stata introdotta la rotta `/dashboard` di aggregazione, per evitare modifiche al backend non necessarie in questo rilascio.

## Controlli e compatibilità

Nessuna migrazione dati. Le regole contabili sono immutate, incluse carta di credito, trasferimenti, deposito, fondi comuni e ricorrenze. L'ottimizzazione non cambia il limite di throttling HTTP API (8 richieste/s, burst 15).

Test locali:

```bash
python3 -m unittest discover -s tests -q
node tests/verify_performance.cjs   # facoltativo: richiede Node.js
node tests/verify_429.cjs           # facoltativo: richiede Node.js
python3 scripts/security_check.py
python3 scripts/mobile_layout_check.py  # facoltativo: richiede Playwright/Chromium
```

## Deploy CloudShell

1. Conserva il precedente pacchetto 2.3.4; per prudenza verifica lo stato degli stack e mantieni il normale backup prima del deploy.
2. Carica lo ZIP e decomprimilo in una directory **nuova**.
3. Esegui:

```bash
unzip moneta-2.3.5-performance.zip
cd moneta-2.2-completa
python3 -m unittest discover -s tests -q
python3 scripts/security_check.py
bash scripts/deploy.sh
```

4. Se usi Moneta come PWA, chiudila e riaprila. Se necessario aggiorna una volta la pagina perché il service worker adotti la nuova cache.

## Collaudo manuale consigliato

1. Apri **Movimenti**, modifica solo la categoria di una spesa già esistente: il movimento deve aggiornarsi e il saldo deve rimanere invariato.
2. Modifica l'importo di una spesa di test (per esempio da 5 € a 6 €): il saldo del conto usato deve diminuire di **1 €** e non di 6 € aggiuntivi.
3. Apri Home / Insights dopo aver riclassificato una spesa: l'analisi deve riflettere la nuova categoria.
4. Esegui un trasferimento di prova banca ↔ contanti e verifica che non appaia come spesa.
5. Registra una piccola spesa con carta di credito: il debito deve aumentare senza sottrazione immediata dal conto bancario.
6. Verifica il comportamento su smartphone e l'assenza di 429 quando riclassifichi più movimenti in sequenza.

**Importante:** se un'operazione viene salvata dal server ma la sincronizzazione successiva fallisce, Moneta avvisa di premere **Aggiorna** prima di ripeterla. Evita di reinserire una spesa senza controllare lo storico.

## Costi

Nessun nuovo servizio AWS, nessun abbonamento o risorsa infrastrutturale aggiunta. Il minor numero di letture API e DynamoDB può ridurre consumi a parità d'uso, senza garantire un importo mensile preciso. Il limite target AWS rimane quello dell'account attuale.
