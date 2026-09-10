# Kvittoplanen

Vad som krävs för att kvittoflödet ska gå av sig självt. Genomgången gjordes 2026-09-04 med nio agenter: en inventering, tre oberoende förslag och två kritiker. Texten beskriver läget nu. Siffrorna överst räknas fram ur tavlans underlag varje gång sidan byggs.

> **Först av allt.** Juni- och juliplanerna får inte köras som de är. Ett tjugotal Amex-rader ersattes redan genom Pleo-utbetalningarna 2026-07-09, bland annat Wincher 2026-06-10 på 3 548,41 SEK, och planbyggaren `fortnox_lane.py` läser inte den flaggan. Planerna är stoppade för hand sedan 2026-09-07. Steg 1 gör stoppet mekaniskt, innan någon godkännandekod lämnas tillbaka.

## Vad "helt automatiskt" betyder

Målet är 15 till 25 minuter i månaden för Oscar, inte noll. Fyra saker stannar hos dig, av skäl som inte går att bygga bort:

- **Amex-CSV en gång i månaden**, cirka 5 minuter. Inget svenskt Amex-kort har en maskinell feed. Att skrapa portalen bakom SafeKey och Akamai blir det bräckligaste i hela systemet.
- **Godkännandekoden för Fortnox**, cirka 5 minuter. Din egen regel, och koden är dess spärr. En vetotid i stället för kod är ditt att ge senare.
- **En tagg per aldrig sedd handlare**, 1 till 2 minuter. Företag eller privat gissas aldrig. Varje svar blir en regel, så samma leverantör frågas aldrig två gånger.
- **BankID och inloggningar.** Kivra kräver Mobilt BankID för varje användare. Pleo- och Fortnox-samtycken är bundna till dig. Utgående mejl och Pleo-utbetalningar likaså.

Allt annat som är manuellt i dag är en av tre saker: kod som finns men ingen har schemalagt, en policy bara du kan lätta på, eller ett kvitto som aldrig når en brevlåda.

## Var det manuella sitter

- **Inget är schemalagt.** Matchning, hämtning, arkivkontroll och tavlan körs bara när en agent kommer ihåg det. Lösningen är en deterministisk körning på OpenClaw-cron, som redan bär 1Password, Gmail-nyckeln och Discord. Fortnox-keepalive klockan 06:15 bevisar att miljön fungerar utan händer. Dagligen: matcha, hämta, kontrollera arkivet, bygg tavlan, posta i #receipts bara när något ändrats. Den 3:e varje månad: månadsstängning, dry-run, plan.md och koden till din telefon.
- **Pleo hänger på en levande session.** Claude Codes nyckellager har ingen refresh-token för Pleo, så varje Pleo-åtgärd kräver att du klistrar in callback-länken. Pleos OAuth-server stödjer refresh-tokens och egen klientregistrering, kontrollerat 2026-09-04. En egen liten klient med token i Odins nyckelring är ett test bort, med en inklistring från dig. Fungerar det kör Lane A utan dig. Annars blir det en session på 5 minuter i veckan.
- **Fel kvitton bifogas.** De flaggade raderna visar att bifoga utan kontroll är felmoden. Tolv rättades 2026-09-07 och de gamla filerna ligger kvar. En kontrollgrind före varje bifogning återanvänder arkivets egna kontroller: belopp på öret, datumfönster, köpare Viseo AB, fakturanummer och filhash som inte redan sitter på ett annat köp. Aldrig på rader som redan exporterats till Trimero. Med grinden på plats kan du ge en stående regel att bifoga utan ja per batch, och sammanfattningen rapporterar i efterhand.
- **Kvitton som aldrig kommer.** Åtgärda vid källan, inte med webbläsarautomation. Hitta inloggningen bakom de tre Claude Pro-raderna och peka faktureringsmejlen mot viseo.se. NordVPN på tvåårsavtal. SATS årskvitto en gång om året. Avgör om ChatGPT Pro är värt en manuell faktura, eftersom OpenAI aldrig mejlar kvitton. Registrera Viseos momsnummer i varje portal. Skapa kvitton@viseo.se som gratis alias, så varje leverantör fakturerar en adress systemet redan läser.
- **Kivra.** En kontroll av dig: lägger Kivra sin egen faktura i företagsbrevlådan? Om ja speglar Kivra-till-Fortnox-kopplingen den till Fortnox-inkorgen och Nicolina bokför den. Om inte, be henne om en regel för bruttobokning och hämta PDF:erna en gång i kvartalet.
- **Beslut som blockerar driftstarten.** Trimero-brevet är oskickat sedan 2026-08-24. Det behöver fyra rader till: brytdag och vem som kör Pleos exportkö, vem som kör lönekörningen som betalar ditt utlägg, hur hon markerar en stängd månad, och beloppsgränsen för kvitto. Av de otaggade Amex-raderna ligger de flesta på privatkortet och blir privata enligt din egen regel. Kvar för dig är raderna på företagskortet plus två kortbeslut: kontot 13003 privat, kontot 61006 samma kort som 62004.
- **Behörigheter.** Klassificeraren stoppar agenten från dry-runs, så du kör dem. En inställningsfil som tillåter dry-runs och de skrivskyddade Gmail-skripten, och nekar allt med `--execute`, tar dig ur varje felsökningsloop.

## Ordning

1. **Systemet:** uteslut Pleo-ersatta rader och inför kvittoregeln i Fortnox-planen: inget momslyft utan kvitto, rader över 4 000 SEK utan kvitto hålls. Bygg om planerna för maj, juni och juli.
2. **Du, 15 minuter:** skicka brevet, tagga raderna på företagskortet, de två kortbesluten, "godkänn receipts cron", godkänn behörighetsinställningarna.
3. **Systemet:** cron-ingången, OpenClaw-jobben, rutindokumenten i Thor, månadssammanfattningen, en fast måndagsrad så att tystnad märks.
4. **Första skarpa körningen** på juli, sedan juni och maj. Utan lönerad tills Nicolinas svar om löneart finns.
5. **Pleo-tokentest**, sedan kontrollgrinden och den stående bifogningsregeln.
6. **Din leverantörsgenomgång**, cirka 30 minuter.
7. **Din eftersläpningskväll**, cirka 2 timmar, förberedd av systemet: Bazooms marsfaktura, Kivra-PDF:erna, de kvarvarande omfilningarna med filer förhämtade, den väntande utbetalningen, och förnya det virtuella kortet en gång före oktober.
8. **Senare, efter två rena Amex-månader:** flytta SaaS-abonnemangen från Pleo och lämna Pleo vid de tre kontrollpunkterna.

Inte nu: skrapning av Amex- eller leverantörsportaler, en andra Fortnox-profil för 5555 Media, en vetotid i stället för godkännandekod.

## Vad det ger

I dag går ungefär 105 minuter i månaden, och det är en underskattning eftersom du driver sessionerna. Efter steg 1 till 7 återstår 15 till 25 minuter, plus en engångsinsats på ungefär 2 timmar för eftersläpningen.
