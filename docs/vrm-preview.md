# VRM 3D character preview

Axral Cut Studio includes an offline VRM preview based on Qt WebEngine, Three.js,
and Pixiv's \`@pixiv/three-vrm\` runtime. The browser assets are copied into the
application package; the running application does not download JavaScript from a CDN.

## Build the bundled viewer

From \`web/vrm_viewer\`:

\`\`\`bash
npm install
npm run build
\`\`\`

This copies Three.js, its GLTFLoader/OrbitControls modules, and the three-vrm
runtime into \`web/vrm_viewer/dist\`. The packaging workflow builds these assets
before running PyInstaller.

## Use

1. Open **表示 → 3Dモード** (or 2D+3D).
2. In the 3D character dock, click **VRMを開く...** and select a local \`.vrm\` file.
3. Drag to orbit and use the wheel to zoom.
4. If a VMC/OSC or WebSocket mocap source is connected, normalized humanoid bone
   rotations and supported expressions are forwarded to the VRM preview.

The loader accepts VRM 0.x and VRM 1.0 files. MToon rendering, spring-bone
updates, humanoid nodes, and VRM expression presets are handled by three-vrm.
The current VMC mapping covers the common humanoid body, arm, leg, eye, jaw,
and finger bones. VMC root (Hips) translation is applied without replacing
the model's own child-bone lengths; head-only sources continue to work without
a root-bone stream. Unknown bone/expression names are ignored safely.

## Recording and timeline playback

Live motion capture recordings now write `aural-motion` version 2 when 3D data
is present. Version 2 stores the full head transform, VMC humanoid bone
positions/rotations, expressions, vowels, and existing 2D-compatible fields.
Version 1 files remain readable and retain their original 2D four-arm-bone
schema and 10 MiB limit; VRM version 2 files have a 64 MiB limit.

Place a VRM character asset on an overlay track and the corresponding motion
clip on a visible motion track. When the playhead is inside both clips, the
sampled quaternion/position and expression values are interpolated and applied
to the loaded VRM. During timeline playback this recorded pose takes priority
over live mocap; when no motion clip is active, live VMC/WebSocket input resumes.
The current milestone is still a live preview and timeline pose player; offline
VRM frame rendering and MP4 export remain separate work.

## Current scope

This milestone is a live 3D preview. VRM timeline motion recording/playback,
offline VRM frame rendering, and VRM MP4 export are not included yet; those
will be integrated through a shared offscreen renderer in a later milestone.
The existing built-in 2D renderer and its export path remain unchanged.

The viewer is served by a temporary HTTP server bound only to `127.0.0.1`.
The selected VRM is exposed through a per-session random route. WebEngine local
file access and remote page access are disabled, and a same-origin Content
Security Policy prevents the viewer from loading resources from other origins.
The temporary server is stopped when the application closes.
