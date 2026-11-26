import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';

const stage = document.getElementById('stage');
const hint = document.getElementById('hint');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x17191f);

const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 100);
camera.position.set(0, 1.45, 3.6);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
stage.appendChild(renderer.domElement);

scene.add(new THREE.HemisphereLight(0xffffff, 0x626b81, 1.7));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
keyLight.position.set(2.0, 3.5, 2.0);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x9abaff, 0.9);
fillLight.position.set(-3.0, 1.5, -2.0);
scene.add(fillLight);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.target.set(0, 0.9, 0);
controls.minDistance = 0.25;
controls.maxDistance = 15;
const grid = new THREE.GridHelper(4, 20, 0x3a4352, 0x2a313d);
grid.position.y = 0;
scene.add(grid);

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));
const clock = new THREE.Clock();
const HUMANOID_BONE_NAMES = [
  'hips', 'spine', 'chest', 'upperChest', 'neck', 'head',
  'leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand',
  'rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes',
  'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes',
  'leftEye', 'rightEye', 'jaw',
  'leftThumbMetacarpal', 'leftThumbProximal', 'leftThumbDistal',
  'leftIndexProximal', 'leftIndexIntermediate', 'leftIndexDistal',
  'leftMiddleProximal', 'leftMiddleIntermediate', 'leftMiddleDistal',
  'leftRingProximal', 'leftRingIntermediate', 'leftRingDistal',
  'leftLittleProximal', 'leftLittleIntermediate', 'leftLittleDistal',
  'rightThumbMetacarpal', 'rightThumbProximal', 'rightThumbDistal',
  'rightIndexProximal', 'rightIndexIntermediate', 'rightIndexDistal',
  'rightMiddleProximal', 'rightMiddleIntermediate', 'rightMiddleDistal',
  'rightRingProximal', 'rightRingIntermediate', 'rightRingDistal',
  'rightLittleProximal', 'rightLittleIntermediate', 'rightLittleDistal',
];

let currentVrm = null;
let headBasePosition = null;
let hipsBasePosition = null;
let boneRestTransforms = new Map();
let appliedBoneNames = new Set();
let previousExpressions = new Set();
let modelLoadGeneration = 0;

function setStatus(message) {
  document.title = 'AXRAL_VRM_STATUS:' + message;
  hint.textContent = message;
}
function resize() {
  const width = Math.max(stage.clientWidth, 1);
  const height = Math.max(stage.clientHeight, 1);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);
resize();

function disposeCurrentModel() {
  if (!currentVrm) return;
  scene.remove(currentVrm.scene);
  VRMUtils.deepDispose(currentVrm.scene);
  currentVrm = null;
  headBasePosition = null;
  hipsBasePosition = null;
  boneRestTransforms.clear();
  appliedBoneNames.clear();
  previousExpressions.clear();
}

function fitCamera(vrm) {
  vrm.scene.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(vrm.scene);
  if (bounds.isEmpty()) {
    controls.target.set(0, 0.9, 0);
    camera.position.set(0, 1.45, 3.6);
    controls.update();
    return;
  }
  const size = bounds.getSize(new THREE.Vector3());
  const center = bounds.getCenter(new THREE.Vector3());
  vrm.scene.position.y -= bounds.min.y;
  const height = Math.max(size.y, 0.5);
  const distance = Math.max(1.8, height * 2.2);
  controls.target.set(center.x, height * 0.53, center.z);
  camera.position.set(center.x, height * 0.58, center.z + distance);
  controls.minDistance = Math.max(0.2, height * 0.15);
  controls.maxDistance = Math.max(5, height * 8);
  controls.update();
}

async function loadModel(url) {
  if (typeof url !== 'string' || !url.startsWith('http://127.0.0.1:')) {
    setStatus('ローカルのVRMファイルを指定してください');
    return;
  }
  const generation = ++modelLoadGeneration;
  setStatus('VRMモデルを読み込んでいます...');
  try {
    const gltf = await loader.loadAsync(url);
    if (generation !== modelLoadGeneration) {
      VRMUtils.deepDispose(gltf.scene);
      return;
    }
    const vrm = gltf.userData.vrm;
    if (!vrm) {
      VRMUtils.deepDispose(gltf.scene);
      throw new Error('VRM拡張情報が見つかりません。VRM 0.x / 1.0形式を確認してください。');
    }
    VRMUtils.rotateVRM0(vrm);
    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);
    VRMUtils.combineMorphs(vrm);
    vrm.scene.traverse((object) => { object.frustumCulled = false; });
    disposeCurrentModel();
    currentVrm = vrm;
    scene.add(vrm.scene);
    vrm.scene.updateMatrixWorld(true);
    const head = vrm.humanoid && vrm.humanoid.getNormalizedBoneNode('head');
    const hips = vrm.humanoid && vrm.humanoid.getNormalizedBoneNode('hips');
    headBasePosition = head ? head.position.clone() : null;
    hipsBasePosition = hips ? hips.position.clone() : null;
    boneRestTransforms = new Map();
    for (const name of HUMANOID_BONE_NAMES) {
      const node = vrm.humanoid && vrm.humanoid.getNormalizedBoneNode(name);
      if (node) {
        boneRestTransforms.set(name, {
          position: node.position.clone(),
          quaternion: node.quaternion.clone(),
        });
      }
    }
    appliedBoneNames.clear();
    fitCamera(vrm);
    setStatus('読み込み完了: ' + (vrm.meta?.name || 'VRMモデル'));
  } catch (error) {
    if (generation !== modelLoadGeneration) return;
    console.error(error);
    setStatus('読み込みエラー: ' + (error?.message || error));
  }
}

function applyMocapState(state) {
  if (!currentVrm || !state || typeof state !== 'object') return;

  // Reset only the bones touched by the previous frame. This prevents a bone
  // omitted by a sparse live packet or by the next timeline clip from staying
  // frozen in a stale pose.
  for (const name of appliedBoneNames) {
    const node = currentVrm.humanoid && currentVrm.humanoid.getNormalizedBoneNode(name);
    const rest = boneRestTransforms.get(name);
    if (node && rest) {
      node.position.copy(rest.position);
      node.quaternion.copy(rest.quaternion);
    }
  }
  appliedBoneNames.clear();

  const bones = state.bones || {};
  for (const [name, transform] of Object.entries(bones)) {
    const node = currentVrm.humanoid && currentVrm.humanoid.getNormalizedBoneNode(name);
    if (!node || !transform) continue;
    appliedBoneNames.add(name);
    const rotation = transform.rotation;
    if (Array.isArray(rotation) && rotation.length === 4) {
      const [x, y, z, w] = rotation;
      if ([x, y, z, w].every(Number.isFinite)) node.quaternion.set(x, y, z, w).normalize();
    }
    if (name === 'hips' && Array.isArray(transform.position) && hipsBasePosition) {
      const [x, y, z] = transform.position;
      if ([x, y, z].every(Number.isFinite)) {
        node.position.set(hipsBasePosition.x + x, hipsBasePosition.y + y, hipsBasePosition.z + z);
      }
    } else if (
      name === 'head' &&
      !Array.isArray(bones.hips?.position) &&
      Array.isArray(transform.position) &&
      headBasePosition
    ) {
      // Keep head-only tracking useful with sources that do not send root-bone data.
      const [x, y, z] = transform.position;
      if ([x, y, z].every(Number.isFinite)) {
        node.position.set(headBasePosition.x + x, headBasePosition.y + y, headBasePosition.z + z);
      }
    }
  }

  const manager = currentVrm.expressionManager;
  if (manager) {
    const expressions = state.expressions || {};
    for (const name of previousExpressions) manager.setValue(name, 0);
    previousExpressions = new Set();
    for (const [name, rawWeight] of Object.entries(expressions)) {
      const weight = Number(rawWeight);
      if (!Number.isFinite(weight)) continue;
      manager.setValue(name, Math.min(1, Math.max(0, weight)));
      previousExpressions.add(name);
    }
  }
}

window.axralVrmViewer = {
  loadModel,
  closeModel() {
    modelLoadGeneration += 1;
    disposeCurrentModel();
    setStatus('VRMモデルを閉じました');
  },
  setMocapState: applyMocapState,
};

function animate() {
  requestAnimationFrame(animate);
  const delta = Math.min(clock.getDelta(), 0.05);
  if (currentVrm) currentVrm.update(delta);
  controls.update();
  renderer.render(scene, camera);
}
animate();
setStatus('VRMモデルを読み込んでください');
