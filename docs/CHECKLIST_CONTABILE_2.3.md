# Moneta 2.3 — Verifica operativa dopo il deploy

## Scenario verificabile (utente di prova)

1. Apri `Il mio bilancio` → `I miei conti`. Imposta saldo iniziale Banca **2.000 €**, Contanti **150 €**. Non inserire contemporaneamente un movimento di entrata di 2.000 €, perché duplicheresti il saldo.
2. Verifica che i saldi principali siano mascherati. Usa l'icona per mostrarli: banca 2.000 €, contanti 150 €, disponibilità 2.150 €.
3. Registra **Prelievo 100 €**: banca 1.900 €, contanti 250 €, disponibilità 2.150 €, spese del mese invariate.
4. Registra **Alimentari 30 € · Contanti**: banca 1.900 €, contanti 220 €, spesa reale 30 €.
5. Configura una carta a saldo con **chiusura 25** e **addebito 10 del mese successivo**, modalità **con conferma**.
6. Registra **Alimentari 80 € · Carta** con data prima del giorno 25: banca invariata, debito carta 80 €, liquidità netta scende di 80 €. La spesa del mese aumenta di 80 €.
7. Verifica in `Scadenze` il prossimo addebito previsto del ciclo. Nella sezione `I miei conti` verifica importo e data.
8. Solo dopo l'effettivo addebito bancario, premi **Conferma addebito**: banca scende di 80 €, debito carta si riduce di 80 € e le spese del mese **non aumentano**.
9. Verifica che una seconda conferma dello stesso ciclo venga rifiutata; verifica anche che spese collegate a un ciclo già saldato non si possano più modificare/eliminare.
10. Registra un nuovo acquisto dopo il giorno 25: deve appartenere al ciclo successivo.
11. Ripeti con un secondo utente Cognito: non deve vedere contanti, carte o trasferimenti del primo.
12. Verifica le ricorrenze preesistenti, il fondo condiviso, gestione utenti admin e la dashboard AWS.
13. Su smartphone prova inserimento rapido, modale movimento, menu I miei conti e conferma estratti. La verifica visuale è necessaria dopo il deploy.

## Recovery

Se i saldi sembrano duplicati: controlla l'impostazione saldo iniziale, i vecchi movimenti di apertura e gli eventuali asset Contanti esterni al registro. Non eliminare la tabella DynamoDB. Il saldo iniziale non ha effetti sulla statistica delle entrate e può essere corretto dal menu I miei conti.

Se lo scheduler automatico fallisce: passa temporaneamente alla modalità con conferma manuale e verifica i log Lambda già in uso. Non forzare manualmente la creazione del movimento bancario come spesa: usa esclusivamente **Conferma addebito**, altrimenti la spesa verrà conteggiata due volte.
