# Alpha testna različica

Alpha portal je namenjen preverjanju novih funkcij, preden pridejo v produkcijsko
različico. Produkcija in alpha sta popolnoma ločeni:

```text
veja main  ──> s50ttt-skedi        ──> port 8023 ──> data/skedi.db
veja alpha ──> s50ttt-skedi-alpha  ──> port 8024 ──> data-alpha/skedi-alpha.db
```

Alpha na vsaki strani prikaže rumeno opozorilo in različico, na primer
`1.25.0-alpha`. Vpisani testni podatki nikoli ne končajo v produkcijski bazi.

Trenutne alpha funkcije vključujejo lokalni imenik klicnih znakov,
administratorsko odpoved ali prestavitev rednega skeda, statistiko, CSV/PDF
poročila, pregled revizijske sledi, dnevne preverjene varnostne kopije in
zaščito prijave pred ponavljajočim ugibanjem gesel, profile klicnih znakov z
zgodovino, internimi opombami in združevanjem podvojenih vnosov, koš z
obnovitvijo pomotoma izbrisanih skedov, iskanje in filtriranje arhiva ter
zapisnik oziroma opombe posameznega skeda, varen CSV-uvoz zgodovinskih
dnevnikov s predogledom in samodejno varnostno kopijo, varnostno utrjevanje ter
pregled temperature, prostora, pomnilnika in obremenitve Raspberry Pija.

## 1. Ustvarjanje veje alpha

Po objavi alpha podpore na veji `main` v lokalnem PowerShellu zaženi:

```powershell
git switch -c alpha
git push -u origin alpha
git switch main
```

## 2. Prva namestitev na Raspberry Pi

Alpha naj bo v svoji mapi, ločeni od produkcijskega repozitorija:

```bash
cd ~
git clone -b alpha https://github.com/S57ZM/S50TTT-Dnevnik-skedov.git S50TTT-Dnevnik-skedov-alpha
cd ~/S50TTT-Dnevnik-skedov-alpha
chmod +x install-alpha.sh
./install-alpha.sh
```

Namestitveni program ustvari `.env.alpha`, novo administratorsko geslo in prazno
bazo `data-alpha/skedi-alpha.db`. Privzeti lokalni naslov je:

```text
http://192.168.1.57:8024
```

## 3. Domena

Predlagani javni naslov je `alpha.skedi.s57zm.eu`. V Nginx Proxy Managerju se
ustvari nov Proxy Host:

- Forward Hostname/IP: `192.168.1.57`
- Forward Port: `8024`
- Scheme: `http`
- SSL: Let's Encrypt, Force SSL in HTTP/2

Ker gre za testno okolje, je smiselno v Nginx Proxy Managerju dodati še Access
List oziroma dodatno zaščito z geslom.

## 4. Posodabljanje alpha različice

Nove funkcije se razvijajo in objavljajo na veji `alpha`. Na strežniku se nato
namestijo z:

```bash
cd ~/S50TTT-Dnevnik-skedov-alpha
git pull --ff-only origin alpha
docker compose --env-file .env.alpha -f docker-compose.alpha.yml up -d --build --force-recreate
docker compose --env-file .env.alpha -f docker-compose.alpha.yml ps
```

Preverjanje različice:

```bash
curl -s http://127.0.0.1:8024/health
```

Pričakovani odgovor vsebuje kanal `alpha`:

```json
{"channel":"alpha","database":"ok","schema_latest":3,"schema_version":3,"status":"ok","version":"1.25.0-alpha"}
```

Alpha varnostne kopije se shranjujejo v `backups-alpha/`. Obnovitev izbrane
kopije se izvede samo ob ustavljenih alpha storitvah:

```bash
cd ~/S50TTT-Dnevnik-skedov-alpha
docker compose --env-file .env.alpha -f docker-compose.alpha.yml stop skedi-alpha backup-alpha
docker compose --env-file .env.alpha -f docker-compose.alpha.yml run --rm --no-deps backup-alpha python backup.py verify IME_KOPIJE.sqlite3
docker compose --env-file .env.alpha -f docker-compose.alpha.yml run --rm --no-deps backup-alpha python backup.py restore IME_KOPIJE.sqlite3 --confirm
docker compose --env-file .env.alpha -f docker-compose.alpha.yml up -d
```

## 5. Prenos preverjene funkcije v produkcijo

Ko je funkcija v alphi preverjena, se veja združi v `main`:

```powershell
git switch main
git pull --ff-only
git merge --no-ff alpha
git push
```

Nato se produkcija običajno posodobi v svoji obstoječi mapi:

```bash
cd ~/S50TTT-Dnevnik-skedov
git pull --ff-only origin main
docker compose up -d --build --force-recreate
```

Pred združitvijo je priporočljivo v alpha mapi zagnati vse preizkuse:

```bash
docker compose --env-file .env.alpha -f docker-compose.alpha.yml run --rm skedi-alpha python -m unittest discover -s tests -v
```

## 6. Preizkus mobilne offline aplikacije

Namestitev PWA in service worker na telefonu zahtevata varen naslov HTTPS.
Neposredni lokalni naslov `http://192.168.1.57:8024` omogoča običajni dostop,
ne omogoča pa namestitve ali offline načina. Za preizkus uporabi alpha domeno z
veljavnim TLS-potrdilom; dostop lahko ostane omejen z Nginx Proxy Manager Access
List oziroma samo na domače omrežje.

Android:

1. v Chromu odpri alpha domeno prek HTTPS in se prijavi;
2. izberi `Namesti aplikacijo` oziroma meni `Namesti aplikacijo`;
3. zaženi nameščeno aplikacijo, se prijavi in obišči domačo stran, da se shranita
   urnik in imenik.

iPhone oziroma iPad:

1. v Safariju odpri alpha domeno prek HTTPS;
2. izberi `Deli` in `Dodaj na začetni zaslon`;
3. zaženi ikono S50TTT, se prijavi in obišči domačo stran, da se shranita urnik
   in imenik.

Offline preizkus:

1. po obisku domače strani na telefonu vključi letalski način;
2. ponovno odpri aplikacijo, odpri shranjeni redni termin ali izredni sked ter
   dodaj udeleženca in zapisnik;
3. izključi letalski način in ponovno odpri aplikacijo;
4. počakaj na obvestilo o končani sinhronizaciji ter preveri podatke in revizijo
   v spletnem portalu.

Zaključevanje skeda je namenoma onemogočeno brez povezave. Ob odjavi ali izbiri
`Odstrani lokalne podatke` se shranjeni sked in neposlane spremembe izbrišejo iz
naprave.

## Pomembno

- Produkcijske baze ne kopiraj čez alpha bazo brez predhodne varnostne kopije.
- Testne prijave in izbrisi sodijo samo v alpha portal.
- Veje `alpha` ne združi v `main`, dokler funkcija ni preverjena.
- Rumena oznaka ALPHA mora biti vedno vidna na testnem portalu.
