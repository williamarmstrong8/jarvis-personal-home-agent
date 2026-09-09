"""Weather data via Open-Meteo — free, no API key."""
import requests

WMO_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "icy fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    95: "thunderstorms", 99: "severe thunderstorms",
}


def get_weather(latitude: float, longitude: float, city: str) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  latitude,
        "longitude": longitude,
        "current":   ["temperature_2m", "apparent_temperature", "weather_code",
                      "wind_speed_10m", "precipitation", "relative_humidity_2m"],
        "daily":     ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"],
        "temperature_unit":  "fahrenheit",
        "wind_speed_unit":   "mph",
        "precipitation_unit": "inch",
        "timezone":    "America/New_York",
        "forecast_days": 1,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data    = resp.json()
    current = data["current"]
    daily   = data["daily"]

    code        = current.get("weather_code", 0)
    description = WMO_CODES.get(code, "variable conditions")

    return {
        "city":          city,
        "condition":     description,
        "temperature":   round(current["temperature_2m"]),
        "feels_like":    round(current["apparent_temperature"]),
        "humidity":      current["relative_humidity_2m"],
        "wind_speed":    round(current["wind_speed_10m"]),
        "high":          round(daily["temperature_2m_max"][0]),
        "low":           round(daily["temperature_2m_min"][0]),
        "precipitation": daily["precipitation_sum"][0],
        "summary": (
            f"{description.capitalize()} in {city}. "
            f"Currently {round(current['temperature_2m'])}°F, "
            f"feels like {round(current['apparent_temperature'])}°F. "
            f"High of {round(daily['temperature_2m_max'][0])}°F, "
            f"low of {round(daily['temperature_2m_min'][0])}°F."
        ),
    }
