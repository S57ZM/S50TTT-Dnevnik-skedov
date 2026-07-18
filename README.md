# S50TTT Dnevnik skedov

Ločen spletni portal Radiokluba Sevnica S50TTT za vodenje skedov.

## Funkcije

- več uporabnikov z vlogama administrator in vodja skeda;
- odpiranje novega skeda z datumom, uro in vodjo;
- hiter vnos imena, klicnega znaka ter ure prijave;
- preprečevanje podvojenega klicnega znaka v istem skedu;
- urejanje in brisanje napačnih vnosov;
- zaključevanje skedov in trajni arhiv;
- tiskanje dnevnika oziroma shranjevanje v PDF prek brskalnika;
- mobilnim napravam prilagojen prikaz;
- sled sprememb v podatkovni bazi;
- SQLite podatkovna baza v trajni mapi `data`.

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
