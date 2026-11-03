# Moneta 2.3.6 — Filtri movimenti e categorie

## Riepilogo

Aggiornamento non distruttivo basato su Moneta 2.3.5. Aggiunge alla sezione **Movimenti** filtri combinabili e due categorie di uscita: **Investimenti** e **Risparmio**. Mantiene le ottimizzazioni delle API della 2.3.5, i saldi separati e la logica delle carte di credito.

## Filtri disponibili

- Periodo: mese selezionato (frecce avanti/indietro), mese precedente, ultimi 90 giorni, anno in corso, tutto lo storico, **intervallo personalizzato Dal/Al** (anche con un solo estremo).
- Categoria: tutte le categorie correnti e quelle storiche presenti nel bilancio selezionato.
- Tipo: entrata / uscita / tutte.
- Ricerca in descrizione, nota e categoria, senza distinzione tra maiuscole e minuscole.
- Pannello **Altri filtri**: conto corrente, contanti o carta di credito; nei fondi condivisi, origine fondo/personale; operazioni ricorrenti, non ricorrenti o stornate; importi minimo e massimo.
- Resoconto immediato del numero di movimenti e delle entrate e uscite corrispondenti ai filtri, escludendo trasferimenti e storni dai totali economici come nel riepilogo esistente.
- Pulsante **Azzera filtri**. Il periodo iniziale rimane il mese corrente.

La ricerca è **in memoria nel browser** sullo storico già restituito da `GET /state`. Non vengono effettuate nuove chiamate API per cambiare filtro. Se l'archivio supera il limite esistente di 10.000 record, sarà in futuro necessaria una paginazione dedicata.

## Categorie

`Investimenti` e `Risparmio` sono disponibili come **categorie di uscita**, anche in creazione, modifica, ricorrenze, fondi comuni e budget. Non sono nuovi conti né funzionalità d'investimento automatico. La riclassificazione non cambia il saldo del conto.

**Nota contabile importante:** un bonifico dal conto corrente a un conto deposito personale è un **trasferimento**, non una spesa `Risparmio`. Registrarlo come spesa conterebbe un'uscita nei report e non aumenterebbe il deposito. La categoria `Investimenti` registra un'uscita, ma non crea una posizione finanziaria: la relativa gestione rimane in pausa.

## Compatibilità e privacy

Nessuna migrazione DynamoDB, nessuna nuova rotta API, nessuna nuova risorsa AWS. Isolamento dei bilanci personali e condivisi invariato. Nessun dato finanziario salvato nella cache pubblica della PWA. Cache statici aggiornata a `moneta-static-v2.3.6`.

## Test locali

```bash
python3 scripts/sync_lambda.py
python3 -m unittest discover -s tests -q
node tests/verify_filters.cjs
node tests/verify_performance.cjs
node tests/verify_429.cjs
python3 scripts/security_check.py
# Opzionale, richiede Chromium e Playwright Python:
python3 scripts/check_movements_filters_ui.py
```

## Deploy da AWS CloudShell

1. Effettuare un backup della tabella DynamoDB e conservare lo ZIP 2.3.5 per rollback.
2. Caricare ed estrarre `moneta-2.3.6-filtri-categorie.zip`.
3. Eseguire:

```bash
unzip moneta-2.3.6-filtri-categorie.zip
cd moneta-2.2-completa
python3 scripts/security_check.py
python3 scripts/sync_lambda.py
python3 -m unittest discover -s tests -q
bash scripts/deploy.sh
```

4. Chiudere/riaprire la PWA sullo smartphone per ricaricare JS e CSS.
5. Provare filtri combinati su Movimenti, una nuova categoria e una riclassificazione. Verificare saldi e storico dopo il salvataggio.

> I test locali non sostituiscono il collaudo effettivo sull'account AWS. Non modificare risorse manualmente fuori dagli stack CloudFormation senza verificare le conseguenze per il rollback.
