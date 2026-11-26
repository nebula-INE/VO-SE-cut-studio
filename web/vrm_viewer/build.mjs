import { cpSync, copyFileSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';

const root = process.cwd();
const dist = resolve(root, 'dist');
const threeRoot = resolve(root, 'node_modules/three');
const vrmRoot = resolve(root, 'node_modules/@pixiv/three-vrm');

for (const path of [threeRoot, vrmRoot]) {
  if (!existsSync(path)) throw new Error('Missing dependency folder: ' + path + '. Run npm install first.');
}
rmSync(dist, { recursive: true, force: true });
mkdirSync(resolve(dist, 'vendor/three'), { recursive: true });
mkdirSync(resolve(dist, 'vendor/three-vrm'), { recursive: true });
copyFileSync(resolve(root, 'index.html'), resolve(dist, 'index.html'));
cpSync(resolve(threeRoot, 'build'), resolve(dist, 'vendor/three/build'), { recursive: true });
cpSync(resolve(threeRoot, 'examples/jsm'), resolve(dist, 'vendor/three/examples/jsm'), { recursive: true });
cpSync(resolve(vrmRoot, 'lib'), resolve(dist, 'vendor/three-vrm/lib'), { recursive: true });

for (const [source, target] of [
  [resolve(threeRoot, 'LICENSE'), resolve(dist, 'THREE-LICENSE.txt')],
  [resolve(vrmRoot, 'LICENSE'), resolve(dist, 'THREE-VRM-LICENSE.txt')],
]) {
  if (existsSync(source)) copyFileSync(source, target);
}
console.log('Offline VRM viewer assets copied to ' + dist);
