# Deploying bv-web (and the bv-* pipeline) on a Synology NAS

A step-by-step walkthrough for running `bv-web` in Container Manager, browsable at `http://<nas-ip>:19373`, plus getting real trips into it. `bv-download`/`bv-config` always run on the NAS; `bv-generate`/`bv-export` can run on the NAS via the `bv-cli` service (self-contained), natively on Christer's PC over an SMB-mapped drive (faster - GPU acceleration), or straight from `bv-web`'s own "Generate assets"/"Export trips" job pages in the browser - see step 7's "Three ways to run bv-generate/bv-export" for all three. `bv-web`'s image and volume mounts now match `bv-cli`'s (full toolchain, read-write archive/trips) precisely so that third option works - see `WORKING_CONTEXT.md` for the NAS failure that drove this.

## Layout on the NAS

Everything lives under one folder, `/volume1/beyond-video`:

```
/volume1/beyond-video/          <- this repo, checked out directly (not nested)
    Dockerfile                  <- bv-web's image
    Dockerfile.cli              <- the full pipeline's image (bv-download/bv-generate/bv-export/...)
    docker-compose.yml
    pyproject.toml
    src/
    ...
    data/
        trips/                  <- bv-export --target output - read-write from all three places that can write it: bv-cli, Christer's PC over SMB, and bv-web's own "Export trips" job
        config/                 <- bv-web's web-users.cfg (accounts file)
        archive/                <- bv-download's target - the raw camera archive; read-write from bv-cli, Christer's PC over SMB, and bv-web's own "Download recordings"/"Generate assets" jobs
        camera-config/          <- bv-config's camera .cfg files (e.g. Kirby.cfg) - bv-web reads/writes this too (see below), not just bv-cli
```

`data/` isn't part of the git repo (see `.gitignore`) - it's created once, on the NAS, and holds everything that needs to persist across container rebuilds (accounts, camera config, the raw archive, and exported trips). `data/archive` and `data/trips` also need to be reachable from Christer's PC over SMB if using the PC path for `bv-generate`/`bv-export` (see step 7) - if `/volume1/beyond-video` itself isn't already a browsable network share, set that up in **Control Panel -> Shared Folder** first.

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
mkdir -p /volume1/beyond-video/data/logs
mkdir -p /volume1/beyond-video/data/argos-translate
```

All six start empty. `data/argos-translate` holds argostranslate's downloaded language packages (`bv-generate --translate`/`bv-lang`, both triggerable from `bv-web`'s own job runner too) - both services' `ARGOS_PACKAGES_DIR=/data/argos-translate/packages` environment variable points argostranslate's own storage there (argostranslate's documented override, not something Beyond Video's code configures itself - see `docker-compose.yml`'s comment on it), the same persistence pattern as `BEYOND_VIDEO_LOGS_DIR` below. Without it, a language pack installed via `bv-lang install` would need reinstalling after every `docker-compose build`/container recreation. `data/logs` holds the persistent output log (`core/joblog.py` - one gzip-rotated-monthly transcript covering every `bv-*` command's output, whether triggered from `bv-web`'s own job runner or typed directly into `docker-compose run bv-cli ...`) - both services' `BEYOND_VIDEO_LOGS_DIR=/data/logs` environment variable points at this same host folder, the same pattern `BEYOND_VIDEO_CONFIG_DIR` already uses for camera configs below. `data/trips` is what `bv-web` browses and what `bv-export` writes into, whether run on the NAS or from Christer's PC over SMB (see step 7) - leaving it empty for now is fine, `bv-web` just shows "No trips found yet." `data/camera-config` is mounted into `bv-web`'s own container too (`docker-compose.yml`'s `BEYOND_VIDEO_CONFIG_DIR=/data/camera-config` environment variable, read by `core/camera_config.py`'s `default_config_dir()`) - without it, `bv-web` has no persistent `$HOME` inside its container and can't see any camera `.cfg` file `bv-config` wrote, whether from `bv-cli` or from bv-web's own job-trigger form: the camera pick-list on the job forms shows empty, and the archive browser 404s every camera id. Same host folder either way, no extra setup needed - this is just why it's mounted into both containers rather than only `bv-cli`'s.

`data/archive` is *also* mounted into `bv-web`'s container (read-write - its own job runner writes here too when it triggers `bv-download`/`bv-generate`, not just the archive browser reading from it), separately from `data/camera-config` above - a camera's `.cfg` `archive` field points into `data/archive` (see step 6's "Archive (download path)" answer), and `bv-web`'s archive-browser feature (`/archive` routes) reads recordings straight from that path, not from `data/trips`. Without this mount, the camera picker and camera-config work fine but every camera's archive page says "No recordings found in this camera's archive yet." even once `bv-download` has actually written files there.

## 4. Build and start bv-web

Either through Container Manager's GUI (**Project** -> **Create** -> pick `/volume1/beyond-video` as the path; it auto-detects `docker-compose.yml`) or over SSH:

```
cd /volume1/beyond-video
sudo docker-compose build bv-web
sudo docker-compose up -d bv-web
```

The `build` step here is slow the first time - `bv-web`'s image now carries the same faster-whisper/pyannote.audio/argostranslate (and torch, transitively) dependencies `bv-cli`'s image does, plus `ffmpeg` and the transformers/qwen-vl-utils scene-description dependencies `bv-scribe` needs, since `bv-web`'s own job runner can trigger `bv-generate`/`bv-export`/`bv-download`/`bv-scribe` directly (see step 7). Expect several minutes and a multi-GB image; that's normal, not stuck.

Note the hyphen: Synology's Container Manager only puts the old standalone `docker-compose` 1.x CLI on the SSH `$PATH`, not the newer `docker compose` (space) v2 plugin - `docker compose ...` will fail with a confusing `unknown shorthand flag` error rather than a clear "not found." Check which one you have with `docker-compose --version` (v1, hyphenated) vs `docker compose version` (v2 plugin) if unsure; every command in this doc uses the hyphenated form to match Christer's actual NAS.

`sudo docker-compose ps` should show `beyond-video-web` as `Up`.

## 5. Create your owner account

One-time, over SSH:

```
sudo docker-compose run --rm bv-web adduser christer --role owner
```

Prompts for a password twice. No `--users-file` flag needed - `docker-compose.yml`'s `BEYOND_VIDEO_USERS_FILE=/data/config/web-users.cfg` environment variable (read by `web/users.py`'s `default_users_path()`) already points both `adduser` and `serve` at the same file by default, so this writes `data/config/web-users.cfg` on the host, which survives container rebuilds. (Without that variable, `adduser` would silently fall back to `$HOME/beyond-video-data/.config/web-users.cfg` - `/root/beyond-video-data/...` inside the container, a path nothing mounts, so the account would vanish on the next `docker-compose run` while `serve` never saw it either - this bit Christer once before the variable was added; see `WORKING_CONTEXT.md`.) Repeat with `--role viewer` later for family members. (`docker-compose run` rather than `docker exec` here since `bv-web`'s main container is running the long-lived `serve` process, which crash-loops if the accounts file is still empty at boot - `run` spins up a separate one-off container from the same image/volumes instead of touching that one.)

## 6. Verify

From a browser on the same network: `http://<nas-ip>:19373`. Log in with the account from step 5. You should land on the trip list, showing "No trips found yet" until step 7 below.

If it doesn't load, check DSM's own firewall (**Control Panel -> Security -> Firewall**) isn't blocking port 19373, and `sudo docker-compose logs -f bv-web` from `/volume1/beyond-video` for errors.

## 7. Feeding it real trips

### Three ways to run bv-generate/bv-export

`bv-download`/`bv-config` always run on the NAS (the camera reaches its network directly). For `bv-generate`/`bv-export` there are three options, and they're not exclusive - use whichever fits a given moment:

- **On the NAS**, via the `bv-cli` service (below) - works standalone, good for scheduled/unattended exports that shouldn't depend on the PC being on.
- **On Christer's PC**, natively, reaching the NAS's `data/archive`/`data/trips` over an SMB-mapped network drive - faster (GPU acceleration, already proven to work there - see `WORKING_CONTEXT.md`'s "GPU auto-detect + CPU fallback" work), good for anything time-sensitive or GPU-hungry (`--transcribe`/`--diarize` in particular).
- **From a browser**, via `bv-web`'s own "Generate assets"/"Export trips"/"Scene description" job pages - no SSH or `docker-compose run` needed, good for a quick one-off from a phone or another PC. Runs inside the `bv-web` container itself (CPU-only, same as the `bv-cli` path - not GPU-accelerated the way the PC path is), which is why `bv-web`'s image now carries the same full toolchain `bv-cli`'s does (plus the scene-description extra `bv-cli`'s image also has) rather than just the lightweight `web` extra.

All three write into the exact same `data/trips`/`data/archive` folders, so there's nothing to reconcile between them - a trip exported from the NAS CLI, the browser, or the PC shows up in `bv-web` identically.

### On the NAS: the full pipeline

**Build the image once** (this one's slow - faster-whisper/pyannote.audio pull in torch, expect several minutes and a multi-GB image; that's normal, not stuck):

```
cd /volume1/beyond-video
sudo docker-compose build bv-cli
```

**Archive already lives somewhere else on the NAS?** `docker-compose.yml`'s `bv-cli` service maps `./data/archive` (i.e. `/volume1/beyond-video/data/archive`) to `/data/archive` inside the container by default. If recordings already live in a different Shared Folder (Christer's case: `/volume1/Dashcam/files`), don't edit `docker-compose.yml` itself to point elsewhere - a direct edit there is a tracked file, so it'd get silently wiped by the next `git fetch && git reset --hard origin/main` (see step 2's note on why `reset --hard` is used for updates). Instead, create a second file, `docker-compose.override.yml`, next to `docker-compose.yml` - docker-compose automatically layers it on top with no extra flags needed, and being untracked (already in `.gitignore`), it survives every future update:

```
cd /volume1/beyond-video
cat > docker-compose.override.yml <<'EOF'
services:
  bv-cli:
    volumes:
      - /volume1/Dashcam/files:/data/archive
      - ./data/trips:/data/trips
      - ./data/camera-config:/data/config
  bv-web:
    volumes:
      - /volume1/Dashcam/files:/data/archive
EOF
```

All three of `bv-cli`'s volumes are repeated here (not just the archive one) since compose merges a service's `volumes:` list as a whole, not entry-by-entry - leaving the other two out would drop them, not keep them. `bv-web`'s override only needs the one line since it only has this single volume to redirect - its other two (`data/trips`, `data/camera-config`) already point at the right place by default. Skipping the `bv-web` block here is an easy mistake: `bv-web`'s own `./data/archive:/data/archive` mount (see `docker-compose.yml`) points at `/volume1/beyond-video/data/archive` unless overridden too, which is a different, empty folder from `bv-cli`'s real archive - the archive browser would still say "No recordings found" even though `bv-cli` can see everything fine (and no `:ro` suffix on either side now - `bv-web` writes into this same folder too, via its own "Download recordings"/"Generate assets" jobs). Verify the merge did what's expected before running anything for real:

```
sudo docker-compose config
```

Check the printed `bv-cli` *and* `bv-web` services' `volumes:` blocks both show `/volume1/Dashcam/files:/data/archive` and nothing pointing at `./data/archive` (or `/volume1/beyond-video/data/archive`) - if an old mount is still there too, or `/data/archive` isn't there at all on either service, adjust the override file and re-check before moving on. With `/volume1/Dashcam/files` as the archive, `bv-config`'s **Archive (download path)** answer below still stays `/data/archive` - that's the *container path* every `bv-*` command reads/writes, regardless of which host folder it's mapped to.

**Set up the camera** (one-time; re-run later to edit):

```
sudo docker-compose run --rm bv-cli bv-config Kirby --config-dir /data/config
```

This is `bv-config`'s interactive wizard - name, endpoints (tried in order), and the archive download path (plus an optional Target directory for `bv-export`, which you can leave blank here). For **Archive (download path)**, answer `/data/archive` - that's the folder mounted to `./data/archive` on the host, which is where `bv-download` will write raw recordings.

**Download from the camera:**

```
sudo docker-compose run --rm bv-cli bv-download Kirby --config-dir /data/config
```

Safe to re-run repeatedly (only fetches what's new). Once you're happy it works, this is the one worth scheduling (cron, or a Synology Task Scheduler job) - add `--yes --trace` for unattended runs, since `--yes` skips the interactive "does this range look right?" confirmation a scheduled task can't answer, and `--trace` gives you something to check in the job's log:

```
sudo docker-compose run --rm bv-cli bv-download Kirby --config-dir /data/config --yes --trace
```

**Enrich recordings** (optional but recommended before export - see `docs/PIPELINE.md`; slow on the NAS's CPU-only hardware for `--transcribe`/`--diarize` - the PC path below is faster for those):

```
sudo docker-compose run --rm bv-cli bv-generate /data/archive --get-duration --transcribe --srt
```

Add `--translate` / `--diarize` as wanted; both need real setup of their own (`bv-lang install` for translation packages, `--hf-token` for diarization) - see `docs/man/bv-generate.md`.

**Export trips** (this is what makes them show up in `bv-web`):

```
sudo docker-compose run --rm bv-cli bv-export /data/archive --target /data/trips --map --stitch --stitch-layout rearview_mirror
```

`/data/trips` here is the *same* host folder `bv-web`'s container has mounted - no copying, no syncing. The moment this finishes, refresh `bv-web`'s trip list and the new trip is there.

`bv-lang install`, `bv-ls`, and any other `bv-*` command work the same way - `sudo docker-compose run --rm bv-cli <command> <args...>`.

### On your PC: the faster path for enrich/export

Map the NAS as a network drive (**This PC -> Map network drive** in Windows Explorer, or `net use`), pointing at `\\<nas-ip>\beyond-video\data` (or wherever DSM's share path resolves to) - say it lands on `Z:\`. Then `data\archive` and `data\trips` are just `Z:\archive` and `Z:\trips`. Run `bv-generate`/`bv-export` exactly as you would locally, just pointed at the mapped drive:

```
bv-generate Z:\archive --get-duration --transcribe --srt
bv-export Z:\archive --target Z:\trips --map --stitch --stitch-layout rearview_mirror
```

Nothing Docker-specific here - this is your PC's normal `bv-generate`/`bv-export` install, GPU and all, just reading/writing over the network instead of a local path. `Z:\trips` maps to the same `data/trips` folder the NAS-side path above writes into, so the two are fully interchangeable trip by trip.

## Updating later

```
cd /volume1/beyond-video
git fetch origin
git reset --hard origin/main
sudo docker-compose up -d --build bv-web
sudo docker-compose build bv-cli
```

`git fetch` + `git reset --hard origin/main`, not `git pull`: this repo's history was rewritten once already (purging some large committed video clips - see `WORKING_CONTEXT.md`), and a plain `git pull` on a checkout from before that rewrite fails with "divergent branches"/"forced update" - the old and new histories share old commits but diverge after the rewrite point, so there's nothing for a merge or rebase to reconcile. `reset --hard origin/main` sidesteps that entirely by just pointing the local branch straight at whatever `origin/main` is, discarding any local commits (there shouldn't be any on a NAS checkout - see step 2) rather than trying to merge two unrelated histories. If it happens again after a future rewrite, the same two commands fix it.

(`bv-cli` has no long-running container to restart - just rebuilding the image is enough, since it's only ever used via `docker-compose run`.) `data/` is untouched by this - accounts, camera config, archive, and trips all survive, since `git reset --hard` only touches files git tracks and `data/` is gitignored.

## Restarting / logs

```
sudo docker-compose restart bv-web
sudo docker-compose logs -f bv-web
sudo docker-compose down          # stops and removes bv-web's container (data/ is unaffected)
```

## See also

- `docs/WEB_ARCHITECTURE.md` - what bv-web is, structurally, and how it relates to the rest of the project
- `WORKING_CONTEXT.md` - the running log of what's been built, including the NAS mount/toolchain fix that let bv-web's own job runner actually write to the archive.
- `docs/PIPELINE.md` - the bv-download -> bv-generate -> bv-export pipeline that produces what `data/trips` holds.
