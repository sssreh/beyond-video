# Deploying bv-web (and the bv-* pipeline) on a Synology NAS

A step-by-step walkthrough for running `bv-web` in Container Manager, browsable at `http://<nas-ip>:19373`, plus getting real trips into it. The pipeline is split across two machines (see "Split across two machines" below step 6): `bv-download`/`bv-config` run on the NAS itself, while `bv-generate`/`bv-export` - the GPU-heavy, ffmpeg-heavy parts - run natively on Christer's PC, reaching the NAS's archive/trips folders over SMB. `bv-web` itself covers increment 1 only (browse/watch trips, owner/viewer login) - see `WORKING_CONTEXT.md` for what's not built yet.

## Layout on the NAS

Everything lives under one folder, `/volume1/beyond-video`:

```
/volume1/beyond-video/          <- this repo, checked out directly (not nested)
    Dockerfile                  <- bv-web's image
    Dockerfile.cli              <- bv-download/bv-config's image (nothing heavier - see below)
    docker-compose.yml
    pyproject.toml
    src/
    ...
    data/
        trips/                  <- bv-export --target output - bv-web browses it (read-only); written by Christer's PC over SMB
        config/                 <- bv-web's web-users.cfg (accounts file)
        archive/                <- bv-download's target - the raw camera archive; also read by Christer's PC over SMB
        camera-config/          <- bv-config's camera .cfg files (e.g. Kirby.cfg)
```

`data/` isn't part of the git repo (see `.gitignore`) - it's created once, on the NAS, and holds everything that needs to persist across container rebuilds (accounts, camera config, the raw archive, and exported trips). `data/archive` and `data/trips` also need to be reachable from Christer's PC over SMB (see step 7) - if `/volume1/beyond-video` itself isn't already a browsable network share, set that up in **Control Panel -> Shared Folder** first.

## 1. One-time host prep

In DSM:

1. **Package Center** -> install **Container Manager** if it isn't already installed.
2. **Control Panel -> Terminal & SNMP** -> enable **SSH service**. You'll use SSH for the one-off setup/account-creation commands below; the containers themselves don't need SSH.
3. Optional but recommended for easy updates later: **Package Center** -> install **Git Server** (this gives you a real `git` binary over SSH, not just DSM's own tools). If you'd rather not install anything else, skip this and use the "no git" option in step 2 below.

Your SSH user likely won't have direct access to the Docker socket - every `docker-compose`/`docker` command below is written with `sudo` in front of it for that reason. If your account can't `sudo` at all, do the equivalent steps through Container Manager's GUI instead.

## 2. Get the code onto the NAS

SSH in first: `ssh <your-dsm-user>@<nas-ip>`

**With git** (recommended - makes updates a one-line `git pull` later):

```
mkdir -p /volume1/beyond-video
cd /volume1/beyond-video
git init
git remote add origin https://github.com/sssreh/beyond-video.git
git fetch
git checkout main
git config core.autocrlf false
```

`git clone https://github.com/sssreh/beyond-video.git .` looks simpler and would also avoid the nesting problem (the trailing `.` means "clone *into* this already-existing folder," not "create a new `beyond-video` folder inside it") - but Synology shared folders aren't actually empty even when File Station shows nothing in them: DSM auto-creates a hidden `@eaDir` housekeeping folder (and sometimes `#recycle`) at the root of every shared folder, and `git clone` refuses to clone into any directory that isn't genuinely empty. Check with `ls -la /volume1/beyond-video` if curious - that's normally all that's there. `git init` + `fetch` + `checkout` doesn't have that restriction, so it's the one to use here.

The `git config core.autocrlf false` at the end matters too: without it, files checked out on the NAS get converted to CRLF line endings, which then look "modified" against this repo's LF-normalized blobs on every future `git pull` - turning a routine update into a merge conflict on files you never touched. Set it once, right after the initial checkout.

**Without git**: zip your local checkout, upload it via File Station into `/volume1/beyond-video`, and extract it so the files listed above land directly in that folder (not inside a subfolder the zip creates - check the zip's top level before extracting, or extract elsewhere and move the contents up one level).

## 3. Create the data folders

Still in the SSH session:

```
mkdir -p /volume1/beyond-video/data/trips
mkdir -p /volume1/beyond-video/data/config
mkdir -p /volume1/beyond-video/data/archive
mkdir -p /volume1/beyond-video/data/camera-config
```

All four start empty. `data/trips` is what `bv-web` browses and what `bv-export` (run from Christer's PC over SMB, see step 7) writes into - leaving it empty for now is fine, `bv-web` just shows "No trips found yet."

## 4. Build and start bv-web

Either through Container Manager's GUI (**Project** -> **Create** -> pick `/volume1/beyond-video` as the path; it auto-detects `docker-compose.yml`) or over SSH:

```
cd /volume1/beyond-video
sudo docker-compose build bv-web
sudo docker-compose up -d bv-web
```

Note the hyphen: Synology's Container Manager only puts the old standalone `docker-compose` 1.x CLI on the SSH `$PATH`, not the newer `docker compose` (space) v2 plugin - `docker compose ...` will fail with a confusing `unknown shorthand flag` error rather than a clear "not found." Check which one you have with `docker-compose --version` (v1, hyphenated) vs `docker compose version` (v2 plugin) if unsure; every command in this doc uses the hyphenated form to match Christer's actual NAS.

`bv-web`'s image only installs the `web` extra (fastapi/uvicorn/jinja2) - it does not pull in faster-whisper/pyannote.audio/argostranslate/torch, so this build should be quick (so should `bv-cli`'s, in step 7 - neither image installs the heavy ML stack; see "Split across two machines" for why). `sudo docker-compose ps` should show `beyond-video-web` as `Up`.

## 5. Create your owner account

One-time, over SSH:

```
sudo docker-compose run --rm bv-web adduser christer --role owner --users-file /data/config/web-users.cfg
```

Prompts for a password twice. This writes `data/config/web-users.cfg` on the host, which survives container rebuilds. Repeat with `--role viewer` later for family members. (`docker-compose run` rather than `docker exec` here since `bv-web`'s main container is running the long-lived `serve` process, which crash-loops if the accounts file is still empty at boot - `run` spins up a separate one-off container from the same image/volumes instead of touching that one.)

## 6. Verify

From a browser on the same network: `http://<nas-ip>:19373`. Log in with the account from step 5. You should land on the trip list, showing "No trips found yet" until step 7 below.

If it doesn't load, check DSM's own firewall (**Control Panel -> Security -> Firewall**) isn't blocking port 19373, and `sudo docker-compose logs -f bv-web` from `/volume1/beyond-video` for errors.

## 7. Feeding it real trips

### Split across two machines

The camera reaches the NAS's network directly, so `bv-download`/`bv-config` run there. But `bv-generate --transcribe/--translate/--diarize` (faster-whisper/pyannote.audio/argostranslate - all much happier with a GPU) and `bv-export` (ffmpeg-heavy) run natively on Christer's PC instead, where GPU acceleration and the full toolchain are already set up and proven fast - see `WORKING_CONTEXT.md`'s earlier "GPU auto-detect + CPU fallback" work. The PC reaches the NAS's `data/archive` (to read) and `data/trips` (to write) over an SMB-mapped network drive, so nothing needs copying or syncing by hand - `bv-generate`/`bv-export` just point straight at the mapped drive letters.

This is why `Dockerfile.cli`/the `bv-cli` service stays deliberately narrow: it only ever runs `bv-download` and `bv-config`, neither of which needs ffmpeg or any ML library, so the image is small and fast to build - not the multi-GB torch-pulling image an earlier version of this doc described.

### On the NAS: download the archive

**Build the image once:**

```
cd /volume1/beyond-video
sudo docker-compose build bv-cli
```

**Set up the camera** (one-time; re-run later to edit):

```
sudo docker-compose run --rm bv-cli bv-config Kirby --config-dir /data/config
```

This is `bv-config`'s interactive wizard - name, endpoints (tried in order), and the target download path. For **Target (download path)**, answer `/data/archive` - that's the folder mounted to `./data/archive` on the host, which is where `bv-download` will write raw recordings.

**Download from the camera:**

```
sudo docker-compose run --rm bv-cli bv-download Kirby --config-dir /data/config
```

Safe to re-run repeatedly (only fetches what's new). Once you're happy it works, this is the one worth scheduling (cron, or a Synology Task Scheduler job) - add `--yes --trace` for unattended runs, since `--yes` skips the interactive "does this range look right?" confirmation a scheduled task can't answer, and `--trace` gives you something to check in the job's log:

```
sudo docker-compose run --rm bv-cli bv-download Kirby --config-dir /data/config --yes --trace
```

### On your PC: enrich and export

Map the NAS as a network drive first (**This PC -> Map network drive** in Windows Explorer, or `net use`), pointing at `\\<nas-ip>\beyond-video\data` (or wherever DSM's share path resolves to) - say it lands on `Z:\`. Then `data\archive` and `data\trips` are just `Z:\archive` and `Z:\trips`.

**Enrich recordings** (optional but recommended before export - see `docs/PIPELINE.md`):

```
bv-generate Z:\archive --get-duration --transcribe --srt
```

Add `--translate` / `--diarize` as wanted; both need real setup of their own (`bv-lang install` for translation packages, `--hf-token` for diarization) - see `docs/man/bv-generate.md`. This runs with your PC's normal `bv-generate` install (GPU and all) - nothing Docker-specific here.

**Export trips** (this is what makes them show up in `bv-web`):

```
bv-export Z:\archive --target Z:\trips --map --stitch --stitch-layout rearview_mirror
```

`Z:\trips` here maps to the *same* host folder (`./data/trips`) that `bv-web`'s container has mounted on the NAS - no separate copy step. The moment this command finishes, refresh `bv-web`'s trip list and the new trip is there.

`bv-lang install`, `bv-ls`, and anything else that only touches the archive/trips (not the camera itself) can run from your PC the same way, against the mapped drive.

## Updating later

```
cd /volume1/beyond-video
git pull
sudo docker-compose up -d --build bv-web
sudo docker-compose build bv-cli
```

(`bv-cli` has no long-running container to restart - just rebuilding the image is enough, since it's only ever used via `docker-compose run`.) `data/` is untouched by this - accounts, camera config, archive, and trips all survive.

## Restarting / logs

```
sudo docker-compose restart bv-web
sudo docker-compose logs -f bv-web
sudo docker-compose down          # stops and removes bv-web's container (data/ is unaffected)
```

## See also

- `WORKING_CONTEXT.md` - what bv-web does and doesn't do yet (increment 1: browse/watch only).
- `docs/PIPELINE.md` - the bv-download -> bv-generate -> bv-export pipeline that produces what `data/trips` holds.
