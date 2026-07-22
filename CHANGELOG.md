# Zgodovina različic

## 1.24.0-alpha

- namestljiva PWA za Android in iPhone z lastno ikono ter samostojnim prikazom;
- varen offline pregled zadnjega odprtega skeda, vpis in odstranitev udeležencev
  ter urejanje zapisnika;
- samodejna sinhronizacija ob ponovni povezavi brez podvajanja že poslanih
  operacij;
- zaznavanje konflikta, če je sked medtem zaključen ali je bil zapisnik
  spremenjen na drugi napravi;
- lokalni podatki se odstranijo ob odjavi ali ročno na offline zaslonu;
- strežniške strani s sejami in varnostnimi žetoni se ne shranjujejo v spletni
  predpomnilnik;
- shema baze 3 in 55 avtomatiziranih testov.

## 1.23.0-alpha

- nova nadzorna plošča Raspberry Pija s temperaturo, prostorom na disku,
  pomnilnikom, obremenitvijo in časom delovanja;
- samodejno osveževanje meritev brez privilegiranega vsebnika ali Docker
  vtičnice;
- posodobljena Flask 3.1.3 in Gunicorn 26.0.0 brez znanih ranljivosti;
- strožja politika izvajanja JavaScripta ter odstranjeni vstavljeni skripti in
  dogodki iz aktivnih pogledov;
- varno preverjanje klicnih znakov, gostiteljev in preusmeritev po prijavi;
- najmanj 15 znakov za nova gesla, absolutna 24-urna omejitev seje ter preklic
  drugih sej ob spremembi ali ponastavitvi gesla;
- posredovane glave se upoštevajo samo iz izrecno dovoljenih mrež;
- utrjena Docker vsebnika z datotečnim sistemom samo za branje, odstranjenimi
  zmožnostmi, omejitvijo procesov in pravilom `no-new-privileges`;
- shema baze 2 in 49 avtomatiziranih testov.

## 1.22.0-alpha

- prenovljen in bolje kontrasten mobilni meni;
- navigacija je razdeljena na portal, upravljanje in uporabniški račun;
- trenutna stran je v meniju jasno označena;
- meni se zapre ob izbiri, pritisku Escape ali pritisku izven menija;
- osvežene kartice, gumbi, obrazci, tabele in mobilni razmiki;
- kompaktnejša oznaka testnega okolja.

## 1.21.0-alpha

- operater in administrator sta edina, ki lahko spreminjata ali zaključita odprt sked;
- način vodenja v živo z zadnjimi petimi prijavami in varno razveljavitvijo;
- opozorilo ob podobnem klicnem znaku;
- javni urnik in koledar `.ics`;
- zložljiv mobilni meni;
- zaščita zadnjega aktivnega administratorja in obvezna menjava začasnih gesel;
- varnejše seje, varnostne HTTP-glave in preverjanje baze v `/health`;
- ločena hramba dnevnih, ročnih in varnostnih kopij ter izbirna druga lokacija;
- administratorski pregled stanja sistema in svežine kopij;
- različice migracij SQLite sheme in indeks klicnih znakov;
- ločeni aktivni HTML-pogledi, CSS in JavaScript;
- 42 izoliranih testov, GitHub Actions in Dependabot.
