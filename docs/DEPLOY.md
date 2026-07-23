# Deploying bv-web on a Synology NAS

A step-by-step walkthrough for running `bv-web` in Container Manager, browsable at `http://<nas-ip>:19373`. Covers increment 1 only (browse/watch trips, owner/viewer login) - see `WORKING_CONTEXT.md` for what's not built yet.

## Layout on the NAS

Everything lives under one folder, `/volume1/beyond-video`:

```
/volume1/beyond-video/          <- this repo, checked out directly (not nested)
    Dockerfile
    docker-compose.yml
    pyproject.toml
    src/
    ...
    data/
        trips/                  <- bv-export --target output bv-web browses (mounted read-only)
        config/                 <- web-users.cfg (accounts file) - mounted read-write
```

`data/` isn't part of the git repo (see `.gitignore`) - it's created once, on the NAS, and holds the two things that need to persist across container rebuilds.

## 1. One-time host prep

In DSM:

1. **Package Center** -> install **Container Manager** if it isn't already installed.
2. **Control Panel -> Terminal & SNMP** -> enable **SSH service**. You'll use SSH for the one-off setup/account-creation commands below; the container itself doesn't need SSH.
3. Optional but recommended for easy updates later: **Package Center** -> install **Git Server** (this gives you a real `git` binary over SSH, not just DSM's own tools). If you'd rather not install anything else, skip this and use the "no git" option in step 2 below.

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
```

`git clone https://github.com/sssreh/beyond-video.git .` looks simpler and would also avoid the nesting problem (the trailing `.` means "clone *into* this already-existing folder," not "create a new `beyond-video` folder inside it") - but Synology shared folders aren't actually empty even when File Station shows nothing in them: DSM auto-creates a hidden `@eaDir` housekeeping folder (and sometimes `#recycle`) at the root of every shared folder, and `git clone` refuses to clone into any directory that isn't genuinely empty. Check with `ls -la /volume1/beyond-video` if curious - that's normally all that's there. `git init` + `fetch` + `checkout` doesn't have that restriction, so it's the one to use here.

**Without git**: zip your local checkout, upload it via File Station into `/volume1/beyond-video`, and extract it so the files listed above land directly in that folder (not inside a subfolder the zip creates - check the zip's top level before extracting, or extract elsewhere and move the contents up one level).

## 3. Create the data folders

Still in the SSH session:

```
mkdir -p /volume1/beyond-video/data/trips
mkdir -p /volume1/beyond-video/data/config
```

Both start empty. `data/trips` is where you'll eventually point `bv-export --target` (see "Feeding it real trips" below) - leaving it empty for now is fine, bv-web just shows "No trips found yet."

## 4. Build and start the container

Either through Container Manager's GUI (**Project** -> **Create** -> pick `/volume1/beyond-video` as the path; it auto-detects `docker-compose.yml`) or over SSH:

```
cd /volume1/beyond-video
docker-compose build
docker-compose up -d
```

Note the hyphen: Synology's Container Manager only puts the old standalone `docker-compose` 1.x CLI on the SSH `$PATH`, not the newer `docker compose` (space) v2 plugin - `docker compose ...` will fail with a confusing `unknown shorthand flag` error rather than a clear "not found." Check which one you have with `docker-compose --version` (v1, hyphenated) vs `docker compose version` (v2 plugin) if unsure; every command in this doc uses the hyphenated form to match Christer's actual NAS.

The image only installs the `web` extra (fastapi/uvicorn/jinja2) - it does not pull in faster-whisper/pyannote.audio/argostranslate/torch, so this build should be quick. `docker-compose ps` should show `beyond-video-web` as running.

## 5. Create your owner account

One-time, over SSH (or via Container Manager's own "Terminal" tab for the container):

```
docker exec -it beyond-video-web bv-web adduser christer --role owner --users-file /data/config/web-users.cfg
```

Prompts for a password twice. This writes `data/config/web-users.cfg` on the host, which survives container rebuilds. Repeat with `--role viewer` later for family members.

## 6. Verify

From a browser on the same network: `http://<nas-ip>:19373`. Log in with the account from step 5. You should land on the trip list, showing "No trips found yet" until step 7 below.

If it doesn't load, check DSM's own firewall (**Control Panel -> Security -> Firewall**) isn't blocking port 19373, and `docker-compose logs -f` from `/volume1/beyond-video` for errors.

## 7. Feeding it real trips

`bv-web` only ever reads what's in `data/trips` - it doesn't run `bv-export` itself in this increment. Whatever machine you actually run `bv-export` on (with `--target`) needs its output to end up in `/volume1/beyond-video/data/trips` on the NAS - e.g. by pointing `--target` at a mapped SMB/NFS share to that folder, or by running `bv-export` on the NAS itself (if it's ever set up there), or by syncing/copying finished trip folders over afterward. This isn't solved yet - flagging it here rather than guessing your setup; happy to work out the exact path once you know how/where you'll be running `bv-export` day to day.

## Updating later

```
cd /volume1/beyond-video
git pull
docker-compose up -d --build
```

`data/` is untouched by this - your accounts and trips survive.

## Restarting / logs

```
docker-compose restart
docker-compose logs -f
docker-compose down      # stops and removes the container (data/ is unaffected)
```

## See also

- `WORKING_CONTEXT.md` - what bv-web does and doesn't do yet (increment 1: browse/watch only).
- `docs/PIPELINE.md` - the bv-download -> bv-generate -> bv-export pipeline that produces what `data/trips` holds.
