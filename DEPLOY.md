# Putting this on your iPhone

The short version: the app is a small web server. It has to live somewhere
that is always on, because it checks for new episodes while your phone is
asleep. Your iPhone then opens it like an app.

An iPhone cannot be that "somewhere" — iOS shuts background apps down, so
anything running on the phone itself would stop the moment you switched away.
What you need is a small always-on computer. Renting one costs about $5 a
month and is set up entirely from your phone's browser. No terminal, no
commands.

---

## Step 1 — rent somewhere for it to live

[Railway](https://railway.app) is the easiest from a phone: it reads your
GitHub repository, builds the app itself, and gives you a web address. Sign in
with your GitHub account.

Two things about Railway worth knowing before you start:

- It costs around **$5/month**. There is a small free trial credit to try it.
- You **must add a Volume** (step 3). Without one, everything you have watched
  is erased every time the app restarts. This is the single most important
  step on the page.

Other services work too — Render, Fly.io, a NAS at home, a spare laptop. The
steps below are Railway-specific, but the ideas transfer: build the
`Dockerfile`, attach persistent storage at `/data`, set the environment
variables.

## Step 2 — create the project

1. In Railway, tap **New Project** → **Deploy from GitHub repo**.
2. Give it access to your `TV` repository and pick it.
3. Choose the branch this work is on if it asks.

Railway finds the `Dockerfile` in the repository and starts building. The first
build takes a couple of minutes.

## Step 3 — add the Volume (do not skip this)

A volume is a disk that survives restarts. Your watch history lives on it.

Volumes are **not** in the service's Variables or Settings tab — they are
created on the project canvas, the screen showing your service as a box:

1. Close the service panel with the **✕** in its top right, so you can see the
   canvas.
2. Tap **+ Create** (on a phone; on a desktop it is right-click on the canvas,
   or `⌘K` → "volume").
3. Choose **Volume**.
4. Pick the **TV** service to attach it to.
5. Set the mount path to exactly:

   ```
   /data
   ```

   A leading slash, no dot. `./data` is a different place — inside the
   container, wiped on every restart.

Without this, your library is wiped on every restart and no backup can save
you, because the backups live on the same disk.

The Trial plan includes 0.5 GB of volume storage, which is far more than this
needs — a large library plus its backups is a few tens of megabytes.

## Step 4 — set the variables

Open the service → **Variables**.

Railway may offer a list of **Suggested Variables** it found by reading this
repository's `.env.example`. **Do not add them all.** Those values are the
ones that suit a laptop, and two of them break a hosted deploy. Add exactly
these two, by hand:

| Name | Value |
| --- | --- |
| `TV_PASSWORD` | something only you know |
| `TV_DATA_DIR` | `/data` |

Everything else has a sensible default. In particular:

- **Never set `TV_PORT`.** Railway assigns a port through its own `PORT`
  variable and routes traffic to it; the app reads that automatically. Setting
  `TV_PORT` overrides it and nothing can reach the app.
- **`TV_DATA_DIR` must be `/data`**, matching the volume's mount path — not
  `./data`, which points inside the container.

If you already added the suggested list, remove `TV_PORT` with the **✕** beside
it and correct `TV_DATA_DIR` to `/data`.

Railway redeploys automatically after a variable change.

## Step 5 — get your web address

In **Settings** → **Networking** → **Generate Domain**. You get something like
`tv-production-a1b2.up.railway.app`.

Open that address in Safari on your iPhone. You should see the password screen.
Log in.

## Step 6 — put it on your home screen

In Safari, with the app open:

1. Tap the **Share** button (the square with an arrow, at the bottom).
2. Scroll down and tap **Add to Home Screen**.
3. Name it whatever you like — "TV" works.

You now have an icon on your home screen. Opening it launches the app
fullscreen with no address bar. That is as close to the old TV Time app as a
web app gets, and it is how most people ran TV Time replacements anyway.

## Step 7 — import your history

Your TV Time export needs to reach the app. The file picker in Safari can read
from iCloud Drive and Files, so put `gdprdata.zip` somewhere in Files first.

1. Open the app → **More** → **Import from TV Time**.
2. Tap **Choose File** and pick the zip.
3. Tap **Preview**. Nothing is saved yet — you get a report of what it found.
4. If it looks right, tap **Import for real**.

The import takes several minutes for a large library, because it looks up every
show. It keeps running even if you close the app, and the progress bar picks up
where it was when you come back.

Afterwards: search for **Serial** in your shows and remove it. TV Time had it
filed under an id TVmaze does not recognise, so it matches the wrong thing.

---

## Once it is running

**Backups happen on their own** — every 24 hours, keeping the last 14, on the
volume. Every so often, open **More → Backups** and tap **Download** on the
newest one so a copy also lives in your iCloud Drive. A volume is not a backup
if the whole account goes away.

**New episodes** appear on the **New** tab with a number badge. The app checks
every 6 hours by itself.

**Tidying up your library**: **Shows** → set the filter to **Never started** →
tap **Select** → **Select all** → **Archive**. That clears the shows you
followed years ago and never watched out of your Up Next list without deleting
anything.

## Moving off the cloud later

If you get a computer, a NAS or a Raspberry Pi later, nothing here traps you:

1. On the new machine, run the app (`docker compose up -d`, or the systemd
   unit in `deploy/`).
2. On the old one, **More → Backups** → **Download** the newest snapshot.
3. On the new one, **More → Backups** → **Restore from file** → pick it.

You get the same library: every show, every watch date, your favorites,
priority pins and archived shows. Then cancel the cloud subscription. The
backup file is the migration format, so this works in either direction and as
many times as you like.

## If something goes wrong

- **"dockerfile invalid: docker VOLUME ... is not supported"** — Railway
  manages its own storage and rejects any `VOLUME` line in a Dockerfile. There
  is none any more; make sure your deployment is building the latest commit.
- **"Application failed to respond"** right after deploying — usually the build
  is still running, so check the **Deployments** tab. If the build succeeded
  and it still says this, make sure `TV_PORT` is *not* set in Variables: it
  overrides the port Railway routes to.
- **Everything vanished after a restart** — the volume is missing, or
  `TV_DATA_DIR` does not match the volume's mount path. Both must be `/data`,
  with a leading slash. Re-check step 3.
- **Cannot find where to add a volume** — it is not in the service panel. Close
  that panel first; volumes are created from the project canvas behind it.
- **The password screen never appears** — `TV_PASSWORD` is not set. Anyone with
  the address can see your library until it is.
- **Shows are not updating** — **More → Check now** forces it.

If you get stuck, paste the error into Claude Code along with which step you
were on.
