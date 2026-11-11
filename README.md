# Nighthawks license backend (Flask + SQLite)

## Deploy on PythonAnywhere
1. Files tab: upload this folder to `/home/YOURUSER/nighthawks_backend`.
2. Bash console:
   ```
   cd ~/nighthawks_backend && python3 app.py init
   ```
   It creates the database and prints your **admin token** (also saved in `admin_token.txt`). Keep it secret.
3. Web tab: Add a new web app, choose Manual configuration, pick a Python version with Flask installed (or `pip install --user Flask`).
4. Open the WSGI file from the Web tab and replace its contents with:
   ```python
   import sys, os
   path = '/home/YOURUSER/nighthawks_backend'
   if path not in sys.path:
       sys.path.insert(0, path)
   os.environ['NH_ALLOWED_ORIGINS'] = 'https://your-website-origin'   # your site, not *
   os.environ['NH_BASE_URL'] = 'https://YOURUSER.pythonanywhere.com'
   from app import app as application
   ```
5. Press Reload. Test: `https://YOURUSER.pythonanywhere.com/api/v1/ping`

## Public endpoints
| Method | URL | Purpose |
|---|---|---|
| POST | `/api/v1/license/validate` | body `{"key","hwid"}`. Activates a fresh key (timer starts) and returns app, version, plan, expiry |
| GET | `/api/v1/fields/<app-slug>/<version or latest>/<field>` | returns one field. Headers `X-License-Key` and `X-HWID` unless the field is marked public |
| GET | `/api/v1/download/mediafire?url=<share link>` | redirects straight to the real MediaFire file (falls back to the normal MediaFire page if the direct link can't be found). Add `&format=json` to get `{"url"}` instead |
| GET | `/api/v1/plans` | subscription plans (name and length) for the customer view |
| POST | `/api/v1/license/info` | body `{"key"}`. Read-only look at a key (app, version, plan, status, expiry). Does not activate or bind a device |

Error replies are `{"ok":false,"code":"...","message":"..."}` with codes: invalid_key, revoked, hwid_required, hwid_mismatch, expired, not_activated, wrong_app, rate_limited.

## Admin endpoints (header `X-Admin-Token: <token>`)
- `GET /api/v1/admin/check`
- Plans: `GET|POST /api/v1/admin/plans` (`{"name","value","unit"}`), `DELETE /api/v1/admin/plans/<id>`
- Apps: `GET|POST /api/v1/admin/apps` (`{"name","version"}`), `DELETE /api/v1/admin/apps/<id>`
- Release version: `POST /api/v1/admin/apps/<id>/versions` (`{"version","note"}`), copies the current fields
- Fields: `GET /api/v1/admin/apps/<id>/versions/<version|latest>/fields` (each field comes back with its request `url`),
  `PUT /api/v1/admin/apps/<id>/fields/<name>` (`{"value","public"}`), `DELETE` same path
- Licenses: `GET /api/v1/admin/licenses`, `POST /api/v1/admin/licenses/generate`
  (`{"app_id","plan_id" or "custom":{"value","unit"},"hwid_mode":"hwid|open","quantity","note"}`),
  `POST .../licenses/<id>/revoke | restore | reset-hwid`, `DELETE .../licenses/<id>`

Units: minutes, hours, days, weeks, months (30 days), lifetime.

## Discord (admin endpoints)
Each Discord channel has its own webhook. Webhook URLs are stored in the backend database and are never returned to the browser (only a masked version).
- `GET /api/v1/admin/discord`: channels, routing and toggles
- `POST /api/v1/admin/discord/channels` `{"name","webhook_url"}`; `PUT|DELETE .../channels/<id>`; `POST .../channels/<id>/test`
- `PUT /api/v1/admin/discord` `{"notify_new_item","notify_price_change","route_new_item":[ids],"route_price_change":[ids]}`: choose which channels get each automatic post
- `POST /api/v1/admin/discord/send` `{"channel_ids":[ids],"content","title","allow_mentions"}`: custom message to one or more channels
- `POST /api/v1/admin/discord/notify` `{"event":"new_item|price_change","title","category","type","price_fjd","price_usdt","old_fjd","old_usdt","description","store_url"}`: the site calls this automatically

An older single-webhook setup is converted automatically into a channel named "Main".

Free PythonAnywhere accounts can only reach whitelisted sites. If sending fails with "could not reach Discord", check that discord.com is allowed for your account.
