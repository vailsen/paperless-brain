# config/extraction_rules/pl.py
"""Polish extraction profile — matching user's custom Paperless-ngx document types.

Keys must match the exact case-sensitive document_type names in Paperless.
"""

from config.extraction_rules.base import BASE_INSTRUCTIONS

RULES: dict[str, dict] = {
    # === REJESTRY I AKTY ===
    "akt notarialny": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: akt notarialny.
Pay particular attention to:
- Numer repertorium (np. Rep. A nr ...) i dane Notariusza (imię, nazwisko, siedziba kancelarii)
- Strony czynności notarialnej (dane osobowe, PESEL, NIP, adresy)
- Przedmiot aktu (np. sprzedaż nieruchomości, darowizna, ustanowienie rozdzielności, pełnomocnictwo)
- Dokładny opis nieruchomości (numer księgi wieczystej KW, działki, położenie) jeśli dotyczy
- Koszty: taksa notarialna, podatki (PCC, od spadków i darowizn), opłaty sądowe
""",
    },

    # === CERTYFIKATY I POTWIERDZENIA ===
    "Certyfikat": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Certyfikat / Atest / Oficjalne potwierdzenie.
Pay particular attention to:
- Podmiot wystawiający oraz osoba/rzecz/firma certyfikowana
- Dokładny przedmiot certyfikacji (co, jakie uprawnienia lub normy potwierdza)
- Data wydania oraz data ważności / termin wygaśnięcia
- Numer certyfikatu lub numer rejestracyjny
""",
    },

    # === URZĘDY, PODATKI I DECYZJE ===
    "Decyzja podatkowa": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Decyzja podatkowa (np. nakaz płatniczy podatku od nieruchomości, decyzja US).
Pay particular attention to:
- Organ wydający (np. Prezydent Miasta, Burmistrz, Naczelnik US) i data wydania
- Znak sprawy / numer decyzji oraz identyfikator podatnika (NIP, PESEL)
- Rok/okres rozliczeniowy i wymiar podatku (kwota całkowita oraz podział na raty)
- Terminy płatności poszczególnych rat — record these as actions with a deadline
- Pouczenie o prawie do odwołania (termin i organ odwoławczy) — record it as an action with a deadline
""",
    },
    "dokument urzędowy": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: dokument urzędowy / Decyzja administracyjna / Pismo z urzędu.
Pay particular attention to:
- Instytucja nadawcza (ZUS, Urząd Skarbowy, Urząd Gminy itp.) i znak sprawy
- Główna treść postanowienia lub wezwania (co urząd nakazuje, zatwierdza lub o co prosi)
- Nieprzekraczalne terminy na odpowiedź, uzupełnienie braków lub odwołanie — record these as actions with a deadline
- Konsekwencje niedotrzymania terminów
""",
    },
    "PIT": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: PIT (Deklaracja podatkowa lub informacja od pracodawcy).
Pay particular attention to:
- Dokładny typ formularza (np. PIT-37, PIT-11, PIT-36, PIT-28, PIT-8C) oraz rok podatkowy
- Dane podatnika (PESEL/NIP, dane osobowe) oraz dane płatnika (pracodawcy)
- Kwoty kluczowe: przychód, koszty uzyskania przychodu, dochód, pobrana zaliczka na podatek
- W przypadku deklaracji rocznych: kwota nadpłaty lub kwota do zapłaty urzędowi
- Informacja czy dołączone jest UPO (Urzędowe Poświadczenie Odbioru) i jego numer referencyjny
""",
    },

    # === POJAZDY ===
    "Dokumentacja pojazdu": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Dokumentacja pojazdu (Dowód rejestracyjny, karta pojazdu, umowa kupna, zaświadczenie SKP).
Pay particular attention to:
- Numer rejestracyjny oraz Numer Identyfikacyjny Pojazdu (VIN)
- Dane właściciela/współwłaściciela pojazdu
- Marka, model, typ, rok produkcji oraz parametry techniczne (pojemność, moc kW)
- Ważne terminy: data następnego badania technicznego (przeglądu) — record it as an action with a deadline
""",
    },

    # === HR / PRACOWNICY ===
    "Dokumentacja pracownicza": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Dokumentacja pracownicza (Świadectwo pracy, zaświadczenie o zarobkach, badania BHP/medycyna pracy).
Pay particular attention to:
- Pracodawca i pracownik (dane identyfikacyjne)
- Okres zatrudnienia, wymiar czasu pracy, zajmowane stanowisko
- Tryb rozwiązania stosunku pracy (jeśli dotyczy)
- Daty ważności orzeczeń lekarskich lub szkoleń BHP — record expiration dates as actions with a deadline
""",
    },

    # === FINANSE, FAKTURY I PŁATNOŚCI ===
    "Faktura": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Faktura (VAT, zaliczkowa, korygująca, proforma).
Pay particular attention to:
- Numer faktury, data wystawienia, data sprzedaży i okres rozliczeniowy
- Pełne dane sprzedawcy oraz nabywcy (nazwy firm, numery NIP)
- Suma brutto, suma netto, podział na stawki podatku VAT (23%, 8%, 5%, zw, np)
- Termin płatności i numer konta bankowego (sprawdź obecność mechanizmu split payment)
- Numer zamówienia, umowy lub identyfikator klienta
""",
    },
    "Paragon": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Paragon (fiskalny, niefiskalny).
Pay particular attention to:
- Nazwa i NIP sprzedawcy, data oraz dokładna godzina zakupu
- Pozycje paragonu (towary/usługi), ceny jednostkowe i kwota łączna (brutto)
- Forma płatności (karta, gotówka, BLIK)
- Informacje o gwarancji lub prawa do zwrotu towaru
""",
    },
    "Rachunki": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Rachunki (Rachunek uproszczony, nota księgowa, dowód kasowy KP/KW).
Pay particular attention to:
- Wystawca, odbiorca, data wystawienia i kwota transakcji
- Tytuł płatności / za co wystawiono rachunek
- Informacja czy dokument potwierdza zapłatę, czy stanowi wezwanie do uregulowania należności
""",
    },
    "Potwierdzenie przelewu": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Potwierdzenie przelewu / dowód wpłaty.
Pay particular attention to:
- Dane nadawcy i odbiorcy (w tym numery rachunków bankowych)
- Data wykonania transakcji / data księgowania
- Kwota, waluta oraz tytuł przelewu
- Numer referencyjny operacji bankowej
""",
    },
    "Potwierdzenie salda": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Potwierdzenie salda / Uzgodnienie sald.
Pay particular attention to:
- Podmiot wysyłający zapytanie i podmiot potwierdzający
- Data (dzień), na który saldo zostało ustalone
- Kwota salda (Wn - winien, Ma - ma), wyszczególnienie faktur spornych lub niezapłaconych
- Termin na odesłanie podpisanego dokumentu — record it as an action with a deadline
""",
    },

    # === NIERUCHOMOŚCI I HARMONOGRAMY ===
    "Harmonogram": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Harmonogram (np. płatności rat, odbioru odpadów, realizacji projektu).
Pay particular attention to:
- Czego dotyczy harmonogram (numer umowy, adres nieruchomości, nazwa projektu)
- Tabela lub lista terminów i dat — przechwyć je jako zestaw działań z konkretnymi datami końcowymi (actions with deadlines)
- Kwoty przypisane do konkretnych terminów (jeśli to harmonogram spłat)
""",
    },
    "Rozrachunki mieszkaniowe": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Rozrachunki mieszkaniowe (Abonament za media, faktura za prąd/gaz, rozliczenie zużycia wody/ogrzewania od spółdzielni/wspólnoty).
Pay particular attention to:
- Zarządca nieruchomości/Dostawca mediów oraz dane lokalu (adres, numer klienta)
- Okres rozliczeniowy, wskazania liczników (początkowe i końcowe) oraz zużycie
- Kwota nadpłaty lub niedopłaty i termin jej uregulowania
- Wysokość nowych zaliczek/prognoz na kolejny okres wraz z terminami spłat
""",
    },

    # === MEDYCYNA I ZDROWIE ===
    "Karta informacyjna leczenia szpitalnego": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Karta informacyjna leczenia szpitalnego (Wypis ze szpitala).
Pay particular attention to:
- Dane szpitala/oddziału, dane pacjenta (imię, nazwisko, PESEL, adres)
- Okres hospitalizacji (data przyjęcia i data wypisu)
- Rozpoznanie główne i współistniejące (opisy oraz kody ICD-10)
- Zastosowane leczenie, wykonane zabiegi/operacje
- Epikryza (podsumowanie przebiegu leczenia)
- Dalsze zalecenia: dawkowanie leków, zwolnienie lekarskie (ZUS ZLA), terminy kontroli — record control visits as an action with a deadline
""",
    },
    "Karta Wizyty Ambulatoryjnej": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Karta Wizyty Ambulatoryjnej (Historia porady lekarskiej, wizyta w POZ lub poradni specjalistycznej).
Pay particular attention to:
- Placówka medyczna, lekarz, pacjent (PESEL) oraz dokładna data wizyty
- Powód zgłoszenia / wywiad lekarski oraz badanie przedmiotowe
- Postawiona diagnoza i zalecone leki/skierowania
""",
    },
    "Wynik badania medycznego": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Wynik badania medycznego (Laboratorium, RTG, USG, Rezonans, Tomografia).
Pay particular attention to:
- Typ badania, data wykonania i dane pacjenta
- Opis badania, wnioski diagnostyczne lub tabela z wynikami laboratoryjnymi (wychwyć wartości, jednostki oraz zakresy referencyjne/normy)
- Wszelkie adnotacje o konieczności pilnego kontaktu z lekarzem
""",
    },
    "Wypis ze szpitala": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Wypis ze szpitala.
Pay particular attention to:
- Identyczna procedura jak dla karty informacyjna leczenia szpitalnego.
- Skup się na datach pobytu, końcowych diagnozach (ICD-10) oraz receptach i zaleceniach domowych.
""",
    },

    # === UMOWY I UBEZPIECZENIA ===
    "Ubezpieczenie": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Ubezpieczenie (Polisa, OWU, potwierdzenie zawarcia umowy OC/AC/NNW/życie).
Pay particular attention to:
- Towarzystwo Ubezpieczeń, numer polisy, dane ubezpieczającego i ubezpieczowanego
- Przedmiot ubezpieczenia (auto, dom, życie, podróż)
- Okres trwania ochrony (początek i koniec) oraz kwota składki i terminy płatności rat
- Suma ubezpieczenia (limity odpowiedzialności) i udziały własne/franszyzy
- Klauzula automatycznego odnowienia umowy
""",
    },
    "umowa": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: umowa (ogólna, np. o dzieło, zlecenie, sprzedaży, abonamentowa).
Pay particular attention to:
- Data i miejsce zawarcia umowy, strony umowy (dane osobowe, firmy, NIP, PESEL)
- Przedmiot umowy oraz obowiązki stron
- Okres obowiązywania (czas określony/nieokreślony), warunki przedłużenia i okres wypowiedzenia — record deadlines as actions
- Wynagrodzenie, kary umowne i warunki odstąpienia
""",
    },
    "Umowa kredytowa": {
        "prompt": BASE_INSTRUCTIONS
        + """
Document type: Umowa kredytowa / pożyczki / leasingu.
Pay particular attention to:
- Instytucja finansowa (Bank/Pożyczkodawca) oraz Kredytobiorca
- Kwota kredytu/pożyczki, waluta, cel kredytowania
- Całkowity koszt kredytu, RRSO, prowizje i oprocentowanie (stałe/zmienne)
- Harmonogram spłat, wysokość raty i termin pierwszej płatności — record as action with a deadline
- Zabezpieczenia kredytu (hipoteka, weksel, ubezpieczenie spłaty)
""",
    },

    # === PROFIL DOMYŚLNY ===
    "_default": {
        "prompt": BASE_INSTRUCTIONS
        + """
The document type is not pre-classified. Determine it from the content and
record it in the "document_type" field using Polish terminology if possible.
In general, pay attention to:
- Sender, recipient, date, place
- Any deadlines, time limits and required actions
- Contract, case, or reference numbers
- Financial amounts, VAT breakdown, and payment terms
""",
    },
}
