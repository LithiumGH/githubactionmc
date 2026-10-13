"""mc_core — minimal but real Minecraft Java installer/launcher.
Supports: vanilla, fabric, quilt, forge, neoforge + Modrinth mods/mrpacks.
Only stdlib + requests. Game files come from Mojang / loader mavens at runtime.
"""
from __future__ import annotations
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

try:
    import requests
    HAVE_REQ = True
except ImportError:
    HAVE_REQ = False

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2/versions"
QUILT_META = "https://meta.quiltmc.org/v3/versions"
FORGE_PROMO = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_API = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
MODRINTH_API = "https://api.modrinth.com/v2"

UA = {"User-Agent": "githubactionmc/1.0 (contact: local)"}

def http_get_json(url, timeout=30):
    if HAVE_REQ:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r.json()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read().decode())

def http_download(url, dest: Path, timeout=120):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if HAVE_REQ:
        with requests.get(url, headers=UA, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for c in r.iter_content(1024 * 64):
                    if c:
                        f.write(c)
    else:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)

def mc_dir(base=None) -> Path:
    return Path(base or os.environ.get("MC_DIR") or Path.home() / "minecraft").resolve()

def rules_allow(rules) -> bool:
    if not rules:
        return True
    allow = False
    for r in rules:
        action = r.get("action") == "allow"
        os_cond = (r.get("os") or {})
        name = os_cond.get("name")
        if name and name != "linux" and "osx" not in str(name):
            if name not in ("linux",):
                continue
        # features (is_demo_user etc) — skip conditioned entries
        if "features" in r:
            continue
        allow = action
    return allow

def lib_path(lib_dir: Path, name: str) -> Path:
    parts = name.split(":")
    g, a, v = parts[0], parts[1], parts[2]
    path = lib_dir / g.replace(".", "/") / a / v / f"{a}-{v}.jar"
    return path

def resolve_manifest_version(want: str):
    m = http_get_json(MOJANG_MANIFEST)
    if want in ("latest", "latest-release", ""):
        return m["latest"]["release"], m
    for v in m["versions"]:
        if v["id"] == want:
            return want, m
    # allow prefix like 1.21 -> newest 1.21.x
    cands = [v["id"] for v in m["versions"] if v["id"].startswith(want)]
    if cands:
        return cands[0], m
    raise ValueError(f"unknown mc version {want!r}")

def version_json(mc_version: str):
    _, m = resolve_manifest_version(mc_version)
    entry = next(v for v in m["versions"] if v["id"] == resolve_manifest_version(mc_version)[0])
    return http_get_json(entry["url"])

def java_for(mc_version: str) -> str:
    # Mojang specifies javaVersion in version json; fallback by mc number
    try:
        vj = version_json(mc_version) if re.match(r"^\d+\.\d+", mc_version) else None
        major = (vj or {}).get("javaVersion", {}).get("majorVersion", 0)
    except Exception:
        major = 0
    if not major:
        nums = [int(x) for x in re.findall(r"\d+", mc_version)][:2]
        if len(nums) == 2 and (nums[0] > 1 or (nums[0] == 1 and nums[1] >= 20)):
            major = 21
        elif len(nums) == 2 and nums[0] == 1 and nums[1] >= 17:
            major = 17
        else:
            major = 8
    cands = []
    for p in [os.environ.get("JAVA_HOME"), "/usr/lib/jvm/temurin-21-jdk-amd64",
              "/usr/lib/jvm/temurin-17-jdk-amd64", "/usr/lib/jvm/java-21-openjdk-amd64",
              "/usr/lib/jvm/java-17-openjdk-amd64", "/usr/lib/jvm/java-8-openjdk-amd64"]:
        if p and Path(p, "bin/java").exists():
            cands.append(str(Path(p, "bin/java")))
    # probe PATH java
    cands.append(shutil.which("java") or "java")
    # pick by version output
    for c in cands:
        try:
            out = subprocess.run([c, "-version"], capture_output=True, text=True, timeout=10)
            txt = out.stderr + out.stdout
            mm = re.search(r'"(\d+)[\.\"]', txt)
            found = int(mm.group(1)) if mm else 0
            if found == 1:  # "1.8.x"
                found = 8
            if found >= major:
                return c
        except Exception:
            continue
    return cands[-1]

def install_vanilla(mc_version: str, base=None, progress=None) -> dict:
    d = mc_dir(base)
    (d / "versions").mkdir(parents=True, exist_ok=True)
    lib_dir, asset_dir = d / "libraries", d / "assets"
    mc_version, _ = resolve_manifest_version(mc_version)
    vj = version_json(mc_version)
    vdir = d / "versions" / mc_version
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"{mc_version}.json").write_text(json.dumps(vj, indent=2))
    client = vj["downloads"]["client"]
    jar = vdir / f"{mc_version}.jar"
    if not jar.exists():
        (progress or print)(f"[vanilla] client {mc_version}")
        http_download(client["url"], jar)
    # libraries
    cp = [str(jar)]
    for lib in vj.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        art = (lib.get("downloads") or {}).get("artifact")
        if not art:
            continue
        lp = lib_dir / art["path"]
        if not lp.exists() or lp.stat().st_size != art.get("size", lp.stat().st_size if lp.exists() else -1):
            try:
                http_download(art["url"], lp)
            except Exception as e:
                (progress or print)(f"[warn] lib {lib['name']}: {e}")
        cp.append(str(lp))
        # natives (linux)
        natives = (lib.get("natives") or {}).get("linux")
        if natives:
            classifiers = (lib.get("downloads") or {}).get("classifiers") or {}
            key = natives.replace("${arch}", "64")
            if key in classifiers:
                c = classifiers[key]
                ndir = vdir / "natives"
                ndir.mkdir(exist_ok=True)
                tmp = lib_dir / (c["path"] + ".dl")
                if not (ndir / ".done").exists():
                    http_download(c["url"], tmp)
                    with zipfile.ZipFile(tmp) as z:
                        z.extractall(ndir)
                    (ndir / ".done").touch()
    # assets
    ai = vj["assetIndex"]
    adir = asset_dir / "indexes"
    adir.mkdir(parents=True, exist_ok=True)
    idx_path = adir / f"{ai['id']}.json"
    if not idx_path.exists():
        http_download(ai["url"], idx_path)
    idx = json.loads(idx_path.read_text())
    for name, o in idx.get("objects", {}).items():
        h, sub = o["hash"], o["hash"][:2]
        p = asset_dir / "objects" / sub / h
        if not p.exists():
            http_download(f"https://resources.download.minecraft.net/{sub}/{h}", p)
    return {"version": mc_version, "version_json": vj, "classpath": cp}

# ---------- loaders ----------
def fabric_profile(mc_version, loader_version="latest"):
    if loader_version in ("latest", "", None):
        vers = http_get_json(f"{FABRIC_META}/loader/{mc_version}")
        loader_version = vers[0]["loader"]["version"]
    prof = http_get_json(f"{FABRIC_META}/loader/{mc_version}/{loader_version}/profile/json")
    return loader_version, prof

def quilt_profile(mc_version, loader_version="latest"):
    if loader_version in ("latest", "", None):
        vers = http_get_json(f"{QUILT_META}/loader/{mc_version}")
        loader_version = vers[0]["loader"]["version"]
    prof = http_get_json(f"{QUILT_META}/loader/{mc_version}/{loader_version}/profile/json")
    return loader_version, prof

def forge_version(mc_version, which="latest"):
    promo = http_get_json(FORGE_PROMO)["promos"]
    key = f"{mc_version}-{which}"
    if key in promo:
        return promo[key]
    cands = [v for k, v in promo.items() if k.startswith(mc_version + "-")]
    if not cands:
        raise ValueError(f"no forge for {mc_version}")
    return sorted(cands)[-1]

def install_loader(mc_version, loader, loader_version="latest", base=None, progress=None) -> dict:
    pr = progress or print
    d = mc_dir(base)
    inst = install_vanilla(mc_version, base, progress)
    mc_version = inst["version"]
    lib_dir = d / "libraries"
    cp_extra, main_class, arg_patch = [], None, {}
    if loader == "vanilla":
        pass
    elif loader in ("fabric", "quilt"):
        prof_fn = fabric_profile if loader == "fabric" else quilt_profile
        lv, prof = prof_fn(mc_version, loader_version)
        pr(f"[{loader}] loader {lv}")
        main_class = prof["mainClass"]
        for lib in prof.get("libraries", []):
            lp = lib_dir / lib["name"].replace(".", "/")  # placeholder
            # maven coords -> path
            g, a, v = lib["name"].split(":")[:3]
            mp = lib_dir / g.replace(".", "/") / a / v / f"{a}-{v}.jar"
            if not mp.exists():
                http_download(lib["url"], mp)
            cp_extra.append(str(mp))
        arg_patch = {"client_args_extra": prof.get("arguments", {}).get("game", []),
                     "jvm_args_extra": prof.get("arguments", {}).get("jvm", [])}
        (d / f"{loader}-profile.json").write_text(json.dumps(prof, indent=2))
    elif loader in ("forge", "neoforge"):
        if loader == "forge":
            fv = loader_version if loader_version not in ("latest", "") else forge_version(mc_version, "recommended" if f"{mc_version}-recommended" in http_get_json(FORGE_PROMO)["promos"] else "latest")
            if "-" not in str(fv):
                fv = f"{mc_version}-{fv}"
            installer_url = f"{FORGE_MAVEN}/{fv}/forge-{fv}-installer.jar"
        else:
            if loader_version in ("latest", "", None):
                vers = http_get_json(NEOFORGE_API)["versions"]
                # filter by mc version prefix in version string if possible
                loader_version = sorted(vers)[-1]
            fv = str(loader_version)
            installer_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{fv}/neoforge-{fv}-installer.jar"
        pr(f"[{loader}] installer {fv}")
        tmp = Path(tempfile.gettempdir()) / f"{loader}-installer.jar"
        http_download(installer_url, tmp)
        java = java_for(mc_version)
        r = subprocess.run([java, "-jar", str(tmp), "--installClient", str(d)],
                           capture_output=True, text=True, timeout=600)
        (d / f"{loader}-install.log").write_text(r.stdout + "\n" + r.stderr)
        if r.returncode != 0:
            raise RuntimeError(f"{loader} installer failed: {r.stderr[-2000:]}")
    else:
        raise ValueError("loader must be vanilla|fabric|quilt|forge|neoforge")
    inst.update({"loader": loader, "loader_version": locals().get("lv", locals().get("fv", "")),
                 "cp_extra": cp_extra, "main_class": main_class, "arg_patch": arg_patch})
    (d / "last_install.json").write_text(json.dumps(
        {"mc": mc_version, "loader": loader, "loader_version": inst["loader_version"]}, indent=2))
    return inst

# ---------- modrinth ----------
def modrinth_search(query, loaders=None, mc_version=None, limit=10):
    params = {"query": query, "limit": str(limit)}
    facets = []
    if loaders:
        facets.append([f"categories:{l}" for l in loaders])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if facets:
        params["facets"] = json.dumps(facets)
    q = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items()) if not HAVE_REQ else None
    if HAVE_REQ:
        r = requests.get(f"{MODRINTH_API}/search", params=params, headers=UA, timeout=30)
        r.raise_for_status()
        return r.json()["hits"]
    import urllib.parse
    with urllib.request.urlopen(f"{MODRINTH_API}/search?{urllib.parse.urlencode(params)}") as f:
        return json.loads(f.read())["hits"]

def modrinth_install(slug_or_id, mc_version, loader, base=None, progress=None) -> Path:
    pr = progress or print
    d = mc_dir(base)
    mods = d / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    vers = http_get_json(f"{MODRINTH_API}/project/{slug_or_id}/version?loaders={urllib.parse.quote(str(loader)) if 'urllib' in dir() else loader}&game_versions={mc_version}" if False else f"{MODRINTH_API}/project/{slug_or_id}/version")
    # filter client-side compatible
    cands = [v for v in vers if mc_version in v.get("game_versions", []) and (loader in v.get("loaders", []) or v.get("loaders", []) == ["minecraft"])]
    cands = cands or [v for v in vers if mc_version in v.get("game_versions", [])] or vers
    if not cands:
        raise ValueError(f"no files for {slug_or_id} on {mc_version}/{loader}")
    v = sorted(cands, key=lambda x: x.get("date_published", ""), reverse=True)[0]
    prim = next((f for f in v["files"] if f.get("primary")), v["files"][0])
    dest = mods / prim["filename"]
    pr(f"[modrinth] {slug_or_id} -> {prim['filename']}")
    http_download(prim["url"], dest)
    return dest

def resolve_mrpack_download(url_or_path: str, progress=None) -> str:
    """Accept direct .mrpack URL, local path, or a Modrinth page URL.
    Returns a direct download URL (or local path unchanged)."""
    pr = progress or print
    if not url_or_path.startswith("http"):
        return url_or_path
    if url_or_path.endswith(".mrpack"):
        # Could still be an HTML page with .mrpack in query — verify later by magic bytes
        return url_or_path
    import urllib.parse as _up
    # Try to extract slug from modrinth.com/modpack/<slug> or /project/<slug> [/version/<ver>]
    try:
        parts = _up.urlparse(url_or_path).path.strip("/").split("/")
        slug, ver = None, None
        if "modrinth.com" in url_or_path:
            if "modpack" in parts:
                slug = parts[parts.index("modpack") + 1]
            elif "project" in parts:
                slug = parts[parts.index("project") + 1]
            elif "mod" in parts:
                slug = parts[parts.index("mod") + 1]
            if "version" in parts:
                ver = parts[parts.index("version") + 1]
        if slug:
            pr(f"[mrpack] resolving Modrinth page '{slug}' via API...")
            if ver:
                v = http_get_json(f"{MODRINTH_API}/version/{urllib.parse.quote(ver)}" if False else f"{MODRINTH_API}/project/{slug}/version/{ver}")
                vers = [v]
            else:
                vers = http_get_json(f"{MODRINTH_API}/project/{slug}/version")
            # prefer a version that actually ships an .mrpack file
            def has_mrpack(v):
                return any(f["filename"].endswith(".mrpack") for f in v.get("files", []))
            with_pack = [v for v in vers if has_mrpack(v)]
            pool = with_pack or vers
            if not pool:
                raise ValueError(f"no versions found for '{slug}'")
            # newest first
            pool = sorted(pool, key=lambda x: x.get("date_published", ""), reverse=True)
            for v in pool:
                for f in v["files"]:
                    if f["filename"].endswith(".mrpack"):
                        pr(f"[mrpack] -> {v.get('name')} ({f['filename']})")
                        return f["url"]
            # version has no .mrpack (e.g. a mod, not a modpack)
            raise ValueError(f"'{slug}' has versions but none ship an .mrpack file — that link is a MOD, not a modpack. Install it via the Mods tab, or paste a direct https://… .mrpack file URL.")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"could not resolve Modrinth link (is it a modpack page URL? try the direct .mrpack file URL): {e}")
    return url_or_path  # not a modrinth page — treat as direct URL


def install_mrpack(url_or_path, base=None, mc_version=None, loader=None, progress=None) -> dict:
    pr = progress or print
    d = mc_dir(base)
    if url_or_path.startswith("http"):
        url_or_path = resolve_mrpack_download(url_or_path, progress=pr)
        zpath = Path(tempfile.gettempdir()) / "pack.mrpack"
        pr(f"[mrpack] downloading {url_or_path}")
        http_download(url_or_path, zpath)
        # friendly check: HTML error pages are not zips
        with open(zpath, "rb") as f:
            magic = f.read(4)
        if magic != b"PK\x03\x04":
            head = magic + open(zpath, "rb").read(200)
            raise ValueError("URL did not return a .mrpack zip (got HTML/text — 'not a zip'). Paste the DIRECT .mrpack file URL (ends in .mrpack) or a modrinth.com/modpack/<slug> page link, not a mod page.")
    else:
        zpath = Path(url_or_path)
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise ValueError("not a zip: file is not a valid .mrpack. Use a direct .mrpack download or Modrinth modpack page URL.")
    try:
        idx = json.loads(z.read("modrinth.index.json"))
    except KeyError:
        raise ValueError("not a zip: archive has no modrinth.index.json — not a Modrinth modpack (.mrpack).")
    pr(f"[mrpack] {idx.get('name')} deps mc={idx['dependencies'].get('minecraft')} loader={idx['dependencies'].get('fabric-loader') or idx['dependencies'].get('forge') or idx['dependencies'].get('neoforge') or idx['dependencies'].get('quilt-loader')}")
    mods = d / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in idx.get("files", []):
        env = (f.get("env") or {}).get("client", "required")
        if env == "unsupported":
            continue
        dest = d / f["path"]
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dl = f["downloads"][0]
        http_download(dl, dest)
        n += 1
    # overrides
    for name in z.namelist():
        if name.startswith("overrides/"):
            rel = name[len("overrides/"):]
            if rel:
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_bytes(z.read(name))
        elif name.startswith("client-overrides/"):
            rel = name[len("client-overrides/"):]
            if rel:
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_bytes(z.read(name))
    return {"name": idx.get("name"), "files": n, "mc": idx["dependencies"].get("minecraft")}

# ---------- launch ----------
PERF_MODS = {
    "fabric": ["sodium", "lithium", "ferrite-core", "entityculling", "immediatelyfast", "krypton"],
    "forge": ["embeddium", "ferrite-core", "entityculling", "krypton"],
    "neoforge": ["embeddium", "ferrite-core", "entityculling"],
    "quilt": ["sodium", "lithium", "ferrite-core"],
    "vanilla": [],
}

AIKAR_FLAGS = ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
               "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch",
               "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
               "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
               "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
               "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem",
               "-XX:MaxTenuringThreshold=1", "-Dusing.aikars.flags=https://mcflags.emc.gs", "-Daikars.new.flags=true"]

def fakegpu_env():
    p = Path("/etc/fakegpu/env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                kv = line[len("export "):]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    v = v.replace("${NPROC:-2}", str(os.cpu_count() or 2))
                    os.environ.setdefault(k, v)

def _merge_arg_list(base_list, extra):
    """Append extra args (str or Mojang rule-dicts) honouring rules/offered keys."""
    for a in extra or []:
        if isinstance(a, str):
            base_list.append(a)
        elif isinstance(a, dict):
            if not rules_allow(a.get("rules")):
                continue
            v = a.get("value")
            if isinstance(v, str):
                base_list.append(v)
            elif isinstance(v, list):
                base_list.extend(x for x in v if isinstance(x, str))
    return base_list


def build_command(mc_version, loader="vanilla", username="Player", ram="4096M",
                  resolution="1280x720", base=None, extra_jvm=None, extra_game=None) -> tuple[str, list]:
    d = mc_dir(base)
    # fall back to last install so "1.21" prefix mismatches can't break launch
    if (d / "last_install.json").exists():
        try:
            last = json.loads((d / "last_install.json").read_text())
        except Exception:
            last = {}
    else:
        last = {}
    if not mc_version:
        mc_version = last.get("mc", "1.21")
    if not loader:
        loader = last.get("loader", "vanilla")
    mc_version, _ = resolve_manifest_version(mc_version)
    vdir = d / "versions" / mc_version
    vj_path = vdir / f"{mc_version}.json"
    if not vj_path.exists():
        raise FileNotFoundError(f"version {mc_version} is not installed yet — hit '1 · Install version' first (looked for {vj_path}).")
    vj = json.loads(vj_path.read_text())
    java = java_for(mc_version)
    fakegpu_env()
    natives = str(vdir / "natives")
    Path(natives).mkdir(parents=True, exist_ok=True)
    libs = [str(lib_dir_p) for lib_dir_p in [d / "libraries"]]
    # classpath from install step: recompute vanilla libs
    cp_entries = []
    for lib in vj.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue
        art = (lib.get("downloads") or {}).get("artifact")
        if art:
            cp_entries.append(str(d / "libraries" / art["path"]))
    cp_entries.append(str(vdir / f"{mc_version}.jar"))
    # loader extras: fabric/quilt jars already in libraries — glob them
    for pat in ["net/fabricmc", "org/quiltmc", "net/minecraftforge", "net/neoforged"]:
        for j in (d / "libraries" / pat).rglob("*.jar"):
            if str(j) not in cp_entries:
                cp_entries.append(str(j))
    main_class = vj.get("mainClass", "net.minecraft.client.main.Main")
    loader_prof = None
    for _cand in (d / f"{loader}-profile.json", d / "fabric-profile.json", d / "quilt-profile.json"):
        if _cand.exists():
            try:
                loader_prof = json.loads(_cand.read_text())
                break
            except Exception:
                continue
    if loader_prof and loader_prof.get("mainClass"):
        main_class = loader_prof["mainClass"]
    else:
        # forge/neoforge replace mainClass via version json in versions/<forge>/ dir
        for cand in (d / "versions").glob("*/**.json"):
            try:
                fj = json.loads(cand.read_text())
                if fj.get("id", "").lower().startswith((loader or "")[:5]) or "forge" in str(cand).lower() or "neoforge" in str(cand).lower():
                    if fj.get("mainClass"):
                        main_class = fj["mainClass"]
                        for lib in fj.get("libraries", []):
                            art = (lib.get("downloads") or {}).get("artifact")
                            if art and (d / "libraries" / art["path"]).exists():
                                cp_entries.append(str(d / "libraries" / art["path"]))
                        break
            except Exception:
                continue
    # args interpolation
    subs = {
        "auth_player_name": username, "version_name": mc_version,
        "game_directory": str(d), "assets_root": str(d / "assets"),
        "assets_index_name": vj.get("assetIndex", {}).get("id", ""),
        "auth_uuid": str(uuid.uuid3(uuid.NAMESPACE_DNS, username)),
        "auth_access_token": "0", "clientid": "githubactionmc",
        "user_type": "legacy", "version_type": vj.get("type", "release"),
        "natives_directory": natives, "launcher_name": "githubactionmc",
        "launcher_version": "1.0", "classpath": os.pathsep.join(cp_entries),
    }
    try:
        W, H = resolution.lower().replace("×", "x").split("x")
    except ValueError:
        W, H = "1280", "720"
    subs.update({"resolution_width": W, "resolution_height": H})
    def sub(s: str):
        for k, v in subs.items():
            s = s.replace("${" + k + "}", v)
        return s
    # jvm args
    jvm = ["-Xmx" + ram, "-Xms" + ram] + AIKAR_FLAGS + [
        "-Dorg.lwjgl.util.NoChecks=true", "-Djava.awt.headless=false",
        "-Dorg.lwjgl.opengl.Display.allowSoftwareOpenGL=true",
        f"-Djava.library.path={natives}", "-cp", os.pathsep.join(cp_entries)]
    _merge_arg_list(jvm, [sub(a) if isinstance(a, str) else {"rules": a.get("rules"), "value": [sub(x) for x in v] if isinstance((v := a.get("value")), list) else sub(v) if isinstance(v, str) else v} for a in (vj.get("arguments", {}).get("jvm", []) or [])])
    if loader_prof:
        _merge_arg_list(jvm, [sub(a) if isinstance(a, str) else a for a in (loader_prof.get("arguments", {}).get("jvm", []) or [])])
    jvm += (extra_jvm or [])
    game = []
    args = vj.get("arguments", {}).get("game", []) or vj.get("minecraftArguments", "").split(" ")
    _merge_arg_list(game, [sub(a) if isinstance(a, str) else {"rules": a.get("rules"), "value": [sub(x) for x in v] if isinstance((v := a.get("value")), list) else sub(v) if isinstance(v, str) else v} for a in args])
    if loader_prof:
        _merge_arg_list(game, [sub(a) if isinstance(a, str) else a for a in (loader_prof.get("arguments", {}).get("game", []) or [])])
    # add defaults only if Mojang args didn't already include them (avoids duplicates that break launch)
    def need(flag):
        return flag not in game
    if need("--username"):
        game += ["--username", username]
    if need("--uuid"):
        game += ["--uuid", subs["auth_uuid"]]
    if need("--accessToken"):
        game += ["--accessToken", "0"]
    if need("--userType"):
        game += ["--userType", "legacy"]
    if need("--versionType"):
        game += ["--versionType", "release"]
    if need("--gameDir"):
        game += ["--gameDir", str(d)]
    if need("--assetsDir"):
        game += ["--assetsDir", str(d / "assets")]
    if need("--assetIndex"):
        game += ["--assetIndex", subs["assets_index_name"]]
    if need("--width"):
        game += ["--width", W, "--height", H]
    game += (extra_game or [])
    return java, [java] + jvm + [main_class] + game

def launch(mc_version, loader="vanilla", username="Player", ram="4096M",
           resolution="1280x720", base=None) -> dict:
    d = mc_dir(base)
    java, cmd = build_command(mc_version, loader, username, ram, resolution, base)
    log = d / "logs" / "game.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":99")
    with open(log, "wb") as f:
        f.write(f"=== launch {mc_version}/{loader} user={username} ===\n".encode())
        f.write((" ".join(cmd) + "\n").encode())
        f.flush()
        p = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, cwd=str(d))
    (d / "game.pid").write_text(str(p.pid))
    (d / "launch_cmd.txt").write_text(" ".join(cmd))
    # early-exit check: if java is missing the game dies in <3s — surface it now
    time.sleep(3)
    if p.poll() is not None:
        tail = log.read_text(errors="replace")[-3000:]
        raise RuntimeError(f"game exited immediately (code {p.returncode}). log tail:\n{tail}")
    return {"pid": p.pid, "log": str(log), "java": java}
