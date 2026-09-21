# Kvittoplanen

Receipt samlar kvitton, matchar dem mot köp och följer upp det som återstår. Odin sköter insamlingen. Oscar lämnar Amex-underlaget, hjälper till vid verkliga åtkomsthinder och godkänner bokföring och ersättning i en gemensam batch. Detta är den beslutade målbilden 2026-09-21. Det löpande flödet är ännu inte färdigbyggt eller verifierat i drift.

## Första provet: augusti 2026

Augusti ska stänga den gamla kön först. Underlaget måste visa att hela perioden omfattas. Senaste köpdatum räcker inte som bevis. Historiska Pleo-utgifter, bokföring och ersättningar ska stämmas av så att ett återfunnet kvitto aldrig leder till dubbel ersättning.

## Det löpande flödet

1. Spara inkommande kvitton från mejl, inklusive PDF, bild och kvittotext i mejlet. Behåll original, källa och sökbar Markdown med frontmatter även innan ett köp har importerats.
2. Håll privata kvitton och respektive bolags kvitton åtskilda. Okänd tillhörighet väntar på klassificering utan att originalet förloras.
3. Importera Amex-underlaget och kontrollera periodtäckning. Matcha mot befintligt arkiv med konto, ägare, belopp, valuta och datum som evidens.
4. Bygg leverantörsregistret från tidigare kvitton, Pleo och bokföring: konto, faktureringsmejl, portal, inloggningsreferens och senast fungerande metod. Lagra hemligheter i avsedd hemlighetshantering.
5. Hämta återstående underlag via mejl, API/MCP eller datorinteraktion. En blockerad leverantör ska inte stoppa andra köp. Spara försöken och återuppta efter avbrott.
6. Rapportera kvarstående undantag med vad som saknas, vad som har prövats, exakt nästa hjälp och hur återkommande problem kan förebyggas.

## Beslutad automatik

Säkert matchade kvitton får bifogas automatiskt till befintliga Pleo-utgifter. Resultatet ska läsas tillbaka och verifieras. Osäkra matchningar behöver granskas.

Bokföring och ersättning förbereds enligt fastställda regler och godkänns tillsammans av Oscar. Den första versionen återanvänder den gemensamma batchen; separat automatisk bokföring behöver inte byggas. Oklar bolagstillhörighet, kontering eller momsbehandling stoppar den berörda raden tills frågan är löst.

Ersättning kräver Oscars godkännande. Ett verifikat, ett förberett löneunderlag eller ett återfunnet kvitto bevisar inte att pengar har betalats ut.

## Fyra separata statusar

- **Kvitto klart:** originalet är sparat och verifierat mot rätt köp och ägare.
- **Bokfört:** posten är verifierad i bokföringen med referens.
- **Ersatt:** faktisk ersättning är styrkt med betalningsreferens.
- **Avslutat:** samtliga tillämpliga steg är verifierat klara.

Varje status behöver visa sin källa. Ett ej tillämpligt steg skiljs från ett steg som saknar evidens. Återstående undantag får inte döljas av att ett annat steg är klart.

## Vad som finns och vad som återstår

Markdown-arkivet, filerna, mejlsökningen och Kvittotavlan finns. Den nuvarande mejlhämtningen utgår främst från transaktioner; insamling före ett känt köp behöver tillkomma.

Kodgranskningen 2026-09-21 visar att Kvittotavlan kan kalla ett köp ersatt enbart på grund av ett verifikat. Fortnox-planbyggaren utesluter inte automatiskt gamla Pleo-ersättningar. Den gemensamma exekveraren hanterar bokföring och löneunderlag i samma batch. Status- och dubblettskydden behöver rättas och provas före skarp körning. Den gemensamma batchen behålls för enkelhetens skull.

Nästa byggsteg behöver därför en gemensam evidensmodell, skydd mot dubbel behandling, ett gemensamt godkännande för bokföring och ersättning samt en inkorg för kvitton som ännu saknar matchat köp. Schemaläggning och beständig åtkomst ska provas i den verkliga körmiljön på Odin, inte antas fungera utifrån en installerad CLI eller en tidigare interaktiv inloggning.

## Senare beslut

Pleo-köp flyttas först efter en avstämd provmånad och granskad historik. Att flytta Oscars köp är ett separat beslut från att säga upp Pleo för hela bolaget. Accounted kan utvärderas som framtida bokföringsdestination; Receipt behåller arkiv och matchning oavsett destination.
