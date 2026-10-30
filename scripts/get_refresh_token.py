"""Obtiene el refresh token de Google Drive (se ejecuta una sola vez, en tu PC).

Uso:
    pip install -r requirements-dev.txt
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_refresh_token.py
"""
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> None:
    try:
        client_id, client_secret = os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"]
    except KeyError as exc:
        sys.exit(f"Falta la variable de entorno {exc}")
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    print("\nGOOGLE_REFRESH_TOKEN=" + creds.refresh_token)


if __name__ == "__main__":
    main()
