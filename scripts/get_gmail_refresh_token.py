"""One-time, LOCAL-ONLY setup script. Run this yourself on your own machine — it opens a
Google consent screen in your browser, you approve access for the account that should hold
the drafts (relianmfg@gmail.com), and it prints a refresh token to store as the
GMAIL_REFRESH_TOKEN GitHub secret. Nobody else can run this step for you: Google's OAuth
consent has to be approved by you, in your own browser, logged into that account.

Prerequisites (see README.md "Gmail API setup" for the click-by-click version):
  1. In Google Cloud Console, create a project and enable the Gmail API.
  2. Create an OAuth 2.0 Client ID of type "Desktop app". Note the client_id/client_secret.
  3. Set them as GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in your local .env (or export them).

Usage:
  python scripts/get_gmail_refresh_token.py
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    # gmail.readonly dropped 2026-08-05 — see scripts/common/gmail_client.py. Re-run this
    # script once under the reduced scope; that mint should be the last one needed before
    # gmail.compose's lighter Sensitive-scope verification takes it out of Testing mode.
]

load_dotenv()


def main() -> None:
    client_config = {
        "installed": {
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=0)

    print("\nSuccess. Store this as the GMAIL_REFRESH_TOKEN GitHub secret:\n")
    print(credentials.refresh_token)


if __name__ == "__main__":
    main()
