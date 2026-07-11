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
1. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:
   - GARMIN_EMAIL = your Garmin Connect login email
   - GARMIN_PASSWORD = your Garmin Connect password
2. Actions tab -> "Garmin morning sync" -> Run workflow (manual test).
3. It writes garmin.json to the repo. The app fetches it on launch and
   shows the readiness card: sleep score, hours, HRV vs 7-day baseline,
   resting HR, body battery, plus a GREEN / AMBER / RED verdict.
4. There is no fixed schedule. In the Coach app, tap **Collect Garmin now**.
   The Cloudflare relay starts the GitHub workflow and Coach waits for the new
   data automatically. Scale weight is auto-logged into the body log.

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
  `garminconnect` library with your own credentials, same approach as
  your daily coaching email. If Garmin prompts MFA, the Action log
  will show a login error — reuse the token approach from your
  existing coaching workflow if that happens.
- App data (sets, habits, weigh-ins) lives on the phone. Don't clear
  site data for the app URL.
