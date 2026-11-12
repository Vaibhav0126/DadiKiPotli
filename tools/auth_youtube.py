"""One-shot YouTube OAuth consent for Module 5.

    .venv/bin/python -m tools.auth_youtube
    .venv/bin/python -m tools.auth_youtube --reauth

Opens a browser, asks you to authorize upload access and read-only access (view
counts for the feedback loop), then writes the refresh token to
secrets/youtube_token.json (path overridable via YOUTUBE_TOKEN_PATH).

A token issued before the read-only scope existed is still valid for uploads,
so it keeps working but cannot read stats. Use --reauth to request the new
scope. It overwrites the token file with a fresh one. Afterwards, update the
YOUTUBE_TOKEN_JSON GitHub secret with the new file's contents.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.publish_engine import YOUTUBE_SCOPES, youtube_credentials  # noqa: E402

log = logging.getLogger("auth_youtube")


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube OAuth for uploads and view counts")
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="request the scopes again and overwrite the token, even if one exists",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()
    cfg = load_config()

    secrets = cfg.youtube_client_secrets()
    token = cfg.youtube_token_path()
    log.info("client secrets: %s", secrets)
    log.info("token will be saved to: %s", token)

    if not secrets.exists():
        log.error(
            "Missing %s\n"
            "1. Create a Google Cloud project and enable YouTube Data API v3\n"
            "2. Create an OAuth client (Desktop app) and download the JSON\n"
            "3. Save it as %s\n"
            "4. Re-run this command",
            secrets,
            secrets,
        )
        return 1

    try:
        if args.reauth:
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(creds.to_json(), encoding="utf-8")
            log.info("wrote a fresh token with scopes: %s", ", ".join(YOUTUBE_SCOPES))
        else:
            creds = youtube_credentials(cfg, interactive=True)
    except Exception as exc:  # noqa: BLE001
        log.error("OAuth failed: %s", exc)
        return 1

    if not creds.refresh_token:
        log.warning(
            "No refresh_token returned. Revoke access at "
            "https://myaccount.google.com/permissions and re-run, "
            "or ensure the OAuth client is Desktop and prompt=consent."
        )
    log.info("YouTube upload auth OK. Token: %s", token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
