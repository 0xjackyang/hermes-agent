---
name: google-workspace
description: Gmail, Calendar, Drive, Contacts, Sheets, and Docs integration via gws CLI (googleworkspace/cli). Uses OAuth2 with automatic token refresh via bridge script. Requires gws binary.
version: 2.0.0
author: Nous Research
license: MIT
required_credential_files:
  - path: google_token.json
    description: Legacy default Google OAuth2 token (backward-compatible fallback)
  - path: google_client_secret.json
    description: Default Google OAuth2 client credentials (downloaded from Google Cloud Console)
  - path: google_accounts.json
    description: Optional multi-account registry mapping aliases/routes to named Google tokens
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth, gws]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya]
---

# Google Workspace

Gmail, Calendar, Drive, Contacts, Sheets, and Docs — powered by `gws` (Google's official Rust CLI). The skill provides a backward-compatible Python wrapper that handles OAuth token refresh and delegates to `gws`.

## Architecture

```
google_api.py  →  gws_bridge.py  →  gws CLI
      ↓                ↓
google_account_registry.py
(account/route resolution against google_accounts.json + legacy fallback)
```

- `setup.py` handles OAuth2 (headless-compatible, works on CLI/Telegram/Discord)
- `google_account_registry.py` resolves `--account` / `--route` against `google_accounts.json`, named token files under `google-accounts/`, and the legacy `google_token.json` fallback
- `gws_bridge.py` refreshes the selected Hermes token and injects it into `gws` via `GOOGLE_WORKSPACE_CLI_TOKEN`
- `google_api.py` provides the same CLI interface as v1 while also forwarding `--account` / `--route` to the bridge

### Multi-account model

When `google_accounts.json` is present, the skill can route to named Google identities. Example shape:

```json
{
  "default": "satori",
  "accounts": {
    "satori": {
      "email": "satori@jackyang.com",
      "token_path": "google-accounts/satori.json",
      "client_secret_path": "google_client_secret.json"
    },
    "jack-electrum": {
      "email": "jack@electrum.id",
      "token_path": "google-accounts/jack-electrum.json",
      "client_secret_path": "google_client_secret.json"
    }
  },
  "routes": {
    "interactive_default": "satori",
    "electrum_docs_drive": "jack-electrum"
  }
}
```

Selection precedence is:
1. explicit `--account`
2. explicit `--route`
3. `HERMES_GOOGLE_ACCOUNT`
4. `HERMES_GOOGLE_ROUTE`
5. registry default / `interactive_default`
6. legacy `google_token.json`

## References

- `references/gmail-search-syntax.md` — Gmail search operators (is:unread, from:, newer_than:, etc.)

## Scripts

- `scripts/setup.py` — OAuth2 setup (run once to authorize)
- `scripts/google_account_registry.py` — Named-account / route resolution helper
- `scripts/gws_bridge.py` — Token refresh bridge to gws CLI
- `scripts/google_api.py` — Backward-compatible API wrapper (delegates to gws)

## Prerequisites

Install `gws`:

```bash
cargo install google-workspace-cli
# or via npm (recommended, downloads prebuilt binary):
npm install -g @googleworkspace/cli
# if npm global prefix points to a non-writable system dir like /usr,
# install into ~/.local instead:
npm install -g --prefix "$HOME/.local" @googleworkspace/cli
# or via Homebrew:
brew install googleworkspace-cli
```

If you used the user-local npm install, make sure `~/.local/bin` is on `PATH`.

Verify: `gws --version`

## First-Time Setup

The setup is fully non-interactive — you drive it step by step so it works
on CLI, Telegram, Discord, or any platform.

Define a shorthand first:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
GWORKSPACE_SKILL_DIR="$HERMES_HOME/skills/productivity/google-workspace"
PYTHON_BIN="${HERMES_PYTHON:-python3}"
if [ -x "$HERMES_HOME/hermes-agent/venv/bin/python" ]; then
  PYTHON_BIN="$HERMES_HOME/hermes-agent/venv/bin/python"
fi
GSETUP="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/setup.py"
```

### Step 0: Check if already set up

```bash
$GSETUP --check
# or check a specific named identity
$GSETUP --check --account jack-electrum
# or use a route from google_accounts.json
$GSETUP --check --route electrum_docs_drive
```

If it prints `AUTHENTICATED`, skip to Usage — setup is already done.

### Step 1: Triage — ask the user what they need

**Question 1: "What Google services do you need? Just email, or also
Calendar/Drive/Sheets/Docs?"**

- **Email only** → Use the `himalaya` skill instead — simpler setup.
- **Calendar, Drive, Sheets, Docs (or email + these)** → Continue below.

**Partial scopes**: Users can authorize only a subset of services. The setup
script accepts partial scopes and warns about missing ones.

**Question 2: "Does your Google account use Advanced Protection?"**

- **No / Not sure** → Normal setup.
- **Yes** → Workspace admin must add the OAuth client ID to allowed apps first.

### Step 2: Create OAuth credentials (one-time, ~5 minutes)

Tell the user:

> 1. Go to https://console.cloud.google.com/apis/credentials
> 2. Create a project (or use an existing one)
> 3. Enable the APIs you need (Gmail, Calendar, Drive, Sheets, Docs, People)
> 4. Credentials → Create Credentials → OAuth 2.0 Client ID → Desktop app
> 5. Download JSON and tell me the file path

Normal path-based import:

```bash
$GSETUP --client-secret /path/to/client_secret.json
```

If the user cannot provide a path but can paste the JSON content in chat, write it to a temporary file first, then import it:

```bash
cat >/tmp/google_client_secret.json <<'JSON'
{ ... pasted client secret JSON ... }
JSON
$GSETUP --client-secret /tmp/google_client_secret.json
rm -f /tmp/google_client_secret.json
```

This is useful in Discord/Telegram/chat flows where attachments are not accessible as local files from the agent runtime.

### Step 3: Get authorization URL

```bash
$GSETUP --auth-url
# or start auth for a specific identity
$GSETUP --auth-url --account jack-electrum
```

Send the URL to the user. After authorizing, they paste back the redirect URL or code.

**Important remote/SSH note:** the redirect URI is `http://localhost:1`, which is intentionally used only to carry the OAuth code back in the browser URL. On a remote machine or when opening the URL on a different device (for example a MacBook while Hermes runs over SSH on a server), the browser may show a localhost error such as `ERR_UNSAFE_PORT` or “This site can’t be reached.” That is expected. The user should simply copy either:
- the **full redirect URL** from the browser address bar, or
- just the **`code`** query parameter

and paste it back to Hermes.

### Step 4: Exchange the code

What matters is the browser URL after redirect. Copy either:
- the **full redirect URL**, or
- just the **`code`** parameter

Then give that value back to Hermes running on the remote machine.

### Step 4: Exchange the code

```bash
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED"
# or exchange for a specific named identity
$GSETUP --account jack-electrum --auth-code "THE_URL_OR_CODE_THE_USER_PASTED"
```

### Step 5: Verify

```bash
$GSETUP --check
```

Should print `AUTHENTICATED`. Token refreshes automatically from now on.

## Usage

All commands go through the API script:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
GWORKSPACE_SKILL_DIR="$HERMES_HOME/skills/productivity/google-workspace"
PYTHON_BIN="${HERMES_PYTHON:-python3}"
if [ -x "$HERMES_HOME/hermes-agent/venv/bin/python" ]; then
  PYTHON_BIN="$HERMES_HOME/hermes-agent/venv/bin/python"
fi
GAPI="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/google_api.py"
```

Identity-aware variants:

```bash
$GAPI --account jack-electrum sheets get SHEET_ID "Sheet1!A1:D10"
$GAPI --route electrum_docs_drive sheets get SHEET_ID "Sheet1!A1:D10"
```

### Gmail

```bash
$GAPI gmail search "is:unread" --max 10
$GAPI gmail get MESSAGE_ID
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"
$GAPI gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1>" --html
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
$GAPI gmail labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
```

### Calendar

```bash
$GAPI calendar list
$GAPI calendar create --summary "Standup" --start 2026-03-01T10:00:00+01:00 --end 2026-03-01T10:30:00+01:00
$GAPI calendar create --summary "Review" --start ... --end ... --attendees "alice@co.com,bob@co.com"
$GAPI calendar delete EVENT_ID
```

### Drive

```bash
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5
```

**Important scope note:** the current setup script requests `drive.readonly`, which is enough for Drive search but **not** for changing permissions/sharing files. If you need Hermes to share a Sheet/Drive file with another user, the Google OAuth token must be reissued with a broader Drive scope such as `drive.file` or `drive`.

### Contacts

```bash
$GAPI contacts list --max 20
```

### Sheets

```bash
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

#### Creating a new spreadsheet with tabs

The wrapper covers read/update/append well, but it does not provide a simple first-class helper for **creating a new spreadsheet with multiple tabs plus formatting/share steps**. In that case, prefer using `googleapiclient` directly against the stored Hermes token rather than fighting the `gws` surface.

Reusable pattern:
1. load credentials from the profile token file (typically `~/.hermes/profiles/<profile>/google_token.json`)
2. refresh if expired
3. build both:
   - `sheets = build("sheets", "v4", credentials=creds)`
   - `drive = build("drive", "v3", credentials=creds)`
4. create spreadsheet with initial tabs via `spreadsheets().create(...)`
5. populate each tab with `values().update(...)`
6. format/freeze/autosize via `spreadsheets().batchUpdate(...)`
7. share with Drive `permissions().create(...)`
8. verify with a read-back call on the created ranges

Minimal Python shape:

```python
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = Path("/home/.../.hermes/profiles/<profile>/google_token.json")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    TOKEN_PATH.write_text(creds.to_json())

sheets = build("sheets", "v4", credentials=creds)
drive = build("drive", "v3", credentials=creds)

spreadsheet = sheets.spreadsheets().create(body={
    "properties": {"title": "My Sheet"},
    "sheets": [
        {"properties": {"title": "Tab 1"}},
        {"properties": {"title": "Tab 2"}},
    ],
}, fields="spreadsheetId,spreadsheetUrl,sheets(properties(sheetId,title))").execute()
```

Practical findings:
- use `values().update(...)` rather than append when you want deterministic tab layouts
- freeze the header row and set wrap formatting in one `batchUpdate`
- for share flows, `drive.file` is sufficient if the file was created by this token/app flow
- after creation, do a read-back of the first few rows to confirm data landed correctly

Use this direct API route when the user asks for a brand-new structured Sheet deliverable rather than incremental edits to an existing file.

### Sharing a Sheet / Drive file

If you need Hermes to share a Sheet or Drive file with another user, `drive.readonly` is not enough. Google Drive `permissions.create` requires one of:
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/drive`

Prefer `drive.file`.

#### One-time scope fix

1. Ensure the setup script requests `drive.file` in its `SCOPES` list.
2. If needed, add the same scope on the Google Cloud OAuth consent screen.
3. Re-run OAuth consent so the stored token actually gains the new scope.

Important: editing the script alone does **not** upgrade an existing token. You must do a fresh consent flow.

#### Re-consent flow

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
GWORKSPACE_SKILL_DIR="$HERMES_HOME/skills/productivity/google-workspace"
PYTHON_BIN="${HERMES_PYTHON:-python3}"
GSETUP="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/setup.py"

$GSETUP --auth-url
```

Open the URL, approve the scopes, then copy back either:
- the full `http://localhost:1/?...` redirect URL, or
- just the `code` parameter

The browser may show a localhost error page after redirect. That is expected. The important part is the URL in the address bar.

Exchange it:

```bash
$GSETUP --auth-code "PASTED_URL_OR_CODE"
$GSETUP --check
```

Verify the token contains `drive.file` before retrying the share.

#### Share via Drive permissions API

```bash
GBRIDGE="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/gws_bridge.py"
$GBRIDGE drive permissions create \
  --params '{"fileId":"FILE_ID","sendNotificationEmail":true}' \
  --json '{"type":"user","role":"writer","emailAddress":"user@example.com"}' \
  --format json
```

Then verify:

```bash
$GBRIDGE drive permissions list \
  --params '{"fileId":"FILE_ID","fields":"permissions(id,emailAddress,role,type)"}' \
  --format json
```

### Docs

Recommended scope for sharing:

```text
https://www.googleapis.com/auth/drive.file
```

If sharing fails with `403 insufficient authentication scopes`, do this:
1. add `drive.file` to the scope list in `scripts/setup.py`
2. ensure the OAuth client in Google Cloud Console allows that scope
3. run `setup.py --auth-url`
4. have the user approve the new consent screen
5. exchange the returned URL/code with `setup.py --auth-code ...`
6. retry the Drive permissions call

For Drive-side sharing after reauth, use the bridge directly:

```bash
GBRIDGE="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/gws_bridge.py"
$GBRIDGE drive permissions create \
  --params '{"fileId":"FILE_ID","sendNotificationEmail":true}' \
  --json '{"type":"user","role":"writer","emailAddress":"user@example.com"}' \
  --format json
```

Then verify:

```bash
$GBRIDGE drive permissions list \
  --params '{"fileId":"FILE_ID","fields":"permissions(id,emailAddress,role,type)"}' \
  --format json
```

### Docs

```bash
$GAPI docs get DOC_ID
```

### Direct gws access (advanced)

For operations not covered by the wrapper, use `gws_bridge.py` directly:

```bash
GBRIDGE="$PYTHON_BIN $GWORKSPACE_SKILL_DIR/scripts/gws_bridge.py"
$GBRIDGE calendar +agenda --today --format table
$GBRIDGE gmail +triage --labels --format json
$GBRIDGE drive +upload ./report.pdf
$GBRIDGE sheets +read --spreadsheet SHEET_ID --range "Sheet1!A1:D10"
```

## Output Format

All commands return JSON via `gws --format json`. Key output shapes:

- **Gmail search/triage**: Array of message summaries (sender, subject, date, snippet)
- **Gmail get/read**: Message object with headers and body text
- **Gmail send/reply**: Confirmation with message ID
- **Calendar list/agenda**: Array of event objects (summary, start, end, location)
- **Calendar create**: Confirmation with event ID and htmlLink
- **Drive search**: Array of file objects (id, name, mimeType, webViewLink)
- **Sheets get/read**: 2D array of cell values
- **Docs get**: Full document JSON (use `body.content` for text extraction)
- **Contacts list**: Array of person objects with names, emails, phones

Parse output with `jq` or read JSON directly.

## Rules

1. **Never send email or create/delete events without confirming with the user first.**
2. **Check auth before first use** — run `setup.py --check`, and when identity matters, use `--account` or `--route` explicitly.
3. **Do not assume a 403 means missing access or scopes.** It can also mean the wrong Google identity was selected. Confirm the account context before asking the user to reshare a file.
4. **Prefer explicit identity selection for cross-org work.** Example: Electrum Docs/Sheets/Drive should use `--account jack-electrum` or `--route electrum_docs_drive` instead of relying on ambient defaults.
5. **Use the Gmail search syntax reference** for complex queries.
6. **Calendar times must include timezone** — ISO 8601 with offset or UTC.
7. **Respect rate limits** — avoid rapid-fire sequential API calls.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `NOT_AUTHENTICATED` | Run setup Steps 2-5 for the selected account, e.g. `$GSETUP --account jack-electrum --auth-url` |
| `REFRESH_FAILED` | Token revoked — redo Steps 3-5 for the same selected account/route |
| `gws: command not found` | Install: `npm install -g @googleworkspace/cli` |
| `HttpError 403` | Could be missing scope **or the wrong Google identity**. First re-run with `--account` / `--route` and confirm the account context before changing scopes or resharing files. |
| `HttpError 403: Access Not Configured` | Enable API in Google Cloud Console |

### Common real-world scope fix: sharing a Sheet/Drive file

If a Drive share operation such as `drive permissions create` fails with:

```text
403 insufficientPermissions
```

the current token likely has `drive.readonly` but not a writable Drive scope.

Google's Drive docs require one of these for `permissions.create`:
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/drive`

Preferred fix:
1. Add `https://www.googleapis.com/auth/drive.file` to the `SCOPES` list in `scripts/setup.py`
2. If needed, add the same scope on the Google Cloud OAuth consent screen
3. Re-run auth:
   ```bash
   $GSETUP --revoke
   $GSETUP --auth-url
   $GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED"
   $GSETUP --check
   ```
4. Retry the share operation

Prefer `drive.file` over full `drive` unless broad Drive management is actually needed.
| Advanced Protection blocks auth | Admin must allowlist the OAuth client ID |

### Sharing Google Sheets / Drive files

If Hermes can create/read a Sheet but fails to share it with an error like:

```text
403 insufficient authentication scopes
```

the usual cause is that the token only has `drive.readonly`.

Google Drive `permissions.create` requires one of:
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/drive`

Recommended fix:
1. Add `https://www.googleapis.com/auth/drive.file` to the `SCOPES` list in `scripts/setup.py`
2. Ensure the same scope is allowed on the Google Cloud OAuth consent screen if needed
3. Re-run consent:

```bash
$GSETUP --revoke
$GSETUP --auth-url
# user opens URL, approves, and pastes back redirect URL or code
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED"
$GSETUP --check
```

Then retry the share operation.

Use `drive.file` rather than full `drive` unless broad Drive management is actually needed.

## Revoking Access

```bash
$GSETUP --revoke
```
