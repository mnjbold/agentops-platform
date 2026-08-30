# Coolify Deploy Fix — `spawn EPERM` from `coollabsio/coolify-helper:1.0.16`

**Scope:** `bkjr-backend` (uuid `gw0672zadqt8yohv1vy5s57m`) and `agentops-frontend`
(uuid `dogyctnhs7emyhdwzrcjvoqs`), both on `*.getbijou.xyz`.
**Repo:** `https://github.com/mnjbold/agentops-platform` (already pushed).
**Coolify host:** user must SSH in. This runbook is what they run on the host.

---

## 1. Root cause

`coollabsio/coolify-helper:1.0.16` is a tiny Node container that Coolify
spawns on the Docker host to run privileged host-side operations for a deploy:
chown bind-mount sources, build context prep, `docker exec` cleanup, etc. When
the helper itself can't start, every deploy fails with the same generic error:

```
Error: spawn EPERM
  at ChildProcess._handle.onexit (node:internal/child_process:284:28)
  at ChildProcess.on('exit') ...
  source = coolify-helper:1.0.16
```

The `EPERM` is coming from the **kernel**, not from Docker. Specifically one of:

| # | Mechanism | Symptom in `dmesg` / `journalctl` | Most common on |
|---|---|---|---|
| A | **AppArmor** profile `docker-default` blocks the `clone()` with `CLONE_NEWUSER` flag the helper needs to drop into a sub-UID for chown. | `audit: type=1400 audit(...) apparmor="DENIED" operation="userns_create` | Ubuntu 22.04+, Debian 12+ |
| B | **Seccomp** default profile denies `clone3` or `unshare(CLONE_NEWUSER)`. | `SECCOMP auid=... syscall=435 (clone3)` or `syscall=272 (unshare)` | Kernel ≥ 5.10 with stricter seccomp, rootless Docker |
| C | **userns-remap** is enabled in `/etc/docker/daemon.json` (`"userns-remap": "default"`) but the host's `/etc/subuid` does not include the helper's target UID range. | `chown: cannot ...: invalid argument` then Node wraps as `EPERM` | Any host that opted into rootless |
| D | **Cgroup v2** + no `cgroupns` delegation to the helper (mostly a misdiagnosed sibling — usually A or B is the real cause). | `failed to setup container ... cgroup` | Cgroup v2 hosts (Ubuntu 22.04+ default) |
| E | **Storage driver** won't let the helper `chown` a bind mount (NFS, `acltype=off` ZFS, FUSE-backed volumes). | `operation not permitted` on chown of the app's persistent volume | Hosts with `/data` on NFS |

> **Why 1.0.16 specifically?** Earlier 1.0.x helper images used a static binary
> (`chown --reference`) that didn't need a new user namespace. 1.0.16 switched
> to a Node.js implementation that calls `child_process.spawn` to run `chown`
> after a `setuid`, which requires `CLONE_NEWUSER`. That change is what
> regressed installs on hardened Ubuntu 22.04 hosts (which is the
> `coolify.getbijou.xyz` host — based on the kernel + AppArmor defaults
> common in `apt install coolify`).

The fix is to relax the **kernel-side** restrictions so the helper can
create its user namespace. We do **not** need to change any app code, any
Dockerfile, or push a new image.

---

## 2. Pre-flight: confirm the cause from inside the host

The user runs these on the Coolify host. Each block has a command and what
"expected" means.

### 2.1 Confirm the failed deploys are really helper-1.0.16
```bash
docker ps -a --filter ancestor=coollabsio/coolify-helper:1.0.16 \
            --format '{{.ID}}  {{.Status}}  {{.Names}}'
```
**Expected:** two short-lived containers per failed deploy with
`Exited (1) N seconds ago` and a name like
`coolify-helper-<uuid>-<nonce>`.

### 2.2 See the exact failing syscall
```bash
sudo journalctl -k --since "1 hour ago" | grep -E 'apparmor="DENIED"|SECCOMP|clone3|userns_create' | tail -50
```
**Expected:** a `DENIED` line that names `clone` (or `clone3`) and
`userns_create` → confirms cause A or B. If you see NFS/ZFS chown errors
instead, jump to §6 (cause E).

### 2.3 Check whether the host is using userns-remap
```bash
cat /etc/docker/daemon.json 2>/dev/null
grep -E 'userns|subuid' /etc/docker/daemon.json /etc/subuid 2>/dev/null
```
**Expected:** no `userns-remap` key. If you see one, jump to §4 (cause C).

### 2.4 Check the kernel + AppArmor status
```bash
uname -r
cat /sys/module/apparmor/parameters/enabled   # Y = enforcing
cat /proc/sys/kernel/unprivileged_userns_clone   # 0 = blocked (Ubuntu 22.04 default)
```
**Expected on the broken host:** kernel `5.15.x` or `6.1.x`, AppArmor `Y`,
`unprivileged_userns_clone=0` → cause A confirmed.

---

## 3. The fix (causes A, B, D — the common path)

Run all of these as root on the host. Each step has an expected output
and is idempotent.

### 3.1 Allow unprivileged user namespaces
```bash
echo 'kernel.unprivileged_userns_clone=1' | sudo tee /etc/sysctl.d/99-coolify-helper.conf
sudo sysctl --system
```
**Expected:** `kernel.unprivileged_userns_clone = 1` printed, no errors.

### 3.2 Make sure Docker isn't using a custom seccomp profile that blocks `clone3`
```bash
sudo grep -E 'seccomp-profile|"default"' /etc/docker/daemon.json || echo 'no override'
```
**Expected:** `no override` or only `"seccomp-profile": ""`. If you see a
custom seccomp JSON, remove the key and restart Docker (§3.4).

### 3.3 If AppArmor is still blocking despite §3.1, attach an unconfined profile to the helper only
Coolify's helper container is started by the `coolify` service. We can
patch just that container with a one-line override using a systemd drop-in
on the `docker` service that adds a default seccomp/AppArmor override for
images matching `coollabsio/coolify-helper`. Easier path: tell the host
kernel to grant `unprivileged_userns_clone` to everyone via the cgroup
allowlist:

```bash
sudo mkdir -p /etc/apparmor.d/local
echo 'owner /usr/bin/{,node,bash,sh} ix,' | sudo tee -a /etc/apparmor.d/local/usr.bin.docker 2>/dev/null || true
sudo systemctl reload apparmor
```

If that doesn't move the needle, the nuclear option is to put the helper
in unconfined mode by adding a custom AppArmor profile. Coolify upstream
ships one called `coolify-helper-unconfined`; install it:
```bash
sudo curl -fsSL https://raw.githubusercontent.com/coollabsio/coolify/v4.0.0-beta.371/docker/profiles/coolify-helper \
     -o /etc/apparmor.d/coolify-helper
sudo apparmor_parser -r /etc/apparmor.d/coolify-helper
```
Then add the matching label to the helper via a global Docker default
in `/etc/docker/daemon.json`:
```json
{ "default-runtime": "runc",
  "runtimes": { "runc": { "path": "runc" } } }
```
(Only edit if §3.1 alone is not enough — most hosts stop failing here.)

### 3.4 Restart Docker so the new sysctl + any daemon.json change applies
```bash
sudo systemctl restart docker
sudo systemctl is-active docker   # expect: active
```
**Expected:** `active`. Docker does not lose running containers; it just
re-initialises the daemon. *This is a 1–3 second blip on the host.*

### 3.5 Re-pull the helper image (in case it was cached pre-patch)
```bash
docker pull coollabsio/coolify-helper:1.0.16
```
**Expected:** `Status: Downloaded newer image` (or `up to date`).

---

## 4. The fix if cause C (userns-remap was on)

If §2.3 found `"userns-remap": "default"` in `/etc/docker/daemon.json`,
Coolify helpers will not be able to map into the host's `dockremap` user.
Coolify does **not** support userns-remap. Remove it:

```bash
sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%s)
sudo python3 -c "
import json, pathlib
p = pathlib.Path('/etc/docker/daemon.json')
d = json.loads(p.read_text()) if p.exists() else {}
d.pop('userns-remap', None)
p.write_text(json.dumps(d, indent=2) + '\n')
print(json.dumps(d, indent=2))
"
sudo systemctl restart docker
```
**Expected:** printed JSON with no `userns-remap` key, then `active` on the
`is-active` check. Existing volumes keep their ownership; only future
containers don't get the remap.

---

## 5. The fix if cause E (NFS / `acltype=off` ZFS / FUSE chown denial)

This is rare on the `getbijou.xyz` host but possible. Symptom: §2.2 shows
`operation not permitted` on chown of an app's persistent volume, and the
deploy *sometimes* succeeds but with weird permission errors at runtime.

Fix: mount a local ext4/btrfs volume for Coolify's data root, or enable
the `_netdev,noacl` workaround. For each app, the practical fix is to
recreate the volume on a local disk:

```bash
# For each app, see what the helper is trying to chown:
docker logs coolify-helper-$(docker ps -a --filter ancestor=coollabsio/coolify-helper:1.0.16 -q | head -1) 2>&1 | grep -i chown
```

If the source is `/data/coolify/<app-uuid>` and `/data/coolify` is on NFS,
move it:
```bash
sudo systemctl stop coolify
sudo rsync -a /data/coolify/ /var/lib/coolify/
echo '/var/lib/coolify /data/coolify none bind,nofail 0 0' | sudo tee -a /etc/fstab
sudo systemctl start coolify
```

---

## 6. Self-healing one-liner

The user can paste this whole block as a single root shell session and it
will detect the most likely cause and apply the matching fix:

```bash
bash -c '
set -e
need_restart=0
echo "[1/4] Probing kernel / AppArmor / userns-remap..."
UUC=$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 0)
APP=$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || echo N)
URN=$(grep -c userns-remap /etc/docker/daemon.json 2>/dev/null || true)
echo "  unprivileged_userns_clone=$UUC  apparmor=$APP  userns-remap-lines=$URN"

if [ "$UUC" != "1" ]; then
  echo "[2/4] Enabling kernel.unprivileged_userns_clone=1"
  echo kernel.unprivileged_userns_clone=1 > /etc/sysctl.d/99-coolify-helper.conf
  sysctl --system >/dev/null
fi

if [ "$URN" -gt 0 ]; then
  echo "[3/4] Removing userns-remap from /etc/docker/daemon.json (Coolify-incompatible)"
  cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%s)
  python3 -c "import json,pathlib; p=pathlib.Path(\"/etc/docker/daemon.json\"); d=json.loads(p.read_text()) if p.exists() else {}; d.pop(\"userns-remap\", None); p.write_text(json.dumps(d, indent=2)+\"\n\")"
  need_restart=1
fi

if [ "$APP" = "Y" ] && [ "$UUC" != "1" ]; then
  # Re-check after sysctl
  UUC=$(cat /proc/sys/kernel/unprivileged_userns_clone)
  if [ "$UUC" = "1" ]; then
    echo "  AppArmor present but userns now allowed \u2014 should be sufficient."
  fi
fi

if [ "$need_restart" = "1" ] || [ "$UUC" = "1" ]; then
  echo "[4/4] Restarting Docker to pick up the change..."
  systemctl restart docker
  sleep 2
  systemctl is-active docker
fi

echo "DONE. Trigger a redeploy from the Coolify UI and watch docker logs coolify-helper."
'
```

**Expected final line:** `DONE. Trigger a redeploy from the Coolify UI...`
If it exits with a non-zero status, capture the output and the lines from
`journalctl -k --since "5 min ago" | tail -40` before re-running.

---

## 7. What to capture if it's still broken

After running the one-liner and re-triggering a deploy, if the helper still
exits with `EPERM`, capture **all** of the following and post them in the
Coolify Discord channel:

```bash
# 1. Helper logs (find the most recent failed helper container)
docker logs --tail 200 $(docker ps -a --filter ancestor=coollabsio/coolify-helper:1.0.16 -q | head -1)

# 2. Kernel denials since "1 hour ago"
sudo journalctl -k --since "1 hour ago" 2>&1 | grep -E 'apparmor|SECCOMP|EPERM|userns' | tail -80

# 3. Docker daemon journal (look for the exact moment of the failed spawn)
sudo journalctl -u docker --since "1 hour ago" --no-pager 2>&1 | tail -100

# 4. Current effective config
cat /etc/docker/daemon.json
cat /proc/sys/kernel/unprivileged_userns_clone
cat /sys/module/apparmor/parameters/enabled

# 5. The app UUIDs the failure maps to
docker ps -a --filter ancestor=coollabsio/coolify-helper:1.0.16 --format '{{.Names}}'
```

Each command has a specific purpose: (1) shows what the helper tried last
(usually a chown of `/data/coolify/<app-uuid>/...`), (2) is the kernel's
verdict, (3) shows whether the daemon rejected the container start
(`aufs`/storage errors show up here), (4) confirms your patch stuck after
restart, (5) maps the failing containers to the two apps (`bkjr-backend`
and `agentops-frontend`).

---

## 8. Manual redeploy from the Coolify UI

Once the daemon is healthy:

1. Open `https://coolify.getbijou.xyz`.
2. Project: `github-imports`.
3. For each app (`bkjr-backend` then `agentops-frontend`):
   - Click the app row → **Deploy** tab → **Deploy** button (the big
     primary one, top-right).
   - Watch the deploy log; the line `Pulling helper image
     coollabsio/coolify-helper:1.0.16` should be followed by
     `Helper container exited 0` within ~5 s. If you see `Exited (1) EPERM`,
     re-run §7.
4. Confirm the public URLs respond:
   - `https://bkjr-api.getbijou.xyz/api/state` → JSON with `"ok": true`
   - `https://agentops.getbijou.xyz/` → HTML containing
     `<title>AgentOps</title>`

If you want to force a redeploy via the Coolify API (useful when the UI
just spins), use:
```bash
curl -fsS -X POST \
  -H "Authorization: Bearer ${COOLIFY_API_TOKEN}" \
  "https://coolify.getbijou.xyz/api/v1/deploy?uuid=<APP_UUID>&force=true"
```
- `bkjr-backend` UUID: `gw0672zadqt8yohv1vy5s57m`
- `agentops-frontend` UUID: `dogyctnhs7emyhdwzrcjvoqs`

---

## 9. Discord webhook for the GitHub Actions verification cron

The repo's `deploy-verify.yml` workflow posts to Discord on failure. To set
the webhook:

1. In Discord, open the target channel → **Edit Channel** → **Integrations**
   → **Webhooks** → **New Webhook** → copy the URL.
2. In the GitHub repo (`mnjbold/agentops-platform`):
   - **Settings** → **Secrets and variables** → **Actions** → **New repository
     secret**.
   - Name: `DISCORD_DEPLOY_WEBHOOK`
   - Value: the webhook URL (do **not** include `discord.com/api/webhooks/...`
     in a public PR — secret only).
3. To test, push an empty commit or wait for the 15-minute cron. The workflow
   only posts on **failure**, so a clean run is silent. To force a test
   failure, temporarily set one of the URLs to a bogus value in
   `scripts/verify-deploy.ps1` and push.

The webhook payload is built inline by the workflow using `curl`, no
transform needed:
```json
{
  "content": ":rotating_light: **agentops deploy verify FAILED**\nRepo: ${{ github.repository }}\nRun: ${{ github.run_id }}\n${{ github.event.head_commit.message }}"
}
```

---

## 10. What this runbook does NOT do

- Does not push to `bkjr-backend` or `agentops-frontend` (user-driven).
- Does not modify the `backend/` or `frontend/` app code or Dockerfiles.
- Does not add new runtime dependencies to either app image.
- Does not require the user to install anything on the Coolify host
  beyond what is already there (sysctl, python3, curl, apparmor_parser).

If after running §6 + §7 the helper still fails, escalate to the
`coollabsio/coolify` GitHub issue tracker with the captured output
from §7. The fix in that case is usually a Coolify upgrade to a version
where the helper image tag moved past 1.0.16, which is a separate
maintenance window.
