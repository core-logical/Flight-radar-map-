from flask import Flask, render_template, jsonify
import requests
import time
import os

app = Flask(__name__)

# OpenSky API
OPENSKY_URL = "https://opensky-network.org/api/states/all"

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)

FLIGHTS_URL = (
    "https://opensky-network.org/api/flights/aircraft"
)

# Dane logowania ustaw w konfiguracji uruchomienia PyCharm.
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

# Cache bieżących pozycji samolotów.
cache = {
    "time": 0,
    "aircraft": []
}

# Cache tokenu OAuth2.
token_cache = {
    "access_token": None,
    "expires_at": 0
}

# Cache historii lotów: maksymalnie jeden wynik na samolot
# przez 6 godzin.
history_cache = {}

# Sesja HTTP pozwala ponownie wykorzystywać połączenia.
session = requests.Session()


def opensky_token():
    """Zwraca ważny token OAuth2 albo None, jeśli brak konfiguracji."""

    if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
        return None

    now = time.time()

    if (
        token_cache["access_token"]
        and now < token_cache["expires_at"] - 60
    ):
        return token_cache["access_token"]

    response = session.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET
        },
        timeout=15
    )
    response.raise_for_status()

    data = response.json()

    token_cache["access_token"] = data["access_token"]
    token_cache["expires_at"] = now + int(
        data.get("expires_in", 1800)
    )

    return token_cache["access_token"]


def naglowki_opensky():
    """Przygotowuje nagłówki do zapytań OpenSky."""

    token = opensky_token()

    if token:
        return {
            "Authorization": f"Bearer {token}"
        }

    return {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/samoloty")
def samoloty():
    """Zwraca aktualne pozycje samolotów z cache'em 15 sekund."""

    now = time.time()

    if now - cache["time"] < 15:
        return jsonify(cache["aircraft"])

    try:
        response = session.get(
            OPENSKY_URL,
            headers=naglowki_opensky(),
            timeout=20
        )
        response.raise_for_status()

        data = response.json()
        aircraft = []

        for s in (data.get("states") or []):
            # Bez współrzędnych nie da się umieścić samolotu na mapie.
            if s[5] is None or s[6] is None:
                continue

            aircraft.append({
                "icao24": s[0],
                "callsign": (s[1] or "").strip(),
                "country": s[2],
                "longitude": s[5],
                "latitude": s[6],
                "altitude": s[7],
                "baro_altitude": s[7],
                "velocity": s[9],
                "heading": s[10],
                "vertical_rate": s[11],
                "on_ground": s[8],
                "squawk": s[14]
            })

        cache["aircraft"] = aircraft
        cache["time"] = time.time()

        return jsonify(aircraft)

    except (requests.RequestException, ValueError) as error:
        app.logger.warning(
            "Nie udało się pobrać pozycji z OpenSky: %s",
            error
        )

        # Jeśli API nie działa, zwróć ostatnie zapisane dane.
        return jsonify(cache["aircraft"])


@app.route("/api/lot/<icao24>")
def historia_lotu(icao24):
    """
    Zwraca ostatni dostępny zapis historyczny dla samolotu.
    Nie oznacza to, że jest to cel jego aktualnego lotu.
    """

    icao24 = icao24.lower().strip()

    # Walidacja identyfikatora transpondera.
    if (
        len(icao24) != 6
        or any(c not in "0123456789abcdef" for c in icao24)
    ):
        return jsonify({
            "available": False,
            "message": "Nieprawidłowy identyfikator ICAO24."
        }), 400

    now = time.time()

    # Nie odpytuj ponownie API przez 6 godzin.
    cached = history_cache.get(icao24)

    if cached and now - cached["time"] < 21600:
        return jsonify(cached["data"])

    try:
        token = opensky_token()

        if not token:
            return jsonify({
                "available": False,
                "message": (
                    "Historia lotów wymaga skonfigurowania "
                    "OPENSKY_CLIENT_ID i OPENSKY_CLIENT_SECRET "
                    "w PyCharm."
                )
            }), 503

        end = int(now)
        begin = end - 172000

        response = session.get(
            FLIGHTS_URL,
            params={
                "icao24": icao24,
                "begin": begin,
                "end": end
            },
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=20
        )

        # Brak dostępnych lotów dla tego zapytania.
        if response.status_code == 404:
            flights = []
        else:
            response.raise_for_status()
            flights = response.json() or []

        # Najnowszy dostępny zapis według czasu zakończenia.
        flights = sorted(
            flights,
            key=lambda flight: flight.get("lastSeen") or 0,
            reverse=True
        )

        flight = flights[0] if flights else {}

        departure = flight.get("estDepartureAirport")
        arrival = flight.get("estArrivalAirport")

        result = {
            "available": bool(flight),
            "data_type": "history",
            "callsign": (
                (flight.get("callsign") or "").strip()
            ),
            "departure_airport": departure,
            "arrival_airport": arrival,
            "message": (
                "To ostatni dostępny zapis historyczny. "
                "Nie musi dotyczyć aktualnego lotu. "
                "Kody lotnisk pochodzą z danych OpenSky."
                if flight
                else
                "Brak dostępnego zapisu historycznego "
                "dla tego samolotu w sprawdzonym okresie."
            )
        }

        history_cache[icao24] = {
            "time": time.time(),
            "data": result
        }

        return jsonify(result)

    except (requests.RequestException, ValueError) as error:
        app.logger.warning(
            "Nie udało się pobrać historii lotu %s: %s",
            icao24,
            error
        )

        return jsonify({
            "available": False,
            "message": (
                "Nie udało się pobrać historii z OpenSky. "
                "Sprawdź połączenie, konfigurację API "
                "i komunikat w terminalu PyCharm."
            )
        }), 502


if __name__ == "__main__":
    app.run(debug=True)