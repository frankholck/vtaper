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

## 5. Garmin In Focus email
Three times a day (07:30, 14:00 and 22:30 Dubai time) GitHub pulls the day's Garmin
numbers and emails them with charts: heart rate, Body Battery, HRV, sleep in detail
(score, stages, and heart rate / HRV / Pulse Ox / respiration through the night),
stress, steps and weight. It uses the same saved Garmin session as the morning sync.

1. Create a Gmail app password (needs 2-step verification on the Google account):
   https://myaccount.google.com/apppasswords
2. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:
   - GMAIL_ADDRESS = the Gmail address (it is both sender and recipient)
   - GMAIL_APP_PASSWORD = the 16-letter app password (spaces do not matter)
3. To send one straight away: Actions tab -> "Garmin In Focus email" -> Run workflow.

If an email does not arrive, open the run in the Actions tab and look at which step
failed:
- "Check the Gmail sign-in": one of the two Gmail secrets is missing or wrong.
- "Pull Garmin data and send the email": Garmin did not answer. If the saved Garmin
  session has ended you get a short email saying so; repeat steps 1-2 of section 3.

Good to know:
- If last night's sleep has not synced by 07:30, the email says so instead of showing
  an older night. With no weigh-in today it shows the last one with its date.
- If nothing at all has synced for today yet (for example the watch is on an earlier
  date while travelling), the email shows the day before and says so at the top.
- The repo is public, so the run log is public. The script only logs which sections
  had data, never a value, and the numbers go nowhere except the email.
- GitHub starts timed runs late, sometimes by hours. So the workflow starts nine runs
  around each send time: the first to start in the hour before waits and sends on the
  minute, the others stop. If none starts in time, the first one after the send time
  sends straight away, at most once and at most 90 minutes late. That is why the
  Actions tab shows many short "Garmin In Focus email" runs each day.
- To change the times, edit `SEND_TIMES_DUBAI` in `garmin-sync/in_focus_slots.py` and
  move the matching cron lines in `.github/workflows/garmin-in-focus.yml`.
- To look at the email without Garmin or Gmail (made-up numbers):

      cd garmin-sync
      python -m pip install matplotlib
      python sample_garmin_day.py sample.json
      python in_focus_report.py --from-file sample.json --out-dir preview

  then open `preview/email.html`. Tests: `python -m unittest test_in_focus_report`.

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
