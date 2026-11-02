# Moneta 2.3.1 — Hotfix carta di credito e layout smartphone

## Errore risolto

Il `GET /accounts` poteva sollevare `TypeError: 'decimal.Decimal' object cannot be interpreted as an integer` in `calendar.monthrange` subito dopo l’inserimento di una spesa su carta di credito. Il motivo è che boto3 deserializza i campi numerici DynamoDB come `decimal.Decimal`, mentre le date del calendario richiedono `int` Python.

Ora `user_accounts` normalizza e valida **all’ingresso dal database** i campi `openingCents`, `closingDay`, `chargeDay`, `chargeMonthOffset`. Il calcolo dei cicli e la lettura degli account lavorano con interi; il formato salvato in DynamoDB resta invariato. Non serve alcuna migrazione e il saldo/spese esistenti non vengono modificati.

**Attenzione:** l’errore registrato nel log riguarda la **lettura GET /accounts**, non necessariamente l’inserimento della spesa. Se hai salvato il movimento prima di ricevere l’errore, è possibile che sia già presente in tabella. **Prima di riprovare a registrarlo, verifica lo storico** per non creare un duplicato.

## Mobile

- Corretta la barra del filtro mensile in **Movimenti** che a 320 px allargava il documento a 354 px.
- Aggiunti `min-width: 0` e regole di wrapping a riquadri, menu e campi che potevano forzare overflow.
- Adattate le righe di conto e carta, controlli del calendario, pulsanti dei dialog e la navigazione inferiore agli schermi stretti.
- Tabelle storiche continuano a scorrere orizzontalmente **all'interno** dei propri riquadri, senza trascinare tutta la pagina.
- Incrementata la versione della cache service worker a `moneta-static-v2.3.1`.

## Verifiche e limiti

- Test Python (inclusi nuovi casi per dati `Decimal` dal database).
- Verifica statica security gate, sintassi JS e Bash.
- Verifica grafica responsive con Chromium **in ambiente di simulazione**, su larghezze 320, 360, 375, 390 e 430 px. Non si tratta di un test sul tuo smartphone reale né di un collaudo end-to-end AWS.
- Nessuna modifica a CloudFormation, nuovi servizi AWS o API pubbliche.

## Installazione

1. Esegui un backup dei dati prima di aggiornare un'applicazione finanziaria.
2. Carica `moneta-2.3.1-fix-carta-mobile.zip` su AWS CloudShell, estrailo e entra nella cartella.
3. Esegui test, verifica template e deploy:

```bash
unzip moneta-2.3.1-fix-carta-mobile.zip
cd moneta-2.2-completa
python3 scripts/security_check.py
python3 scripts/sync_lambda.py
python3 -m unittest discover -s tests -q
aws cloudformation validate-template --region eu-south-1 --template-body file://cloudformation/backend.yml
bash scripts/deploy.sh
```

4. Sullo smartphone, chiudi e riapri Moneta (PWA) o aggiorna la pagina per acquisire le nuove risorse statiche.
5. Apri **I miei conti** e **Movimenti**, verifica una spesa su carta già registrata, poi testa una nuova registrazione con importo di prova e controlla saldo bancario, debito carta e calendario.

Se un problema visivo resta su una pagina specifica, acquisisci uno screenshot con la parte che esce dai bordi: la versione 2.3.1 corregge i difetti riproducibili dagli stili, senza poter garantire ogni combinazione di dispositivo e dati reali.
