# S50TTT Dnevnik skedov

Ločen spletni portal Radiokluba Sevnica S50TTT za vodenje skedov.

## Funkcije

- več uporabnikov z vlogama administrator in vodja skeda;
- samodejni izračun naslednjega mesečnega in sobotnega rednega skeda;
- odpiranje novega skeda z datumom, uro in vodjo;
- hiter vnos imena, klicnega znaka ter ure prijave;
- preprečevanje podvojenega klicnega znaka v istem skedu;
- urejanje in brisanje napačnih vnosov;
- zaključevanje skedov in trajni arhiv;
- tiskanje dnevnika oziroma shranjevanje v PDF prek brskalnika;
- mobilnim napravam prilagojen prikaz;
- sled sprememb v podatkovni bazi;
- SQLite podatkovna baza v trajni mapi `data`.

## Redni skedi

Portal pozna redni urnik Radiokluba Sevnica in na domači strani ponudi naslednja
termina:

- mesečni sked vsak prvi četrtek v mesecu ob 19.00;
- sobotni sked prek repetitorja `S55USX` na Sv. Roku vsako soboto;
- sobotni sked se od 1. septembra do 31. maja začne ob 20.00, od 1. junija do
  31. avgusta pa ob 21.00.

Upravna postaja rednega skeda je `S50TTT`, operater pa prijavljeni član kluba, ki
odpre dnevnik. Portal prepreči, da bi bil za isti redni termin odprt podvojen
dnevnik. Drug ali izredni sked je še vedno mogoče odpreti ročno.

## Namestitev

Portal je pripravljen za Docker Compose. Privzeto posluša na portu `8023`.

```bash
chmod +x install.sh
./install.sh
```

Namestitveni program ob prvem zagonu ustvari administratorski račun `S57ZM` in
izpiše naključno začetno geslo. Po prvi prijavi ga je treba zamenjati.

Lokalni naslov na rpi-services:

```text
http://192.168.1.57:8023
```

Za javni naslov `skedi.s57zm.eu` se v Nginx Proxy Managerju ustvari Proxy Host:

- Forward Hostname/IP: `192.168.1.57`
- Forward Port: `8023`
- Scheme: `http`
- Websockets Support: vključeno
- SSL: nov Let's Encrypt certifikat, Force SSL in HTTP/2

## Varnostna kopija

Za varnostno kopijo je dovolj kopirati datoteko:

```text
data/skedi.db
```

Pred kopiranjem je priporočljivo za kratek čas ustaviti vsebnik.

## Posodobitev

```bash
git pull
docker compose up -d --build
```

## Preizkus

Po gradnji slike je mogoče preveriti pravila rednih terminov z:

```bash
docker compose run --rm skedi python -m unittest discover -s tests -v
```
