<!--
SPDX-FileCopyrightText: 2026 Tsolo.io

SPDX-License-Identifier: Apache-2.0
-->

# Preparing an NVIDIA host

Do these steps before [Installing](01-install.md) on a server with NVIDIA GPU(s). NVIDIA is the
best-supported vendor: only NVIDIA hosts show GPU numbers and run the AI session.

## Driver

```bash
sudo ubuntu-drivers install --gpgpu && sudo reboot      # then check: nvidia-smi
```

## NVIDIA Container Toolkit

The toolkit lets containers use the GPU:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi              # must list the GPU(s)
```

## Setting

Set `OW_GPU_VENDOR=nvidia` in `.env` (or **Settings → GPU**). Then:

- Desktops get `/dev/dri` and a device request for all GPUs, with `NVIDIA_DRIVER_CAPABILITIES=all`.
- `make start` adds `compose.nvidia.yaml`, so the dashboard and System page show GPU load and memory
  (from NVML). For a plain `docker compose`, add it to `COMPOSE_FILE`.
- The desktop stream uses NVENC for video encoding. OpenGL runs through zink on Vulkan.
- The AI session (vLLM), model suggestions and MATLAB GPU computing (`gpuArray`) work.
