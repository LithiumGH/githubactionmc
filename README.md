# GitHubActionMC — Play Minecraft in GitHub Actions via Browser

Run Minecraft Java (Vanilla / Fabric / Forge / NeoForge / Quilt) inside a GitHub Actions runner, headless, with a virtual GPU stack + custom launcher + Modrinth support, playable from any browser via noVNC + Cloudflare Tunnel.

> **Honest limits (read first):**
> 1. **You can't run "without an OS"** — Actions runners *are* Ubuntu. What we do is run **headless without a desktop**: `Xvfb + Openbox + EGL`, no GNOME/KDE overhead.
> 2. **There is no magic FakeGPU that beats a real GPU.** Runners have 0 GPUs. `FakeGPU` here = Mesa `llvmpipe`/`lavapipe` + SwiftShader + Zink exposed as a virtual Vulkan/OpenGL device + JVM tuning + auto-perf-mods (Sodium, FerriteCore, etc). Expect ~20-60 FPS at 720p with Sodium, not 240 FPS.
> 3. **You must own Minecraft Java.** No cracked client is included. Login is Microsoft device-code OAuth or offline-mode (only for servers/tests you own). Game files are downloaded at runtime from Mojang/Minecraft servers.
> 4. **Actions limit:** 6h per job, 2 vCPU / 7GB RAM. Use `5120M` max RAM, render distance 6-8, 1280x720.

---

## Architecture

```
Browser  <--https-->  Cloudflared Tunnel  -->  Runner :8080 (Launcher Web UI)
                                            -->  Runner :6080 (noVNC → Xvfb :99 → Minecraft)

Runner boot:
  setup-fakegpu.sh  → Mesa + lavapipe + SwiftShader + Zink + fake GL strings
  setup-desktop.sh  → Xvfb + Openbox + TigerVNC + noVNC + Cloudflared + Pulse dummy
  launcher/server.py → Custom launcher (versions, loaders, Modrinth, mrpack, launch)
  start.sh           → wires it all together, prints tunnel URLs to Action logs
```

## Quick start

1. Push this folder to a GitHub repo.
2. Go to **Actions → Minecraft Browser Play → Run workflow**, fill inputs:
   - `mc_version`: e.g. `1.21` (or `latest`)
   - `loader`: `vanilla` | `fabric` | `forge` | `neoforge` | `quilt`
   - `username`: offline name or Microsoft gamertag
   - `modpack_url`: optional Modrinth `.mrpack` URL
   - `ram`: e.g. `4096M`
3. Wait ~2-4 min. Logs will show:
   - `Launcher UI: https://xxxx.trycloudflare.com`
   - `Game desktop (noVNC): https://yyyy.trycloudflare.com`
4. Open Launcher UI → pick version → **Install & Launch**. Then open desktop link → play with keyboard/mouse in browser.

Local test (Fedora/Debian with Python 3.10+):

```bash
pip install -r launcher/requirements.txt
bash scripts/setup-fakegpu.sh
bash scripts/setup-desktop.sh
python3 launcher/server.py --port 8080
# open http://localhost:8080, game on http://localhost:6080/vnc.html
```

## Repo layout

```
.github/workflows/minecraft.yml  — the Action (manual dispatch + tunnel + keepalive)
launcher/
  mc_core.py    — install/launch logic: Mojang, Fabric, Forge, NeoForge, Quilt, Modrinth, mrpack
  server.py     — stdlib+Flask-lite web API + static UI server
  public/index.html — launcher web UI
  requirements.txt
scripts/
  setup-fakegpu.sh  — virtual GPU stack
  setup-desktop.sh  — Xvfb/VNC/noVNC/cloudflared
  start.sh          — entrypoint used by the Action
```

## FakeGPU — what it actually does

`scripts/setup-fakegpu.sh`:
- installs `mesa-dri-drivers`, `mesa-vulkan-drivers` (lavapipe), `libgl1`, SwiftShader fallback
- enables `Zink` (OpenGL-on-Vulkan) when available, else `llvmpipe`
- sets `LP_NUM_THREADS=$(nproc)`, `vblank_mode=0`, `__GL_SYNC_TO_VBLANK=0` (uncapped FPS)
- fakes `glxinfo` renderer string to `FakeGPU-4090-Virtual (llvmpipe)` so Minecraft/shaders see a "GPU"
- writes `/etc/fakegpu/env` sourced by launcher before every launch
- JVM FPS flags: G1GC/Aikar flags, `-Dorg.lwjgl.util.Debug=false`, render thread opts, plus one-click Sodium/ImmediatelyFast/FerriteCore/EntityCulling/Lithium install from Modrinth

## Launcher features

- Vanilla, Fabric, Quilt, Forge (installer headless), NeoForge
- Java auto-select: 8 (<1.17), 17 (1.17–1.20.4), 21 (1.20.5+)
- Modrinth API v2: search, latest stable version for your MC+loader, install to `mods/`
- Modpack: paste any `.mrpack` URL (Modrinth) → downloads mods + extracts `overrides/`
- Offline + Microsoft device-code auth (paste code at microsoft.com/link)
- Saves instances to `~/minecraft/` (survives within one job only — download your world before job ends!)

## Inputs (workflow_dispatch)

| input | default | notes |
|---|---|---|
| mc_version | 1.21 | or `latest` |
| loader | fabric | vanilla/fabric/forge/neoforge/quilt |
| loader_version | latest | or pin |
| username | Player | offline name |
| ram | 4096M | ≤5120M on free runners |
| modpack_url | (empty) | `.mrpack` direct link |
| modrinth_mods | sodium,ferrite-core | comma list, auto perf mods |
| resolution | 1280x720 | WxH |

## Keep alive / anti-idle

Action runs `start.sh` which heartbeats every 5 min and holds the job up to 6h (`timeout-minutes: 360`). Mouse/keyboard input in noVNC counts as activity. Don't use this to mine crypto or host 24/7 public servers — that violates GitHub ToS. Personal play/testing is the intended use.

## License

MIT — game assets belong to Mojang/Microsoft.
