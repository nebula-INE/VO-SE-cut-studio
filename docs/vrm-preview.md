# VRM 3D character preview

Axral Cut Studio includes an offline VRM preview based on Qt WebEngine, Three.js,
and Pixiv's `@pixiv/three-vrm` runtime. The browser assets are copied into the
application package; the running application does not download JavaScript from a CDN.

## Build the bundled viewer

From `web/vrm_viewer`:

```bash
npm install
npm run build
```

This copies Three.js, its GLTFLoader/OrbitControls modules, and the three-vrm
runtime into `web/vrm_viewer/dist`. The packaging workflow builds these assets
before running PyInstaller.

## Use

1. Open **表示 → 3Dモード** (or 2D+3D).
2. In the 3D character dock, click **VRMを開く...** and select a local `.vrm` file.
3. Drag to orbit and use the wheel to zoom.
4. If a VMC/OSC or WebSocket mocap source is connected, normalized humanoid bone
   rotations and supported expressions are forwarded to the VRM preview.

The loader accepts VRM 0.x and VRM 1.0 files. MToon rendering, spring-bone
updates, humanoid nodes, and VRM expression presets are handled by three-vrm.
The current VMC mapping covers the common humanoid body, arm, leg, eye, jaw,
and finger bones. VMC root (Hips) translation is applied without replacing
the model's own child-bone lengths; head-only sources continue to work without
a root-bone stream. Unknown bone/expression names are ignored safely.

## Motion capture and timeline playback

VMC/OSC and WebSocket head, humanoid bone, and expression samples can be
recorded at 30 fps into `.motion.json`. Files containing VRM transforms use
motion format v2; existing v1 2D recordings remain readable and continue to be
saved as v1 when no VRM transforms are present.

The motion track interpolates positions linearly and rotations with quaternion
SLERP. At playback time it drives the active VRM character clip. Hips position
is retained, while child-bone rest positions are kept from the model so motion
does not overwrite each avatar's bone lengths. When playback leaves a motion
clip, the VRM returns to its rest pose or live VMC feed.

When recording with a VRM model loaded but without an overlapping character
clip, the recorder registers that model as an external project asset and
creates the matching VRM character clip automatically. Existing 2D recording
continues to create the built-in 2D character clip.

## Multiple characters and MP4 export

Each visible character clip is rendered to its own transparent PNG sequence and
composited by the existing FFmpeg video path. Multiple VRM clips may overlap in
time. Overlapping characters are automatically arranged in horizontal stage
lanes in timeline track order; clips in separate time ranges reuse the available
lanes. A single character keeps the original lower-left placement.

For independent motion, place each character on its own visible Overlay/Other
track and create corresponding visible Motion tracks in the same order. When
there are multiple character tracks and multiple Motion tracks, the first
character track uses the first Motion track, the second uses the second, and so
on. A single visible Motion track remains shared across character tracks for
backward compatibility. Overlapping motion clips on the same paired track are
rejected as ambiguous instead of silently mixing two poses.

Select a character clip to edit its stage transform in the timeline controls.
**自動配置** keeps the existing lower-left/smart multi-character placement.
Disable it to set X/Y placement as percentages of the free stage area. Size is
relative to the existing character display size (10–250%), and rotation is
available from -180° to 180°. These values are stored in the project timeline
and are preserved when a clip is split.

VRM rendering is performed through the Qt WebEngine viewer and requires a
working WebGL context. The existing VO-SE synthesis, timeline audio rendering,
audio mixdown, and MP4 muxing algorithms are unchanged.

The viewer is served by a temporary HTTP server bound only to `127.0.0.1`.
The selected VRM is exposed through a per-session random route. WebEngine local
file access and remote page access are disabled, and a same-origin Content
Security Policy prevents the viewer from loading resources from other origins.
The temporary server is stopped when the application closes.
