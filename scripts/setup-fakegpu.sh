#!/usr/bin/env bash
# FakeGPU — virtual GPU stack for GitHub Actions (no physical GPU on runners).
# Honest version: exposes Mesa llvmpipe/lavapipe + SwiftShader + Zink as a
# virtual device + uncapped vsync + thread tuning. Won't beat real hardware.
set -eux

if [ -f /etc/os-release ]; then . /etc/os-release; fi
echo "[fakegpu] OS: ${PRETTY_NAME:-unknown}"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \
    mesa-utils mesa-utils-extra libgl1 libglx-mesa0 libegl1 libegl-mesa0 \
    mesa-vulkan-drivers mesa-va-drivers vainfo \
    libvulkan1 vulkan-tools xvfb \
    unzip curl wget jq python3-pip
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y mesa-dri-drivers mesa-vulkan-drivers vulkan-loader \
    mesa-libGL mesa-libEGL xorg-x11-server-Xvfb unzip curl wget jq python3-pip || true
else
  echo "[fakegpu] unknown package manager, continuing with what exists"
fi

# SwiftShader fallback (software Vulkan for LWJGL) — best-effort download
SWDIR=/opt/swiftshader
sudo mkdir -p "$SWDIR"
if [ ! -f "$SWDIR/libvk_swiftshader.so" ]; then
  echo "[fakegpu] fetching SwiftShader..."
  curl -fsSL -o /tmp/sw.zip "https://github.com/google/swiftshader/releases/download/v1.0.0/swiftshader-linux64.zip" || echo "[fakegpu] swiftshader dl failed, using mesa only"
  if [ -f /tmp/sw.zip ]; then sudo unzip -o /tmp/sw.zip -d "$SWDIR" || true; fi
fi

NPROC=$(nproc || echo 2)
sudo mkdir -p /etc/fakegpu /usr/local/bin

# Central env file sourced by launcher before every game launch
sudo tee /etc/fakegpu/env >/dev/null <<EOF
# FakeGPU env — sourced by launcher/server
export GALLIUM_DRIVER=\${GALLIUM_DRIVER:-llvmpipe}
export LP_NUM_THREADS=${NPROC}
export LP_NO_RAST=false
export MESA_LOADER_DRIVER_OVERRIDE=\${MESA_LOADER_DRIVER_OVERRIDE:-}
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_HUD=\${GALLIUM_HUD:-}
export vblank_mode=0
export __GL_SYNC_TO_VBLANK=0
export __GL_THREADED_OPTIMIZATIONS=1
export MESA_GL_VERSION_OVERRIDE=4.6
export MESA_GLSL_VERSION_OVERRIDE=460
export VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json
export XDG_RUNTIME_DIR=\${XDG_RUNTIME_DIR:-/tmp/runtime-\$USER}
export SDL_VIDEO_X11_VISUALID=
export _JAVA_AWT_WM_NONREPARENTING=1
# LWJGL: prefer system GL, no debug
export JAVA_LWJGL_OPTS="-Dorg.lwjgl.util.NoChecks=true -Dorg.lwjgl.opengl.libname=libGL.so.1"
EOF

# Fake glxinfo wrapper so tools/Minecraft see a "GPU" string
sudo tee /usr/local/bin/fakegpu-info >/dev/null <<'EOS'
#!/usr/bin/env bash
echo "name of display: ${DISPLAY:-:99}"
echo "display: ${DISPLAY:-:99}  screen: 0"
echo "direct rendering: Yes"
echo "Extended renderer info (GLX_MESA_query_renderer):"
echo "    Vendor: FakeGPU (0x6666)"
echo "    Device: FakeGPU-4090-Virtual (llvmpipe/Zink) (0xF409)"
echo "    Version: 24.x"
echo "    Accelerated: yes (software-simd, ${THREADS:-} threads)"
echo "    Video memory: 6144MB (shared CPU)"
echo "    Unified memory: yes"
echo "OpenGL renderer string: FakeGPU-4090-Virtual (llvmpipe, LLVM)"
echo "OpenGL core profile version string: 4.6 (Core Profile) Mesa 24.x (git-fakegpu)"
command -v glxinfo >/dev/null && exec glxinfo "$@" || true
EOS
sudo chmod +x /usr/local/bin/fakegpu-info

# Persist threads + ICD check
mkdir -p "/tmp/runtime-$USER" 2>/dev/null || true
echo "[fakegpu] ICDs:"; ls /usr/share/vulkan/icd.d/ 2>/dev/null || echo "(no icd dir — lavapipe may still work via default)"
echo "[fakegpu] glxinfo (real):"; glxinfo -B 2>/dev/null || echo "(no DISPLAY yet — expected before Xvfb)"
echo "[fakegpu] done. Source with: set -a; . /etc/fakegpu/env; set +a"
