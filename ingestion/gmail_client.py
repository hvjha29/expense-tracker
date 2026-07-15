import os.path
import base64
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CREDS_PATH = os.path.join(BASE_DIR, 'credentials.json')
DEFAULT_TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

class GmailClient:
    def __init__(self, credentials_path=DEFAULT_CREDS_PATH, token_path=DEFAULT_TOKEN_PATH):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.creds = None
        self._service = None

    def _get_service(self):
        if not self._service:
            self.creds = self._authenticate()
            self._service = build('gmail', 'v1', credentials=self.creds)
        return self._service

    def _authenticate(self):
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first time.
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"{self.credentials_path} not found. Please download it from "
                        "Google Cloud Console and place it in the project root."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES)
                # Use a fixed port to match the Google Cloud Console redirect URI
                creds = flow.run_local_server(port=8080)
            
            # Save the credentials for the next run
            with open(self.token_path, 'w') as token:
                token.write(creds.to_json())
        
        return creds

    def list_messages(self, query='', max_results=10):
        try:
            service = self._get_service()
            results = service.users().messages().list(
                userId='me', q=query, maxResults=max_results
            ).execute()
            return results.get('messages', [])
        except HttpError as error:
            print(f'An error occurred: {error}')
            return []

    def get_message(self, message_id):
        try:
            service = self._get_service()
            return service.users().messages().get(
                userId='me', id=message_id, format='full'
            ).execute()
        except HttpError as error:
            print(f'An error occurred: {error}')
            return None

if __name__ == '__main__':
    # Test script
    try:
        client = GmailClient()
        print("Successfully authenticated with Gmail.")
        messages = client.list_messages(max_results=5)
        print(f"Found {len(messages)} messages.")
    except Exception as e:
        print(f"Error: {e}")
