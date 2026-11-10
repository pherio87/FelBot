# FelBot — assistente documentale su Formazione e Lavoro

> Strumento non ufficiale, non collegato alla Regione Emilia-Romagna.
>
> Il repository ha due parti:
> - **`worker/`** — il servizio online su Cloudflare (chat, ricerca, verifica
>   delle risposte, pagina `/bot`). Istruzioni in `worker/README.md`.
> - **cartella principale** — il crawler e l'indicizzazione, in Python. Il
>   resto di questo documento descrive questa parte e la versione originale
>   su VPS, da cui deriva la logica del Worker.

# Assistente documentale — Formazione e Lavoro, Regione Emilia-Romagna

Un assistente che risponde **soltanto** con quanto la Regione Emilia-Romagna ha
effettivamente pubblicato. Non ragiona per analogia, non colma le lacune, non
ricorda vagamente cosa dice di solito una normativa regionale: legge gli atti
indicizzati, cita l'estratto da cui ha preso ogni affermazione, e verifica che
i numeri che scrive stiano davvero in quell'estratto. Se la risposta non c'è
nei documenti, lo dice.

> Questa è la versione 1.2. La 1.1 era uscita da una revisione che aveva trovato
> difetti gravi nella 1.0; una seconda revisione (novembre 2026) ha mostrato che
> alcune garanzie dichiarate nella 1.1 non reggevano alla prova: la verifica dei
> valori si aggirava in cinque modi, il crawler poteva ancora ritirare documenti
> pubblicati, e la rivalidazione mensile non avveniva mai. Il §10 elenca cosa è
> cambiato e perché: ogni voce è una trappola in cui è facile ricadere.

---

## 1. Come fa a non inventare

L'obiettivo — *non immagina, non inventa, non allucina* — non si ottiene
chiedendo per favore al modello di non inventare. Si ottiene togliendogli la
possibilità di farlo e poi **controllando meccanicamente che non l'abbia
fatta**. Sono cinque livelli, e sono anche i cinque punti da guardare se un
giorno qualcosa andrà storto.

**1. Il modello non ha accesso alla propria memoria.** Il prompt gli vieta di
usare quello che crede di sapere sulla normativa italiana. L'unico materiale
ammesso è il fascicolo di estratti consegnato ad ogni domanda. Temperatura a
zero: nella documentazione amministrativa la creatività è un difetto.

**2. Ogni affermazione porta un riferimento.** La risposta esce nella forma «gli
organismi accreditati trasmettono la comunicazione entro trenta giorni [3]»,
dove `[3]` è un estratto reale, cliccabile, con pagina e link al PDF originale.

**3. Dopo la generazione il codice verifica i VALORI, non solo le parentesi.**
È il cuore del sistema, ed è ciò che distingue questa versione dalla precedente.
`answer.verify()` estrae ogni valore della risposta — importi, percentuali,
scadenze, date intere, durate, numeri d'atto, in cifre o in lettere («trenta
giorni»), anche a una sola cifra quando ha un'unità («5 giorni», «3%») — e
controlla che compaia **nell'estratto citato da quella frase**, non in uno
qualunque. Un valore presente in un altro estratto produce un avviso specifico
di attribuzione errata; un valore che compare solo nella domanda dell'utente
non conta come confermato. La logica sta in `valori.py`. Un contributo da «250.000 euro»
attribuito a un estratto che dice 50.000 viene segnalato e la risposta
declassata, **anche se la citazione è formalmente valida**. Era il buco più
costoso: l'allucinazione tipica non è citare un estratto inesistente, è citarne
uno vero dicendo un numero falso.

Sullo stesso piano: un riferimento inventato (`[7]` quando gli estratti sono
tre) resta **visibile** nel testo come `[RIFERIMENTO INESISTENTE]` e forza
l'affidabilità a *bassa*. Prima veniva cancellato, il che lasciava
l'affermazione infondata sul posto priva di ogni marcatore, e — riducendo il
conteggio delle frasi non citate — poteva perfino far salire l'affidabilità ad
*alta*. Cancellare la prova non è una correzione.

**4. Se il recupero è debole, non si risponde affatto.** Prima di interpellare
il modello il sistema valuta la pertinenza degli estratti
(`Retriever.is_confident`). Le condizioni sono **congiuntive**: serve almeno un
frammento sopra la soglia di similarità *e* un quorum di due, oppure un
riferimento d'atto riscontrato con numero e anno in prossimità. Nella versione
precedente bastava «tre dei primi cinque risultati hanno un rango lessicale» —
che è vero praticamente sempre — e il sistema non si asteneva mai.

**5. Stato dei documenti calcolato, non chiesto al modello.** Un bando con
termine scaduto, un documento non pubblicato, un testo ottenuto via OCR: lo
stato è scritto in chiaro nel fascicolo e produce un avviso **generato dal
codice** a partire dai metadati. Il sistema non afferma mai che un atto è
vigente o abrogato, perché non può saperlo; quando una fonte citata ha più di
due anni, la riga sulla verifica della vigenza viene aggiunta in coda alla
risposta dal codice, non dalla buona volontà del modello.

Il pannello laterale mostra gli estratti usati con data, stato e link
all'originale, e distingue quelli citati da quelli solo recuperati. La verifica
resta a un clic, com'è giusto per un testo che finirà in un atto.

---

## 2. Come raccoglie i documenti

Il portale gira su **Plone 6** ed espone la REST API in lettura anonima: invece
di seguire i link a tentoni si interroga il catalogo del sito.

| | |
|---|---|
| Oggetti pubblicati | **21.320** |
| di cui allegati (PDF, DOC, ODT…) | **8.843** |
| Endpoint | `/++api++/@search` |
| Paginazione | `b_start`, verificata fino in fondo al catalogo |
| Metadato chiave | `modified` per ogni oggetto |

Due assunzioni "ovvie" su questa API sono state **verificate false sul campo**,
e dettano l'implementazione. Chi metterà mano a `sources/plone.py` deve
conoscerle, perché entrambe fallivano in silenzio:

- **`metadata_fields` ripetuto non è onorato**: viene letto solo il primo
  valore. Chiedere l'elenco dei campi necessari restituiva risposte prive di
  `created` e `modified` — cioè prive esattamente dei due campi su cui si
  reggono paginazione e aggiornamento incrementale. Si usa `_all`.
- **La paginazione "keyset" su `created` perde documenti**: il valore torna in
  un fuso e viene reinterpretato in un altro, e il DateIndex di ZCatalog ha
  risoluzione al minuto con ordine arbitrario fra pari. Misurate perdite di
  decine di documenti su un solo confine di pagina. Si usa `b_start`.

Da qui la terza regola, la più importante: **un'enumerazione incompleta non
deve mai essere indistinguibile da una completa.** Il numero di oggetti visti
si confronta sempre con `items_total` dichiarato dal portale; se non torna, si
solleva un errore e la fase di rimozione non parte. Senza questo, un 502 a metà
scansione faceva marcare come «non più pubblicati» diciassettemila documenti
ancora presenti e la fase successiva ne cancellava i frammenti. Come ulteriore
freno, se in un giro sparisce più del 5% dei documenti non si cancella nulla e
lo si scrive nei log.

L'aggiornamento incrementale chiede al portale cosa è cambiato dall'ultimo
giro. Il punto di ripartenza avanza **solo** se il giro è andato a buon fine:
avanzarlo dopo una scansione interrotta dichiarava coperta una finestra
temporale che nessuno aveva guardato. In più, ogni notte una quota di documenti
viene rimessa in coda e riverificata con richiesta condizionale, in modo che
l'intero indice ruoti in 30 giorni (`FLER_REVALIDATE_DAYS`), perché il portale
sostituisce allegati senza toccare la data di modifica.

La domenica lo scheduler esegue una **scansione completa**: è l'unica che può
accorgersi dei documenti ritirati. Un documento non visto in enumerazione non
viene ritirato sulla fiducia: lo si interroga singolarmente e lo si toglie
dall'indice solo se il portale risponde 404/410 (o 401/403, non più pubblico).

### Formati gestiti

PDF (con numero di pagina conservato e ordine di lettura corretto anche su due
colonne), DOCX, DOC binario, ODT, ODS, ODP, XLSX, XLS, PPTX, RTF, CSV, TXT,
HTML e **archivi ZIP**, di cui si estraggono ricorsivamente i membri
riconosciuti: la modulistica allegata ai bandi arriva spesso così, e prima
veniva scartata in silenzio.

I PDF scansionati passano per l'OCR in italiano, deciso **pagina per pagina**:
sulla media del documento, una delibera di 200 pagine con 50 di allegato
scansionato non avrebbe mai attivato l'OCR e quelle 50 pagine si sarebbero
perse. Gli estratti che ne derivano portano il flag `via_ocr`, che arriva fino
al fascicolo e produce un avviso automatico.

### Educazione verso il server della Regione

È un servizio pubblico, non un bersaglio. Il crawler si identifica con nome e
pagina di contatto, rispetta `robots.txt` (un `robots.txt` che risponde 5xx o va
in timeout è trattato come **divieto**, non come via libera), rispetta
`Crawl-delay` se presente, si limita a 1,5 richieste al secondo su 3
connessioni, usa richieste condizionali e rallenta da sé su un `429`. Anche
l'enumerazione passa dal controllo di `robots.txt`. Questi valori stanno in
`.env` e conviene non alzarli.

---

## 3. Come cerca

Recupero **ibrido**, e il motivo è concreto: metà delle domande di un CFP
contengono un identificativo esatto — «DGR 1298/2015», «PG.2017.333374», «L.R.
17/2005», «allegato 3» — che gli embedding trattano come rumore numerico e
sbagliano; il BM25 li trova al primo colpo. Viceversa «chi può firmare gli
attestati di un corso IeFP» è concettuale, e lì è il BM25 ad annaspare.

Tre tarature non ovvie, tutte misurate:

- **Un riferimento d'atto è una coppia (numero, anno), non un numero sciolto.**
  Trattare ogni numero di tre cifre come estremo d'atto faceva sì che
  «aggiornati al 2024» cercasse `2024`, che compare nel **16,8%** dei
  frammenti, e che quei frammenti arrivassero in cima etichettati come
  «riferimento esatto» — un'etichetta di precisione su rumore.
- **I due numeri di un atto si cercano in prossimità** (`NEAR`), non in AND
  sparso: `"17" AND "2005"` selezionava il 3,8% del corpus, perché «17» è anche
  un articolo, un comma, un giorno. E più atti citati insieme si uniscono in
  OR, non in AND, che azzerava il risultato.
- **`RRF_K` vale 20, non 60.** Su un pool di 80 candidati la costante da
  letteratura appiattisce tutto: fra il primo e l'ottantesimo correvano meno di
  2,5 volte, e un risultato presente in entrambe le liste a qualunque rango
  batteva il primo assoluto di una sola lista — cioè proprio il match esatto
  sul numero di protocollo.

I frammenti sono tagliati sui confini logici dell'atto (`Art. 3`, `Allegato B`,
`punto 4.2`), con un tetto massimo perché un allegato senza righe vuote
produceva un frammento unico da 14.000 caratteri. Le rubriche d'articolo non
vengono più scartate: stavano sotto la soglia minima e venivano buttate, ed
erano proprio il testo che rende trovabile «quante ore di stage». Ogni
frammento porta in testa titolo, sezione e data, **conteggiati nel budget** per
non riempire il 20% di ogni vettore con la stessa intestazione.

Il contesto ammette fino a 3 frammenti per documento, ma se un solo atto domina
il recupero — un manuale di rendicontazione che contiene tutta la risposta — gli
si concede metà dello spazio invece di diluire con dieci documenti che non
c'entrano. I frammenti scelti vengono estesi ai contigui, perché tre pezzi
sconnessi di una procedura sono peggio di due consecutivi.

---

## 4. Installazione su VPS

Requisiti: Linux con Docker, **2 vCPU e 4 GB di RAM**, **25 GB di disco**. Un
Hetzner CX22 o equivalente, sui 5 €/mese.

```bash
git clone <questo-repo> /opt/fler && cd /opt/fler
cp .env.example .env
nano .env          # password d'accesso e chiavi API: entrambe obbligatorie
chmod 600 .env     # contiene le chiavi: non lasciarlo leggibile a tutti
docker compose build
```

Il servizio **non parte senza `FLER_ACCESS_PASSWORD`**, e non è un capriccio:
espone un indice documentale e consuma una chiave API a consumo. Nella versione
precedente una password vuota significava «aperto a tutta internet» senza alcun
sintomo visibile — l'interfaccia non mostrava nemmeno il riquadro di login.

Prima indicizzazione, in `screen` o `tmux`:

```bash
./scripts/prima_indicizzazione.sh
```

Lo script prova su 200 documenti e si ferma a chiedere conferma: è il momento
di guardare `fler stato` e verificare che l'estrazione funzioni prima di
impegnare molte ore di crawling.

Poi:

```bash
docker compose up -d
```

La chat risponde su `127.0.0.1:8080`. Davanti va un reverse proxy con HTTPS —
`Caddyfile.esempio` ha la configurazione minima, comprese HSTS e il tetto sul
corpo delle richieste. **Non esporre la porta 8080 direttamente su internet.**

### Quanto dura davvero la prima indicizzazione

Non «qualche ora». Con i parametri predefiniti:

| Fase | Stima |
|---|---|
| Enumerazione (214 pagine di API) | ~3 minuti |
| Scaricamento di ~20.700 documenti a 1,5 req/s | **~4 ore**, 4–5 GB di traffico |
| Estrazione, in parallelo allo scaricamento | dipende dall'OCR |
| OCR dei PDF scansionati (~5% degli allegati) | **fino a 10 ore** su 2 vCPU |
| Embedding (~120–150k frammenti) | ~30–60 minuti |

**Totale realistico 12–25 ore.** L'estrazione gira su thread separati e si
sovrappone allo scaricamento, ma l'OCR resta il collo di bottiglia. Il processo
è ripartibile: se cade, `fler ingest` riprende dai documenti rimasti.

### Aggiornamento periodico

Il servizio `scheduler` nel compose parte alle 04:30 ora italiana —
incrementale nei giorni feriali, completa la domenica — (il container
ha `TZ=Europe/Rome`: senza, girerebbe in UTC e partirebbe alle 06:30 legali, in
orario d'ufficio). In alternativa c'è `scripts/aggiornamento.sh` da crontab —
**se si usa quello, commentare il servizio `scheduler`**, altrimenti gli
aggiornamenti sono due. Non si corrompe nulla, perché le ingestioni sono
serializzate da un lock su `/data/ingest.lock`, ma il traffico verso la Regione
raddoppia inutilmente.

### Backup

`scripts/backup.sh` da crontab alle 05:30 tiene sette copie a rotazione
settimanale di catalogo e indice vettoriale; `scripts/ripristino.sh <giorno>` le
rimette al loro posto mettendo da parte i dati attuali. Perdere `/data` non è
irreversibile — si rigenera dal portale — ma costa una giornata di crawling e
qualche euro di embedding, da rifare a riga di comando: esattamente ciò che non
si vuole dover fare di fretta. Lo snapshot del VPS offerto dal provider, circa
1 €/mese, copre il resto.

### Comandi utili

```bash
docker compose logs -f web                             # il comando da sapere a memoria
docker compose run --rm web fler stato                 # stato dell'indice e ultimi errori
docker compose run --rm web fler ingest                # aggiornamento manuale
docker compose run --rm web fler ingest --full         # riscansione completa
docker compose run --rm web fler cerca "accreditamento organismi"
docker compose run --rm web fler chiedi "Quante ore di stage nei percorsi IeFP?"
docker compose restart web                             # invalida le sessioni aperte
```

`fler ingest` esce con codice **2** se il giro è stato parziale: è così che
cron può accorgersene. `fler cerca` è il comando più utile per capire se un
problema sta nel recupero o nella generazione — mostra cosa restituisce
l'indice, con similarità, riferimenti riconosciuti e se il sistema
risponderebbe o si asterrebbe.

---

## 5. Calibrare la soglia di astensione

`FLER_MIN_RELEVANCE` decide quando il sistema si rifiuta di rispondere, e il
valore predefinito (**0,42**) è una stima prudente, non una misura. Va calibrato
sulle vostre domande, ed è mezz'ora di lavoro che vale più di qualunque altra
messa a punto:

1. Scrivete 30–40 domande reali del centro. Metà con una risposta che sapete
   essere nella documentazione, metà su materie che sicuramente **non** ci sono
   (normativa di un'altra regione, prassi interne, domande fuori tema).
2. Per ognuna lanciate `fler cerca "<domanda>"` e guardate la riga
   «Il sistema risponderebbe / SI ASTERREBBE».
3. Alzate la soglia finché il sistema non risponde più a **nessuna** delle
   domande fuori corpus. Quello è il valore giusto.
4. Se a quel punto si astiene anche su domande legittime, il problema non è la
   soglia: è che quei documenti non sono indicizzati, o che la domanda usa
   termini diversi da quelli degli atti. Si verifica con `fler cerca`.

Non abbassate la soglia per far rispondere più spesso. Il valore basso era il
difetto della versione precedente: il sistema rispondeva sempre, anche con
estratti non pertinenti, ed è esattamente la configurazione che produce
risposte verosimili e infondate.

---

## 6. Costi

| Voce | Quando | Stima |
|---|---|---|
| Embedding prima indicizzazione | una tantum | 4–9 € |
| Embedding aggiornamenti | mensile | < 1 € |
| Risposte (~500 domande/mese) | mensile | 8–15 € |
| VPS + snapshot | mensile | ~6 € |

**Circa 15–22 € al mese** a regime. Ogni domanda costa due chiamate al modello
(riscrittura della domanda e generazione). **Impostate un tetto di spesa
mensile sulla console Anthropic o OpenAI**: è l'unica difesa che funziona anche
quando il codice ha un buco. I limiti di frequenza (`FLER_RATE_CHAT`, 30
domande l'ora per indirizzo) sono la prima barriera, non l'ultima.

---

## 7. Limiti da conoscere

Un assistente di cui non si conoscono i limiti è più pericoloso di uno che non
c'è.

- **Il sistema non sa cosa è vigente.** Indicizza il pubblicato, comprese
  delibere superate. Non ricostruisce le catene di abrogazione. Segnala i
  termini scaduti e retrocede leggermente gli atti vecchi a parità di
  pertinenza, ma la verifica della vigenza resta umana.
- **Copre il pubblicato, non il non pubblicato.** Circolari interne, prassi
  consolidate, risposte ricevute per email dal servizio regionale non sono sul
  portale e per l'assistente non esistono.
- **L'OCR sbaglia, tipicamente sulle cifre.** Gli estratti OCR sono segnalati e
  producono un avviso automatico. Su un importo o una scadenza, aprire sempre
  l'originale.
- **Le tabelle complesse si appiattiscono.** Un prospetto a celle unite esce
  come sequenza di celle. Per i parametri di costo verificare sull'originale.
- **La verifica dei valori controlla la presenza, non il senso.** Dalla 1.2 il
  valore deve stare nell'estratto citato da quella frase, non in uno qualunque;
  ma se dentro lo stesso estratto un importo è riferito ad altro (due misure
  nello stesso avviso), il controllo lo lascia passare. Intercetta i valori
  inventati e quelli presi dalla fonte sbagliata, non le letture sbagliate
  della fonte giusta.
- **Non c'è un reranker.** Con 40 candidati fusi e 14 passati al modello, una
  parte del contesto è rumore. È il miglioramento successivo più sensato (un
  cross-encoder su 40 coppie costa poche centinaia di millisecondi).
- **Resta uno strumento di supporto.** Non va usato come fonte in un atto, in
  una rendicontazione o in una comunicazione ufficiale senza aver aperto il
  documento citato.

---

## 8. Privacy e conformità

Due questioni reali, di cui la prima è quella che non si aspetta nessuno.

**L'indice non deve contenere dati personali di terzi.** Il portale pubblica
graduatorie, elenchi di ammessi, determine con nominativi, moduli firmati.
Dalla 1.2 questi documenti sono **esclusi** (`privacy.py`): sul titolo prima
ancora di scaricarli, sul testo estratto quando un allegato dal titolo neutro
contiene codici fiscali o dati di nascita. La modulistica in bianco resta.
`fler stato` riporta quanti documenti sono stati esclusi; il motivo è nel
campo `error` del catalogo. Il resto di questa sezione vale per ciò che le
regole non intercettano. Indicizzando
8.800 allegati se ne copiano sul VPS i dati personali, si conservano a tempo
indeterminato e — al momento della risposta — se ne inviano estratti al
fornitore del modello. Che il documento sia pubblico non rende il trattamento
privo di obblighi: serve la voce nel registro dei trattamenti, il DPA con il
fornitore e il riferimento al trasferimento extra-UE. Vale la pena parlarne con
chi in Officina si occupa di privacy **prima** di aprire il servizio ai
colleghi.

**Le domande vengono registrate.** Servono alla diagnostica, e vengono
cancellate automaticamente dopo 90 giorni ad ogni ingestione. L'interfaccia
avverte di non inserire nomi di allievi. Se un operatore scrive «l'allievo
Mario Rossi può essere reinserito?», quel testo finisce nel database e al
fornitore del modello.

La copia della documentazione pubblica regionale in sé non è un problema:
informazione del settore pubblico, riuso consentito, copia di lavoro interna. È
comunque corretto, se il sistema esce dall'ufficio, informare il servizio
regionale competente. Il crawler si presenta come `FelBot/1.2` e nel
`User-Agent` porta il link a https://felbot.pherio.net/bot, una pagina che
spiega cos'è e come contattare chi lo gestisce: serve a renderlo riconoscibile,
così che un amministratore possa scrivere invece di bloccarlo. Quella pagina
deve esistere prima di avviare la prima scansione.

---

## 9. Aggiungere altre fonti regionali

In `src/fler/sources.yml` sono predisposte, disattivate, le voci per BURERT,
Agenzia regionale per il lavoro, area pubblica SIFER e sezioni lavoro del
portale istituzionale. Per accenderne una:

1. Controllare `robots.txt` del portale e le sue condizioni d'uso.
2. `enabled: true` e, dove utile, restringere con `allow_prefixes`.
3. Provare in piccolo: `fler ingest --full --source agenzialavoro --limit 100`.
4. Guardare `fler stato`: molti documenti `empty` significano che l'estrazione
   per quel portale va tarata.
5. Solo allora la scansione completa.

Partire dall'**Agenzia regionale per il lavoro**: copre tirocini, centri per
l'impiego e politiche attive, la lacuna più sentita rispetto al solo portale
Formazione e Lavoro. Il BURERT va valutato con attenzione: volume alto e in
gran parte estraneo alla formazione, conviene restringerlo per materia.

---

## 10. Cosa è cambiato nella 1.1 e nella 1.2, e perché

Esito di una revisione indipendente su quattro fronti. Ogni voce è una trappola
reale: conoscerle serve a non ricadervi.

**Il crawler indicizzava 100 documenti su 21.320 e terminava senza errori.**
`metadata_fields` ripetuto non è onorato dall'API, quindi mancavano `created` e
`modified`: la paginazione si fermava al primo giro e l'incrementale non
avrebbe mai visto una modifica. Ora si usa `_all`, la paginazione è a offset, e
il numero di oggetti visti si confronta con `items_total`.

**Un errore di rete a metà enumerazione cancellava l'indice.** Enumerazione
interrotta e completata erano indistinguibili; `mark_gone_unseen` marcava come
rimossi i documenti non visti e la fase successiva ne cancellava i frammenti da
SQLite e da LanceDB, irreversibilmente. Ora la rimozione richiede
un'enumerazione dichiarata completa e una quota di scomparsi sotto il 5%, e un
documento ritirato torna in coda invece di restare «indicizzato» a vuoto.

**`last_run_finished` avanzava anche dopo un giro fallito**, dichiarando coperta
una finestra temporale che nessuno aveva guardato. Ora avanza solo in caso di
successo, e `fler ingest` esce con codice 2 se il giro è parziale.

**La verifica delle citazioni non guardava i contenuti.** Controllava che i
numeri fra parentesi quadre esistessero. Un importo alterato con citazione
valida usciva con affidabilità *alta*. Ora i valori vengono confrontati con il
testo degli estratti citati.

**La frase «Non risulta dai documenti indicizzati» disattivava ogni
controllo** se compariva in qualunque punto della risposta — e il prompt chiede
al modello di dichiarare ciò che non risulta, quindi la collisione era
strutturale. Ora il rifiuto si riconosce solo se la risposta inizia con quella
formula e non contiene altro.

**Un riferimento inventato veniva cancellato dal testo**, lasciando
l'affermazione infondata priva di marcatore e potendo far salire l'affidabilità.
Ora resta visibile e forza *bassa*.

**Le affermazioni sotto 60 caratteri non erano controllate**: «Importo massimo:
50.000 euro» e le voci di elenco, cioè esattamente ciò che conta in una nota
amministrativa. Ora la risposta è spezzata anche su righe e punti elenco.

**`is_confident()` era quasi sempre vero**, quindi l'astensione non esisteva in
pratica. Le condizioni sono ora congiuntive con quorum, e la soglia è passata da
0,32 a 0,42 — 0,32 cadeva dentro il rumore di fondo del corpus, alzato dalle
intestazioni standardizzate presenti in ogni frammento.

**`expires`, `review_state` e il flag OCR erano raccolti, salvati, idratati e
mai usati.** Un bando chiuso nel 2022 arrivava al modello identico a uno aperto,
e la regola del prompt sull'OCR era inapplicabile perché nessun estratto portava
quel flag: il README della 1.0 dichiarava il contrario, ed era falso. Ora lo
stato è nel fascicolo e gli avvisi sono generati dal codice.

**Al modello arrivava la domanda grezza, non quella riscritta.** Su un follow-up
(«E la scadenza?») il modello doveva indovinare il soggetto dagli estratti, e
citava plausibilmente la scadenza di un altro procedimento — con citazione
valida e dato esatto, quindi invisibile a ogni verifica.

**L'estrazione bloccava l'event loop**, rendendo seriale ciò che doveva
sovrapporsi; l'OCR era deciso sulla media del documento (perdendo gli allegati
scansionati dentro PDF lunghi); i PDF a due colonne uscivano illeggibili ma
superavano i controlli; gli archivi ZIP venivano trattati come `.docx` e
fallivano in silenzio.

**La cancellazione full-text costava una scansione integrale** per frammento
(~250 ms a 315.000 righe): una re-indicizzazione erano ore di soli DELETE, con
transazioni lunghe che facevano scadere i writer del servizio web. Ora è per
rowid, 0,9 ms costanti. Contestualmente il titolo è uscito dall'indice
full-text: era in tre posti con peso 2,5 su un campo cortissimo, e bastava un
titolo parlante per portare in cima frammenti il cui corpo non trattava
l'argomento.

**Il perimetro del servizio era aperto:** password che poteva essere vuota senza
alcun sintomo, nessun limite di frequenza su login e chat, `history` e `k` senza
tetto (una sola richiesta poteva esaurire la memoria del VPS), container come
root con LibreOffice su file arbitrari, cookie senza `secure`, documentazione
automatica pubblica, log senza rotazione, nessun backup. Tutto corretto.

**La ricerca vettoriale poteva morire in silenzio** dopo ogni ingestione: il
processo web teneva il manifest LanceDB in memoria mentre lo scheduler
ricostruiva i file. Ora si rilegge prima di ogni ricerca, e un ramo di ricerca
non disponibile produce un avviso visibile nella risposta e in `fler stato`
invece di degradare il sistema senza dirlo.

---

### Cosa è cambiato nella 1.2

**La verifica dei valori si aggirava in cinque modi**, tutti riprodotti nei test:
un numero giusto attribuito all'estratto sbagliato usciva con affidabilità
*alta*, perché bastava che comparisse in un qualunque estratto citato; un numero
suggerito dalla domanda («è vero che sono 80.000 euro?») era sempre accettato;
le cifre singole erano ignorate («entro 5 giorni» al posto di 30, «3%» al posto
di 5%); i valori in lettere non erano controllati; le date erano spezzate in
numeri sciolti e giorno e mese sfuggivano. Ora c'è `valori.py`, con un insieme
generoso di letture per le fonti e un controllo stretto, frase per frase, sulla
risposta. Sul versante opposto, i numeri delle tabelle appiattite («300 400
600») venivano fusi in uno solo e generavano falsi allarmi: un controllo che
grida al lupo viene ignorato presto.

**Un rifiuto con contenuto in coda passava senza controlli**, se restava sotto i
500 caratteri. Ora è rifiuto solo se dopo la formula non ci sono né valori né
riferimenti.

**Il crawler poteva ancora ritirare documenti pubblicati.** L'enumerazione è
dichiarata completa con una tolleranza del 2%, la soglia di sicurezza scattava
solo oltre il 5%: fino a circa 400 documenti saltati dalla paginazione potevano
essere cancellati in silenzio con esito «ok». Ora ogni candidato alla rimozione
viene verificato singolarmente.

**Rimozioni e rivalidazione non avvenivano mai in esercizio.** Lo scheduler
lanciava solo l'incrementale, che enumera i documenti modificati: nessun
documento ritirato veniva rilevato, e la rivalidazione a 30 giorni dichiarata
dal README non scattava per i documenti con data invariata, cioè proprio quelli
per cui esiste. Ora scansione completa settimanale e rivalidazione a quote
notturne.

**I vettori dei frammenti sostituiti restavano nell'indice per sempre.** L'id
di un frammento contiene un hash del testo: a ogni modifica di un documento i
vecchi vettori diventavano orfani, invisibili nelle risposte ma capaci di
occupare posti fra i risultati semantici. Ora vengono accodati e cancellati.

**Graduatorie ed elenchi nominativi sono esclusi dall'indice** (§8).

**Ogni scansione completa riscaricava i documenti senza testo** e quelli
esclusi (robots, troppo grandi): centinaia di download inutili verso la Regione.

**Il limite di frequenza valeva per l'intero ufficio.** Dietro Caddy tutte le
richieste arrivano dallo stesso indirizzo: 30 domande l'ora in tutto, e cinque
password sbagliate di un collega bloccavano il login per tutti. Inoltre la
password era accettata anche in un'intestazione su qualunque rotta, il che
aggirava il limite sul login. Corretti entrambi.

---

## 11. Struttura del codice

```
src/fler/
  config.py      impostazioni (tutto da .env)
  catalog.py     SQLite: stato del crawl, documenti, frammenti, full-text
  fetcher.py     HTTP educato: rate limit, robots.txt, richieste condizionali
  sources/
    plone.py     enumerazione via REST API — la fonte primaria
    web.py       sitemap e crawling per i portali senza API
  extract.py     PDF/DOCX/DOC/ODT/XLSX/PPTX/ZIP/RTF/CSV/HTML + OCR per pagina
  chunking.py    frammentazione sui confini degli atti
  embed.py       embedding
  store.py       indice vettoriale LanceDB
  retrieve.py    recupero ibrido, riferimenti d'atto tipizzati, astensione
  privacy.py     esclusione degli elenchi nominativi
  answer.py      prompt ancorato, generazione, VERIFICA DEI VALORI
  valori.py      estrazione e normalizzazione di importi, date, durate, lettere
  api.py         servizio FastAPI
  web/index.html interfaccia di chat
  cli.py         riga di comando
tests/test_offline.py   117 verifiche, in chiave avversaria, senza rete
```

I file da leggere per capire il sistema sono **`answer.py`** e **`valori.py`**
(il prompt e `verify()`), e **`sources/plone.py`** con **`pipeline.py`** (le assunzioni sull'API e il controllo di
completezza). Sono i due punti in cui un errore non si vede e costa caro.

```bash
pip install pydantic-settings pyyaml httpx tenacity lxml   # dipendenze leggere
python3 tests/test_offline.py    # nessuna rete, nessuna chiave
```

I test sono scritti per **fallire se una garanzia si indebolisce**: ogni caso è
un tentativo di far passare un'affermazione infondata o di far perdere un
documento al crawler. Se ne modificate il comportamento, aspettatevi che
protestino — è il loro lavoro.

---

## 12. Se qualcosa non va

| Sintomo | Dove guardare |
|---|---|
| Il login restituisce 500 | password con caratteri accentati su una versione vecchia; qui è corretto, ma controllare `docker compose logs web` |
| «Non risulta» su domande che dovrebbero avere risposta | `fler cerca "<domanda>"`: se l'indice restituisce i documenti giusti la soglia è troppo alta; se non li restituisce, il documento non è indicizzato |
| Risposte generiche su domande concettuali | `fler stato` → `ricerca_semantica`: se non è «attiva», l'indice vettoriale o la chiave embedding hanno un problema |
| Molti documenti `empty` | PDF scansionati senza OCR: verificare che `tesseract-ocr-ita` sia nell'immagine |
| Molti `error` | `fler stato` mostra gli ultimi messaggi; se sono `HTTP 429` abbassare `FLER_REQUESTS_PER_SECOND` |
| `esito: parziale` in `fler stato` | il giro non è andato a buon fine; il punto di ripartenza non è avanzato, rilanciare `fler ingest` |
| «rimozioni sospese» nei log | il portale ha restituito molti meno documenti del previsto: **non** forzare, verificare il portale e rilanciare `fler ingest --full` |
| Avviso «valori non trovati negli estratti» ricorrente | il modello fatica sul contesto: alzare `FLER_CONTEXT_CHUNKS` o usare un modello più capace |
| L'indicatore in alto è giallo con ⚠ | l'indice non si aggiorna da più di 4 giorni: controllare `docker compose logs scheduler` |
