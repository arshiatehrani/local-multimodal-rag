#!/usr/bin/env python
"""One-command environment setup for the Local Multimodal RAG app.

What it does (in order):
  1. Detects your NVIDIA CUDA driver version via `nvidia-smi`.
  2. (Re)installs a matching CUDA build of PyTorch — or the CPU build if no GPU.
  3. Installs the rest of `backend/requirements.txt`.
  4. Verifies that torch sees the GPU.

Run it with the SAME Python you'll run the app with (activate your env first):

    python setup_env.py

It's portable: different machines with different CUDA versions get the right
wheel automatically, and machines without an NVIDIA GPU fall back to CPU.
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS = os.path.join(HERE, "backend", "requirements.txt")

# PyTorch CUDA wheel channels, newest first. We keep every channel whose CUDA
# version is <= the installed driver's CUDA version, then try them top-down until
# one provides a wheel for the resolved torch version. Each channel resolves to
# the index URL https://download.pytorch.org/whl/<channel>.
# Channel list mirrors https://download.pytorch.org/whl/ (CUDA folders only).
CUDA_CHANNELS = [
    (13, 2, "cu132"),
    (13, 0, "cu130"),
    (12, 9, "cu129"),
    (12, 8, "cu128"),
    (12, 6, "cu126"),
    (12, 4, "cu124"),
    (12, 1, "cu121"),
    (11, 8, "cu118"),
]

TORCH_PKGS = ["torch", "torchvision"]


def run(cmd):
    print(">", " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def pip(*args):
    return run([sys.executable, "-m", "pip", *args])


def detect_cuda():
    """Return (major, minor) CUDA version from nvidia-smi, or None if no GPU."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi"], text=True, stderr=subprocess.STDOUT
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def install_torch(cuda):
    if cuda is None:
        print("No NVIDIA GPU detected -> installing CPU build of PyTorch.")
        return pip("install", "--upgrade", *TORCH_PKGS)

    major, minor = cuda
    print(f"Detected CUDA driver {major}.{minor}.")
    channels = [ch for (cmaj, cmin, ch) in CUDA_CHANNELS if (cmaj, cmin) <= (major, minor)]
    if not channels:
        print("Driver CUDA is older than the oldest prebuilt wheel -> CPU build.")
        return pip("install", "--upgrade", *TORCH_PKGS)

    for ch in channels:
        url = f"https://download.pytorch.org/whl/{ch}"
        print(f"\nTrying PyTorch CUDA channel '{ch}' ({url}) ...")
        if pip("install", "--upgrade", *TORCH_PKGS, "--index-url", url) == 0:
            return 0
        print(f"Channel '{ch}' did not work; trying the next one.")

    print("All CUDA channels failed -> falling back to CPU build.")
    return pip("install", "--upgrade", *TORCH_PKGS)


def main():
    print("== Step 1: detect GPU and (re)install PyTorch ==")
    # Remove any existing (possibly CPU-only) torch so the right one is installed.
    pip("uninstall", "-y", "torch", "torchvision", "torchaudio")
    cuda = detect_cuda()
    if install_torch(cuda) != 0:
        sys.exit("ERROR: PyTorch installation failed.")

    print("\n== Step 2: install application requirements ==")
    if pip("install", "-r", REQUIREMENTS) != 0:
        sys.exit("ERROR: requirements installation failed.")

    print("\n== Step 3: verify ==")
    run([
        sys.executable, "-c",
        "import torch; print('torch', torch.__version__,"
        " '| cuda available:', torch.cuda.is_available(),"
        " '| device:', (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'))",
    ])
    print("\nDone. If it prints 'cuda available: True', you're GPU-ready.")


if __name__ == "__main__":
    main()
