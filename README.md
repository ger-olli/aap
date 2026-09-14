# Amayama Price Fetcher

Ruft Amayama-Preise für eine Liste von Teilenummern ab und schreibt sie als JSON.

## Ziel

- Lieferland: Germany
- Währung: EUR
- Alle sichtbaren Preisangebote je Teilenummer
- Herkunft (`source`) je Angebot
- `OoP: 1`, wenn Amayama den Artikel als `out of production` kennzeichnet
- Keine nachträgliche Währungsumrechnung: es werden die von Amayama in EUR dargestellten Preise übernommen

## Eingabe

`parts.txt`, eine Teilenummer pro Zeile:

```text
4342335010
2360017032
4340160081
```

Standardmäßig wird `toyota` als Marke verwendet. Eine andere Marke kann über `--brand` gesetzt werden.

## Ausgabe

`output.json`:

```json
{
  "4342335010": {
    "OoP": 0,
    "prices": [
      {
        "source": "UAE",
        "price": 2.10,
        "currency": "EUR"
      }
    ]
  },
  "4720160550": {
    "OoP": 1,
    "prices": []
  }
}
```

Falls ein Abruf technisch fehlschlägt, erhält nur dieser Artikel zusätzlich ein `error`-Feld; der restliche Lauf wird fortgesetzt.

## Lokal ausführen

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python fetch_prices.py
```

Optional:

```bash
python fetch_prices.py --input parts.txt --output output.json --brand toyota --delay 4
```

## GitHub Actions

Der Workflow `.github/workflows/fetch-prices.yml` läuft automatisch zweimal pro Woche (Montag und Donnerstag) und kann zusätzlich über **Actions → Fetch Amayama prices → Run workflow** manuell gestartet werden.

Nach erfolgreichem Lauf wird `output.json` automatisch ins Repository committed.

## Hinweis

Die Länder- und Währungseinstellung wird über die echte Amayama-Seite in einer persistenten Playwright-Browser-Session gesetzt. Falls Amayama seine Oberfläche oder Selektoren ändert, kann eine Anpassung nötig werden. Der Fetcher bricht die Germany/EUR-Initialisierung bewusst mit einer Fehlermeldung ab, statt stillschweigend Preise aus einer falschen Länder- oder Währungseinstellung als EUR auszugeben.
