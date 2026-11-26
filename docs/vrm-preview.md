# VRM 3D character preview

Axral Cut Studio includes an offline VRM preview based on Qt WebEngine, Three.js,
and Pixiv's `@pixiv/three-vrm` runtime. Browser assets are copied into the
application package; the running application does not download JavaScript from a CDN.

## Build the bundled viewer

From `web/vrm_viewer`:

```bash
npm install
npm run check
npm run build
```

This copies Three.js, GLTFLoader, OrbitControls, and the three-vrm runtime into
`web/vrm_viewer/dist`. The packaging workflow builds these assets before PyInstaller.

## Preview and live tracking

1. Open **表示 → 3Dモード** (or 2D+3D).
2. In the 3D character dock, click **VRMを開く...** and select a local `.vrm` file.
3. Drag to orbit and use the wheel to zoom.
4. A connected VMC/OSC or WebSocket source drives the VRM humanoid and supported
   expression presets. The viewer supports VRM 0.x and VRM 1.0.

The VMC mapping covers common humanoid body, arm, leg, eye, jaw, and finger bones.
Root (Hips) translation is applied without replacing the model's child-bone lengths;
head-only sources continue to work without a root-bone stream. Unknown names are ignored.

## Motion recording and timeline playback

Live motion capture records the full 3D head transform, VMC humanoid positions and
rotations, expressions, and vowels. When 3D data is present, the recorder writes
`aural-motion` version 2. Existing version 1 files remain readable and retain their
original 2D four-arm-bone schema and 10 MiB limit. VRM version 2 files have a 64 MiB limit.

To play a recording, place a VRM character asset on an overlay track and its motion
clip on a visible Motion track. While the playhead is inside both clips, sampled
positions, quaternion rotations, and expression weights are interpolated and applied
to the VRM. A recorded timeline pose takes priority over live tracking; when no motion
clip is active, the preview returns to live VMC/WebSocket input.

The existing built-in 2D character renderer continues to use motion version 1 and its
current MP4 export path. This milestone does not yet render VRM frames offline or
include VRM models in MP4 exports; that requires a separate offscreen renderer.

## Runtime isolation

The viewer is served by a temporary HTTP server bound only to `127.0.0.1`.
The selected VRM is exposed through a per-model random route. WebEngine local-file
access and remote page access are disabled, and a same-origin Content Security Policy
prevents the viewer from loading resources from other origins. The server is stopped
when the application closes.
