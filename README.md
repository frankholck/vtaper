# V-Taper Coach — install + one-tap Garmin sync

## 1. Put the app online (GitHub Pages)
1. github.com -> New repository -> name it `vtaper` -> Create.
2. Upload ALL files from this zip, keeping the folder structure:
   - index.html, manifest.webmanifest, sw.js, the 3 icon PNGs (repo root)
   - garmin-sync/fetch_garmin.py
   - .github/workflows/garmin-sync.yml  (move it from garmin-sync/.github/workflows/ to the repo's own .github/workflows/ folder)
3. Settings -> Pages -> Deploy from branch -> main / root -> Save.
4. Live in ~1 min at: https://YOUR-USERNAME.github.io/vtaper/

## 2. Install on the phone
Open the URL in Chrome -> menu -> "Add to Home screen" -> Install.
(Samsung Internet: menu -> Add page to -> Home screen.)

## 3. Garmin morning sync
Garmin blocks password logins from GitHub's servers, so the sync signs in with a
saved Garmin session instead of the password.

1. One-time login on your own computer (PowerShell on Windows, Python 3.12+;
   if `python` is not found: `winget install Python.Python.3.12`, then open a
   new PowerShell window):

       curl.exe -sO https://raw.githubusercontent.com/frankholck/vtaper/main/garmin-sync/garmin_login.py
       python -m pip install -U garminconnect
       python garmin_login.py

   Type the Garmin email, password and (if asked) the code Garmin sends. The
   script copies the session to the clipboard.
2. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:
   - GARMIN_TOKENS = paste the clipboard
3. In the Coach app, tap **Collect Garmin now** (or Actions tab -> "Garmin
   morning sync" -> Run workflow). The Cloudflare relay starts the GitHub
   workflow and Coach waits for the new data automatically.
4. It writes garmin.json to the repo. The app fetches it on launch and
   shows the readiness card: sleep score, hours, HRV vs 7-day baseline,
   resting HR, body battery, plus a GREEN / AMBER / RED verdict. Scale weight
   is auto-logged into the body log.

If the workflow log says the Garmin session is missing or expired, repeat
steps 1-2. Changing the Garmin password also ends the session.

How the session is kept alive: Garmin can replace the refresh token on each
use, so every run stores the newest session in `garmin-sync/session.enc`,
encrypted with a key derived from the GARMIN_TOKENS secret. The file is useless
without that secret. GARMIN_EMAIL / GARMIN_PASSWORD are now only a last-resort
fallback and can be deleted.

## 4. Private Cloudflare relay
The public app never contains a GitHub token. `garmin-relay/` is deployed as the
Cloudflare Worker `vtaper-garmin-relay`. Add one encrypted Worker secret named
`GITHUB_TOKEN`. Use a fine-grained GitHub token limited to the `frankholck/vtaper`
repository with **Actions: Read and write** and no broader repository access.

The relay only accepts the Coach website origin and limits repeat starts for two
minutes. Do not commit the token or paste it into `index.html`.

## Private visual progress photos
- Open **Progress -> Visual Progress** to take or choose front, side, and back photos.
- Photo copies are compressed and saved only in the phone browser's private IndexedDB storage.
- Photos are never committed to GitHub, written to `garmin.json`, or uploaded by the app.
- Removing site data or uninstalling the PWA can delete the private copies, so keep originals in Samsung Gallery or Secure Folder.

## Choosing a workout
- The five workouts (Push, Pull, Legs + Core, Shoulder Cap + Back, Arms + Delt Blast) are no
  longer tied to weekdays. Pick any of them on any day from **Today** or the **Train** tab.
- Each one shows whether it is done, partly done or still open this week (Monday to Sunday).
  "Next up" is the workout you have gone longest without.
- Every exercise shows the weights and reps from the last time you did that workout, with a
  note on whether to stay or go up. Tap a session under **Progress -> Session History** to see
  its full weights.

## Online save
- **Plan -> Online save**: choose a passcode (8+ characters) and tap **Turn on online save**.
  Sessions, weights, habits and weigh-ins are then saved online after every change.
- On a new phone, install the app and turn on online save with the same passcode: the history
  is restored and joined with anything already on that phone.
- The data lives in the Supabase project `vtaper-coach` (not in this repo). It can only be read
  or written with the passcode; five wrong passcodes lock it for 15 minutes. The last saved
  state of each day is kept for 60 days as a safety net.
- Photos are not saved online; they stay on the phone.
- `online-save/setup.sql` is the one-time database setup. `.github/workflows/online-save-keepalive.yml`
  pings the store every two days so the free-tier project is not paused for inactivity.

Readiness logic:
- GREEN: full session as programmed
- AMBER: drop a set from non-priority lifts, keep all lateral volume
- RED:   walk + sauna only, or light laterals-only

## Notes
- PRIVACY: the repo is public (required for free GitHub Pages), so
  garmin.json (daily sleep/HRV numbers, no identity) is publicly
  reachable by anyone with the URL. If you want it private, make the
  repo private and enable Pages via a GitHub Pro plan instead.
- Garmin has no official personal API; the sync uses the community
  `garminconnect` library with your own Garmin session. Garmin can change
  its login at any time, which may require updating the library.
- App data (sets, habits, weigh-ins) lives on the phone. Don't clear
  site data for the app URL.
