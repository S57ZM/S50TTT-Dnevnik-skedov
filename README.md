# S50TTT Dnevnik skedov

Ločen spletni portal Radiokluba Sevnica S50TTT za vodenje skedov.

Trenutna alpha različica: **1.12.0-alpha**

## Funkcije

- več uporabnikov z vlogama administrator in vodja skeda;
- samodejni izračun naslednjega mesečnega in sobotnega rednega skeda;
- administratorska odpoved ali prestavitev rednega skeda z obveznim razlogom;
- zaklenjeno odpiranje rednega dnevnika do petka pred skedom, s predčasnim
  odklepom po petih hitrih pritiskih;
- živ odštevalnik, številka sobotnega skeda in skupno število prijavljenih na
  prijavni strani;
- zaporedna številka in število prijavljenih za zadnja dva zaključena sobotna
  skeda na prijavni strani;
- zaporedno številčenje sobotnih skedov od 5. januarja 2019;
- odpiranje novega skeda z datumom, uro in vodjo;
- hiter vnos imena, klicnega znaka ter ure prijave;
- preprečevanje podvojenega klicnega znaka v istem skedu;
- urejanje in brisanje napačnih vnosov;
- brisanje praznega odprtega skeda s strani operaterja ali administratorja;
- administratorsko popravljanje podatkov in prijav v zaključenih skedih;
- nadzorovano brisanje zaključenega skeda z obveznim razlogom in ohranjeno
  revizijsko kopijo vseh podatkov;
- zaključevanje skedov in trajni arhiv;
- tiskanje dnevnika oziroma shranjevanje v PDF prek brskalnika;
- mobilnim napravam prilagojen prikaz;
- sled sprememb v podatkovni bazi;
- SQLite podatkovna baza v trajni mapi `data`.

## Imenik klicnih znakov (alpha)

Alpha različica vsebuje lokalni imenik klicnih znakov. Ko je novi udeleženec
prvič uspešno dodan v dnevnik, se njegov klicni znak in ime samodejno shranita v
imenik. Ob naslednjem vnosu izbira znanega klicnega znaka samodejno izpolni ime.

Če je obstoječi klicni znak pozneje vpisan z drugačnim imenom, imenik imena ne
prepiše samodejno. Administrator ga lahko popravi ali skrije na strani `Imenik`.
Ob nadgradnji se imenik enkrat dopolni tudi iz že shranjenih prijav. Vsak vnos
hrani število uporab in čas zadnje prijave.

Administrator lahko v urejevalniku zaključenega skeda naknadno doda, popravi ali
izbriše prijavljenega člana. Naknadno dodani klicni znaki se prav tako shranijo v
imenik, vsi posegi pa ostanejo v revizijski sledi.

## Popravki zaključenih skedov

Administrator lahko v arhivu popravi naslov, datum, začetno in končno uro,
operaterja ter posamezne prijave zaključenega skeda. Sprememba se zabeleži v
revizijsko sled. Skedi, ki se končajo po polnoči, pravilno dobijo končni datum
naslednjega dne.

Zaključeni sked lahko izbriše samo administrator. Pred brisanjem mora navesti
razlog z najmanj 10 znaki; obrazec za brisanje je na voljo neposredno pod
obrazcem za popravljanje skeda. V tabeli `net_deletions` ostanejo razlog, datum
in izvajalec brisanja ter kopija podatkov skeda in vseh njegovih prijav.

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

Administrator lahko prihodnji redni sked odpove ali prestavi na drug datum in
uro. Odpovedani termin se odstrani iz odštevalnika in ga ni mogoče odpreti.
Prestavljenemu sobotnemu skedu ostane zaporedna številka prvotnega termina,
odštevalnik pa sledi novemu terminu. Obvezen razlog, sprememba in morebitna
razveljavitev se shranijo v revizijsko sled.

Gumb za odpiranje rednega dnevnika je do petka pred terminom siv. Sobotni sked
se običajno odklene dan prej, mesečni četrtkov sked pa v petek prejšnjega tedna.
Če je treba dnevnik izjemoma pripraviti prej, ga pet hitrih zaporednih pritiskov
na sivi gumb predčasno odpre. Ob petem pritisku se prikaže sporočilo »Ti si
pravi Heker«, dogodek pa se zabeleži v revizijsko sled.

Sobotni skedi se številčijo neprekinjeno od prve sobote leta 2019: sked 5.
januarja 2019 je št. 1. Po tem pravilu je sobotni sked 25. julija 2026 št. 395.

## Namestitev

Portal je pripravljen za Docker Compose. Privzeto posluša na portu `8023`.

```bash
chmod +x install.sh
./install.sh
```

Namestitveni program ob prvem zagonu ustvari administratorski račun `S57ZM` in
izpiše naključno začetno geslo. Po prvi prijavi ga je treba zamenjati.

Za varno preizkušanje novih funkcij je na voljo tudi povsem ločena alpha
namestitev. Uporablja vejo `alpha`, port `8024`, vsebnik `s50ttt-skedi-alpha` in
lastno podatkovno bazo. Celoten postopek je opisan v [ALPHA.md](ALPHA.md).

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
