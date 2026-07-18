import os
import json
import asyncio
from typing import Callable, Awaitable
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CREDS_PATH = os.path.join(BASE_DIR, "credentials.json")
DEFAULT_TOKEN_PATH = os.path.join(BASE_DIR, "token.json")


class GmailClient:
    def __init__(
        self,
        credentials_path=DEFAULT_CREDS_PATH,
        token_path=DEFAULT_TOKEN_PATH,
        token_json_override: str | None = None,
        on_token_refresh: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.credentials_path = credentials_path
        self.token_path = token_path
        # Per-user token JSON string (e.g. from GMAIL_TOKEN_JSON_ALICE env)
        self.token_json_override = token_json_override
        self.on_token_refresh = on_token_refresh
        self.creds = None
        self._service = None

    async def _get_service(self):
        if not self._service:
            self.creds = await self._authenticate()
            self._service = build("gmail", "v1", credentials=self.creds)
        return self._service

    async def _authenticate(self):
        creds = None

        # 1. Per-user override (from DB or env)
        if self.token_json_override:
            try:
                token_info = json.loads(self.token_json_override)
                creds = Credentials.from_authorized_user_info(token_info, SCOPES)
            except Exception as e:
                print(f"Failed to load per-user token JSON override: {e}")
        # 2. Global env (legacy single-user)
        if not creds:
            token_env = os.environ.get("GMAIL_TOKEN_JSON")
            if token_env and not self.token_json_override:
                try:
                    token_info = json.loads(token_env)
                    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
                except Exception as e:
                    print(f"Failed to load token from GMAIL_TOKEN_JSON: {e}")
        # 3. Per-user token file
        if not creds and os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                # Refreshing is a blocking HTTP call, run in executor
                await asyncio.to_thread(creds.refresh, Request())
                
                # Call the async callback to persist the new token (e.g. to DB)
                if self.on_token_refresh:
                    await self.on_token_refresh(creds.to_json())
                    
                # Persist refresh when using files (not pure env override)
                if not self.token_json_override and not os.environ.get("GMAIL_TOKEN_JSON"):
                    try:
                        os.makedirs(os.path.dirname(os.path.abspath(self.token_path)) or ".", exist_ok=True)
                        with open(self.token_path, "w") as token:
                            token.write(creds.to_json())
                    except OSError:
                        # Fallback for read-only filesystems
                        pass
            else:
                # Interactive OAuth (local setup only; not for headless cloud)
                creds_env = os.environ.get("GMAIL_CREDENTIALS_JSON")
                if creds_env:
                    try:
                        client_config = json.loads(creds_env)
                        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                    except Exception as e:
                        raise ValueError(
                            f"Failed to load client config from GMAIL_CREDENTIALS_JSON: {e}"
                        )
                else:
                    if not os.path.exists(self.credentials_path):
                        raise FileNotFoundError(
                            f"{self.credentials_path} not found. Please set GMAIL_CREDENTIALS_JSON "
                            "env var or place credentials.json in the project root."
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_path, SCOPES
                    )

                # Use a fixed port to match the Google Cloud Console redirect URI
                creds = await asyncio.to_thread(flow.run_local_server, port=8080)

                if self.on_token_refresh:
                    await self.on_token_refresh(creds.to_json())

                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.token_path)) or ".", exist_ok=True)
                    with open(self.token_path, "w") as token:
                        token.write(creds.to_json())
                except OSError:
                    # Fallback for read-only filesystems
                    pass

        return creds

    async def list_messages(self, query="", max_results=10):
        try:
            service = await self._get_service()
            results = await asyncio.to_thread(
                service.users().messages().list(
                    userId="me", q=query, maxResults=max_results
                ).execute
            )
            return results.get("messages", [])
        except HttpError as error:
            print(f"An error occurred: {error}")
            return []

    async def get_message(self, message_id):
        try:
            service = await self._get_service()
            return await asyncio.to_thread(
                service.users().messages().get(userId="me", id=message_id, format="full").execute
            )
        except HttpError as error:
            print(f"An error occurred: {error}")
            return None


if __name__ == "__main__":
    async def main():
        try:
            client = GmailClient()
            print("Successfully authenticated with Gmail.")
            messages = await client.list_messages(max_results=5)
            print(f"Found {len(messages)} messages.")
        except Exception as e:
            print(f"Error: {e}")
            
    asyncio.run(main())
