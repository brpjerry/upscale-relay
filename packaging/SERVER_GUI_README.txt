Upscale Relay Server - Windows GUI

1. Extract the entire ZIP to a permanent folder. Keep _internal next to
   upscale-relay-server-gui.exe. Launch the EXE from the extracted folder.
2. First launch installs and verifies the NVIDIA runtime. Allow several GB
   of download and free disk space. A current NVIDIA driver is required.
3. Open Configure from the tray icon. Select the models folder (ONNX models
   and their manifests) and optionally add media-library folders. The
   passthrough model is always available for initial connection testing.
4. Apply the settings. The default control and media ports are 8590 and 8591.
   Allow the server on your private network in Windows Firewall. If an
   explicit rule is needed, run this in an administrator PowerShell:

   New-NetFirewallRule -DisplayName "Upscale Relay" -Direction Inbound -Protocol TCP -LocalPort 8590,8591 -Action Allow -Profile Private

5. Use the address shown beside Connect to in Configure. If needed, find the
   desktop's LAN IPv4 address with ipconfig. On the client, connect to
   ADDRESS:8590. Check http://ADDRESS:8590/status from the client machine.
6. Start with the passthrough model. Choose a quality tier the network can
   sustain, then test playback, audio/subtitles, pause, seeks, and Stop.
   Once that works, select an actual ONNX model with the GPU otherwise idle.

The runtime is stored under %LOCALAPPDATA%\upscale-relay\runtimes.
The optional default-on server log is Documents\upscale-relay-server.log.
It retains an 8 MiB current log and three rotated snapshots.
BUILD.txt identifies the exact source commit and build run.
No Python, CUDA Toolkit, TensorRT, or libmpv installation is needed manually.
This server currently assumes a trusted LAN. Pairing/TLS are not implemented.
