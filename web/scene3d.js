/* 在路上 · 3D 暮色山径（three.js r147，本地 vendor，file:// 可用）
 *
 * 场景与 assets/poster.png 同构：深海军蓝夜幕 + 落日橙地平线，
 * 低多边形线框山脉（山脊灰蓝），一条发光小径通向远处灯塔（光束金）。
 * 数据路碑 = 有记录的日期，亮度/高度按当日会话数（热力）；点击路碑 → onPick(dayIndex)。
 * 对外 API：Journey3D.init(el, days, opts) → bool（false=WebGL 不可用，走 2D 兜底）
 *           Journey3D.setSelected(i)
 */
(function () {
  'use strict';

  var PALETTE = {
    night0: 0x0b1320, night1: 0x101826, night2: 0x1b2a47,
    ridge: 0x9fb0c1, accent: 0xe07b39, accentSoft: 0xf2a65a,
    gold: 0xd9b45c, beam: 0xffd98f, slate: 0x2c3e57,
  };

  // ——— 确定性 2D value noise（种子固定，山脉形状每次构建一致）———
  function makeNoise(seed) {
    function hash(x, y) {
      var h = Math.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453;
      return h - Math.floor(h);
    }
    function smooth(t) { return t * t * (3 - 2 * t); }
    function noise(x, y) {
      var xi = Math.floor(x), yi = Math.floor(y);
      var xf = x - xi, yf = y - yi;
      var a = hash(xi, yi), b = hash(xi + 1, yi);
      var c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
      var u = smooth(xf), v = smooth(yf);
      return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
    }
    return function fbm(x, y) {  // 3 倍频，山脊感
      var v = 0, amp = 0.55, f = 1;
      for (var i = 0; i < 3; i++) {
        var n = noise(x * f, y * f);
        v += (1 - Math.abs(n * 2 - 1)) * amp;  // ridged
        amp *= 0.5; f *= 2.1;
      }
      return v;
    };
  }
  var fbm = makeNoise(7);

  // ——— 地形参数：小径 = 山脊线，两侧下落（路碑/行者永远不被前景山遮挡）———
  var PATH_X0 = -58, PATH_X1 = 52;           // 小径起止（压缩到相机视野内）
  var LIGHTHOUSE_X = 66;
  function zPath(x) { return 14 * Math.sin(x * 0.032) + 7 * Math.sin(x * 0.011 + 1.7); }
  function smoothstep(a, b, x) {
    var t = Math.min(1, Math.max(0, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  }
  function terrainH(x, z) {
    // 主脊线：起伏的脊顶，小径就压在上面
    var ridge = 13 + 3.5 * Math.sin(x * 0.045) + fbm(x * 0.015, 7.3) * 6;
    var d = Math.abs(z - zPath(x));
    var side = smoothstep(2, 34, d);                    // 0=脊顶 1=谷底
    var valley = -7 + fbm(x * 0.03, z * 0.03) * 5;      // 谷底起伏
    var h = ridge * (1 - side) + valley * side
          + fbm(x * 0.02, z * 0.02) * 3.5 * (1 - side * 0.6);
    h += smoothstep(-45, -95, z) * 16;                  // 远山背景层（更高，做剪影）
    h += smoothstep(30, 68, x) * 6;                     // 整条脊在灯塔端抬升
    return h;
  }

  // ——— 程序化纹理 ———
  function softCircleTexture() {
    var c = document.createElement('canvas'); c.width = c.height = 64;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.35, 'rgba(255,255,255,.55)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(c);
  }
  function beamTexture() {
    // 水平渐隐 + 垂直羽化，避免光束看成实心白板
    var c = document.createElement('canvas'); c.width = 256; c.height = 64;
    var g = c.getContext('2d');
    var grad = g.createLinearGradient(0, 0, 256, 0);
    grad.addColorStop(0, 'rgba(255,236,190,.9)');
    grad.addColorStop(0.3, 'rgba(255,217,143,.4)');
    grad.addColorStop(1, 'rgba(255,217,143,0)');
    g.fillStyle = grad; g.fillRect(0, 0, 256, 64);
    var img = g.getImageData(0, 0, 256, 64);
    for (var y = 0; y < 64; y++) {
      var feather = 1 - Math.abs(y - 32) / 32;  // 上下边缘羽化
      feather = feather * feather;
      for (var x = 0; x < 256; x++) img.data[(y * 256 + x) * 4 + 3] *= feather;
    }
    g.putImageData(img, 0, 0);
    return new THREE.CanvasTexture(c);
  }

  var glowTex = null;

  function makeTwinklePoints(defs, map, opacity) {
    // defs: [{pos:[x,y,z], size, phase, color:THREE.Color}]
    var n = defs.length;
    var pos = new Float32Array(n * 3), size = new Float32Array(n),
        phase = new Float32Array(n), color = new Float32Array(n * 3);
    for (var i = 0; i < n; i++) {
      pos.set(defs[i].pos, i * 3);
      size[i] = defs[i].size; phase[i] = defs[i].phase;
      color[i * 3] = defs[i].color.r; color[i * 3 + 1] = defs[i].color.g; color[i * 3 + 2] = defs[i].color.b;
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('aSize', new THREE.BufferAttribute(size, 1));
    geo.setAttribute('aPhase', new THREE.BufferAttribute(phase, 1));
    geo.setAttribute('color', new THREE.BufferAttribute(color, 3));
    var mat = new THREE.ShaderMaterial({
      // uDrift=1 时在顶点着色器里漂浮（萤火虫），uFogAmp=1 时按场景雾衰减（星星是天体不衰减）
      uniforms: { uTime: { value: 0 }, uMap: { value: map }, uOpacity: { value: opacity },
                  uDrift: { value: 0 }, uFogAmp: { value: 0 },
                  uFogNear: { value: 60 }, uFogFar: { value: 240 } },
      vertexShader: [
        'attribute float aSize; attribute float aPhase;',
        'uniform float uTime; uniform float uDrift; uniform float uFogNear; uniform float uFogFar;',
        'varying float vTw; varying vec3 vColor; varying float vFog;',
        'void main(){',
        '  vTw = 0.62 + 0.38 * sin(uTime * 1.7 + aPhase);',
        '  vColor = color;',
        '  vec3 p = position;',
        '  p.x += uDrift * sin(uTime * 0.22 + aPhase * 1.7) * 1.6;',
        '  p.y += uDrift * sin(uTime * 0.5 + aPhase * 2.3) * 1.1;',
        '  vec4 mv = modelViewMatrix * vec4(p, 1.0);',
        '  vFog = 1.0 - smoothstep(uFogNear, uFogFar, -mv.z);',
        '  float s = aSize * (0.82 + 0.18 * sin(uTime * 2.1 + aPhase * 1.3));',
        '  gl_PointSize = s * (300.0 / -mv.z);',
        '  gl_Position = projectionMatrix * mv;',
        '}'].join('\n'),
      fragmentShader: [
        'uniform sampler2D uMap; uniform float uOpacity; uniform float uFogAmp;',
        'varying float vTw; varying vec3 vColor; varying float vFog;',
        'void main(){',
        '  vec4 tex = texture2D(uMap, gl_PointCoord);',
        '  float a = tex.a * vTw * uOpacity * mix(1.0, vFog, uFogAmp);',
        '  gl_FragColor = vec4(vColor, a);',
        '}'].join('\n'),
      vertexColors: true, transparent: true, depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    var pts = new THREE.Points(geo, mat);
    pts.frustumCulled = false;
    return pts;
  }

  function lerpColor(a, b, t) {
    return C(a).lerp(C(b), t);
  }
  // r147 开了 sRGBEncoding 后颜色按 linear 解释会发白，统一先转 linear
  function C(hex) { return new THREE.Color(hex).convertSRGBToLinear(); }

  // 世界里的落日辉光（不是屏幕背景）：大竖板放在远山后，构图怎么变都对齐
  function glowPlaneTexture() {
    var c = document.createElement('canvas'); c.width = 512; c.height = 256;
    var g = c.getContext('2d');
    var grad = g.createRadialGradient(256, 256, 10, 256, 256, 250);
    grad.addColorStop(0, 'rgba(252,230,190,.85)');
    grad.addColorStop(0.35, 'rgba(228,198,160,.42)');
    grad.addColorStop(0.7, 'rgba(150,140,170,.12)');
    grad.addColorStop(1, 'rgba(150,140,170,0)');
    g.fillStyle = grad; g.fillRect(0, 0, 512, 256);
    return new THREE.CanvasTexture(c);
  }

  function init(container, days, opts) {
    opts = opts || {};
    if (!window.THREE || !container) return false;
    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'default' });
    } catch (e) { return false; }
    if (!renderer.getContext()) return false;
    glowTex = softCircleTexture();

    var W = container.clientWidth || 800, H = container.clientHeight || 400;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(W, H);
    renderer.outputEncoding = THREE.sRGBEncoding;
    container.appendChild(renderer.domElement);

    var scene = new THREE.Scene();
    scene.background = C(0x0d1522);
    scene.fog = new THREE.Fog(0x0d1522, 60, 240);
    var camera = new THREE.PerspectiveCamera(52, W / H, 0.1, 600);
    // 落日辉光板：远山背后，两层叠加出光晕
    // 小而淡：只做山脊背后的一线晨光，不洗整个天空（前几版太大的教训）
    var glowFar = new THREE.Mesh(new THREE.PlaneGeometry(260, 110),
      new THREE.MeshBasicMaterial({ map: glowPlaneTexture(), transparent: true, opacity: 0.55,
        blending: THREE.AdditiveBlending, depthWrite: false, fog: false }));
    glowFar.position.set(20, 40, -160);
    scene.add(glowFar);
    var glowNear = new THREE.Mesh(new THREE.PlaneGeometry(140, 60),
      new THREE.MeshBasicMaterial({ map: glowPlaneTexture(), transparent: true, opacity: 0.5,
        blending: THREE.AdditiveBlending, depthWrite: false, fog: false }));
    glowNear.position.set(30, 30, -150);
    scene.add(glowNear);

    // ——— 地形：大三角低多边形 + 逐面平涂上色（山脉图的切面感）———
    // 上色规则：高度决定底色（深navy→灰蓝→雾白），面向光源的面染淡金（黎明从右侧山后打来）
    var TER_W = 300, TER_D = 180, SEG_X = 52, SEG_Z = 32;   // 大三角：~5.7×5.6 一格
    var terGeo = new THREE.PlaneGeometry(TER_W, TER_D, SEG_X, SEG_Z);
    terGeo.rotateX(-Math.PI / 2);
    var vp = terGeo.attributes.position;
    // 顶点抖动：规整方格 → 不规则三角（种子固定不漂移；边缘少抖保持轮廓）
    var cellX = TER_W / SEG_X, cellZ = TER_D / SEG_Z;
    for (var i = 0; i < vp.count; i++) {
      var jx = vp.getX(i), jz = vp.getZ(i);
      var edge = Math.min(1, Math.min(Math.abs(jx) / (TER_W / 2), Math.abs(jz) / (TER_D / 2)));
      var amp = 0.85 * (1 - edge * 0.7);   // 内部抖 85% 格距，边缘递减
      jx += (fbm(i * 0.173, 3.1) - 0.55) * 2 * cellX * amp;
      jz += (fbm(9.7, i * 0.211) - 0.55) * 2 * cellZ * amp;
      vp.setX(i, jx); vp.setZ(i, jz);
      vp.setY(i, terrainH(jx, jz));
    }
    var wireGeo = terGeo.clone();                 // 线框用共享边的网格（少一倍线）
    terGeo = terGeo.toNonIndexed();               // 断开共享边：逐面平涂的前提
    terGeo.computeVertexNormals();
    var np = terGeo.attributes.position, nn = terGeo.attributes.normal;
    var colors = new Float32Array(np.count * 3);
    // 高度色带（山脉图：谷底深navy、坡面灰蓝、脊线雾白微金）
    var ramp = [C(0x131e33), C(0x2c3e57), C(0x5d7389), C(0x93a5b6), C(0xc3cdd6)];
    var cGold = C(0xe8c98f), cShadow = C(0x0d1522);
    var lightDir = new THREE.Vector3(0.55, 0.4, -0.72).normalize();  // 光从右后方（灯塔方向）
    function rampColor(h) {
      var stops = [-8, 2, 9, 16, 24];  // 与 ramp 一一对应
      if (h <= stops[0]) return ramp[0].clone();
      for (var k = 0; k < stops.length - 1; k++) {
        if (h <= stops[k + 1])
          return ramp[k].clone().lerp(ramp[k + 1], (h - stops[k]) / (stops[k + 1] - stops[k]));
      }
      return ramp[ramp.length - 1].clone();
    }
    for (var f = 0; f < np.count; f += 3) {     // 逐三角面：同色平涂
      var hf = (np.getY(f) + np.getY(f + 1) + np.getY(f + 2)) / 3;
      var nrm = new THREE.Vector3(nn.getX(f), nn.getY(f), nn.getZ(f));
      var sun = Math.max(0, nrm.dot(lightDir));
      var col = rampColor(hf);
      col.lerp(cShadow, Math.max(0, -nrm.dot(lightDir)) * 0.35);  // 背光面压深
      col.lerp(cGold, sun * sun * 0.5);                           // 向光面染淡金
      for (var k2 = 0; k2 < 3; k2++) {
        colors[(f + k2) * 3] = col.r; colors[(f + k2) * 3 + 1] = col.g; colors[(f + k2) * 3 + 2] = col.b;
      }
    }
    terGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    var DEBUG_TERRAIN = /debug-terrain/.test((window.location && location.hash) || '');
    // DoubleSide：相机可能低于山脊（在褶皱里穿行视角），单面会被剔除（实测）
    var terrain = new THREE.Mesh(terGeo, new THREE.MeshBasicMaterial(
      DEBUG_TERRAIN ? { color: 0xff00ff, side: THREE.DoubleSide }
                    : { vertexColors: true, side: THREE.DoubleSide }));
    scene.add(terrain);
    var wire = new THREE.LineSegments(
      new THREE.WireframeGeometry(wireGeo),
      new THREE.LineBasicMaterial(DEBUG_TERRAIN ? { color: 0x00ff00 }
        : { color: C(PALETTE.ridge), transparent: true, opacity: 0.14 }));
    wire.position.y += 0.06;
    scene.add(wire);

    // ——— 发光小径：实体细线 + 叠加光点（海报的萤光路）———
    var pathPts = [];
    for (var px = PATH_X0; px <= PATH_X1; px += 3)
      pathPts.push(new THREE.Vector3(px, terrainH(px, zPath(px)) + 0.45, zPath(px)));
    var curve = new THREE.CatmullRomCurve3(pathPts);
    var lineGeo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(240));
    scene.add(new THREE.Line(lineGeo,
      new THREE.LineBasicMaterial({ color: C(PALETTE.accentSoft), transparent: true, opacity: 0.85 })));
    var dotDefs = [];
    var cGold = C(PALETTE.gold), cBeam = C(PALETTE.beam);
    for (var di = 0; di < 130; di++) {
      var p = curve.getPoint(di / 129);
      dotDefs.push({ pos: [p.x, p.y + 0.15, p.z], size: 1.1 + Math.random() * 0.9,
        phase: Math.random() * 6.28, color: cGold.clone().lerp(cBeam, Math.random()) });
    }
    scene.add(makeTwinklePoints(dotDefs, glowTex, 0.9));

    // ——— 数据路碑：高度与色温按当日会话数（热力）———
    var n = days.length;
    var maxSess = 1;
    days.forEach(function (d) { maxSess = Math.max(maxSess, d.n_sessions || 0); });
    var hitMeshes = [], milestones = [];
    var hitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false });
    for (var mi = 0; mi < n; mi++) {
      var r = (days[mi].n_sessions || 0) / maxSess;
      var mx = PATH_X0 + (n === 1 ? 0.5 : mi / (n - 1)) * (PATH_X1 - PATH_X0);
      var mz = zPath(mx), my = terrainH(mx, mz);
      var postH = 1.1 + r * 4.2;
      var grp = new THREE.Group();
      grp.position.set(mx, my, mz);
      var post = new THREE.Mesh(
        new THREE.CylinderGeometry(0.2, 0.32, postH, 6),
        new THREE.MeshBasicMaterial({ color: C(0x22304a) }));
      post.position.y = postH / 2;
      grp.add(post);
      var glowMat = new THREE.SpriteMaterial({
        map: glowTex, transparent: true, depthWrite: false,
        blending: THREE.AdditiveBlending,
        color: lerpColor(PALETTE.ridge, r > 0.66 ? PALETTE.beam : PALETTE.accentSoft, Math.min(1, r * 1.4)),
      });
      var glow = new THREE.Sprite(glowMat);
      var gs = 1.8 + r * 3.2;
      glow.scale.set(gs, gs, 1);
      glow.position.y = postH + 0.35;
      grp.add(glow);
      var hit = new THREE.Mesh(new THREE.CylinderGeometry(1.9, 1.9, Math.max(6, postH + 3), 6), hitMat);
      hit.position.y = postH / 2 + 0.5;
      hit.userData.dayIndex = mi;
      grp.add(hit);
      hitMeshes.push(hit);
      scene.add(grp);
      milestones.push({ grp: grp, glow: glow, baseScale: gs, x: mx });
    }

    // ——— 选中日：光柱 ———
    var selBeam = new THREE.Mesh(
      new THREE.CylinderGeometry(0.38, 0.6, 14, 10, 1, true),
      new THREE.MeshBasicMaterial({ color: C(PALETTE.beam), transparent: true, opacity: 0.16,
        blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    selBeam.visible = false;
    scene.add(selBeam);

    // ——— 行者：少年剪影 billboard（gregorian_2 画风），沿小径缓行 ———
    var walker = null, walkerHalo = null;
    if (window.WALKER_SPRITE) {
      var wtex = new THREE.TextureLoader().load(window.WALKER_SPRITE, function () {
        if (reduceMotion) renderStatic();  // 贴图异步就绪后补渲静态帧（review 修正）
      });
      var wmat = new THREE.SpriteMaterial({
        map: wtex,
        transparent: true, depthWrite: false,
      });
      walker = new THREE.Sprite(wmat);
      walker.center.set(0.5, 0.04);
      walker.scale.set(5.0, 7.5, 1);
      scene.add(walker);
      walkerHalo = new THREE.Sprite(new THREE.SpriteMaterial({  // 暖光晕，把剪影从夜色里托出来
        map: glowTex, color: C(PALETTE.accentSoft), transparent: true, opacity: 0.28,
        blending: THREE.AdditiveBlending, depthWrite: false }));
      walkerHalo.scale.set(9.5, 9.5, 1);
      scene.add(walkerHalo);
    }

    // ——— 灯塔：剪影塔身 + 旋转光束 ———
    var LH_X = LIGHTHOUSE_X, LH_Z = zPath(PATH_X1 - 4) + 2;
    var lhGround = terrainH(LH_X, LH_Z) + 2.5;
    var lh = new THREE.Group();
    lh.position.set(LH_X, lhGround, LH_Z);
    var darkMat = new THREE.MeshBasicMaterial({ color: C(0x0d1522) });
    var rock = new THREE.Mesh(new THREE.ConeGeometry(6, 7, 7), darkMat);
    rock.position.y = 2.2; lh.add(rock);
    var tower = new THREE.Mesh(new THREE.CylinderGeometry(1.15, 1.7, 13, 8), darkMat);
    tower.position.y = 10; lh.add(tower);
    var lampY = 17.2;
    var lamp = new THREE.Mesh(new THREE.CylinderGeometry(1.25, 1.25, 2.2, 8),
      new THREE.MeshBasicMaterial({ color: C(PALETTE.beam) }));
    lamp.position.y = lampY; lh.add(lamp);
    var roof = new THREE.Mesh(new THREE.ConeGeometry(1.7, 2.4, 8), darkMat);
    roof.position.y = lampY + 2.3; lh.add(roof);
    var lampGlow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTex, color: C(PALETTE.beam), transparent: true,
      blending: THREE.AdditiveBlending, depthWrite: false }));
    lampGlow.scale.set(16, 16, 1);
    lampGlow.position.y = lampY;
    lh.add(lampGlow);
    var beamGrp = new THREE.Group();
    beamGrp.position.y = lampY;
    var bGeo = new THREE.PlaneGeometry(52, 6.5);
    bGeo.translate(26, 0, 0);
    var bMat = new THREE.MeshBasicMaterial({ map: beamTexture(), color: C(0xffe4ae),
      transparent: true, opacity: 0.4, blending: THREE.AdditiveBlending,
      depthWrite: false, side: THREE.DoubleSide });
    beamGrp.add(new THREE.Mesh(bGeo, bMat));
    var b2 = new THREE.Mesh(bGeo, bMat);
    b2.rotation.y = Math.PI / 2;
    beamGrp.add(b2);
    lh.add(beamGrp);
    scene.add(lh);

    // ——— 萤火虫 + 星空 ———
    var flyDefs = [], starDefs = [];
    var cFly = C(PALETTE.accentSoft), cStar = C(0xdfe8f2);
    for (var fi = 0; fi < 150; fi++) {
      var fx = PATH_X0 + Math.random() * (PATH_X1 - PATH_X0);
      var fz = zPath(fx) + (Math.random() - 0.5) * 46;
      flyDefs.push({ pos: [fx, terrainH(fx, fz) + 0.8 + Math.random() * 6, fz],
        size: 0.9 + Math.random() * 1.4, phase: Math.random() * 6.28,
        color: cFly.clone().lerp(cBeam, Math.random() * 0.7) });
    }
    for (var si = 0; si < 240; si++)
      starDefs.push({ pos: [-170 + Math.random() * 340, 22 + Math.random() * 70, -150 + Math.random() * 130],
        size: 0.5 + Math.random() * 1.0, phase: Math.random() * 6.28,
        color: cStar.clone().lerp(cBeam, Math.random() * 0.25) });
    var flies = makeTwinklePoints(flyDefs, glowTex, 0.85);
    flies.material.uniforms.uDrift.value = 1;   // 萤火虫：顶点着色器里漂浮
    flies.material.uniforms.uFogAmp.value = 1;  // 且受远雾衰减（review 修正穿透雾）
    var stars = makeTwinklePoints(starDefs, glowTex, 0.7);  // 星星是天体：不漂不雾化
    scene.add(flies); scene.add(stars);

    // ——— 相机：静止机位 + 选中聚焦；滚轮缩放、拖拽平移（用户二轮反馈：不要鼠标视差）———
    // 视野要覆盖小径全程 + 灯塔：按容器宽高比反推相机距离（宽屏近、窄屏远）
    var FIT_HALF_W = 95;  // 含灯塔（66）+ 左右余量
    var camZ = Math.max(58, FIT_HALF_W / (Math.tan(26 * Math.PI / 180) * camera.aspect));
    // 低机位、视线略抬：山脊线压在画面中上部，辉光从山后透出（海报构图）
    var camBase = { x: 0, y: 26, z: camZ }, lookBase = { x: 4, y: 14, z: -30 };
    var focusX = 0, focusTarget = 0;
    var zoomFactor = 1, panX = 0, panY = 0;   // 滚轮缩放 0.7~1.4，拖拽平移限幅
    camera.position.set(camBase.x, camBase.y, camBase.z);

    // 相机定位抽成一处：动画帧与静态帧（减动效）共用，保证构图一致（review 修正）
    function placeCamera(t) {
      void t;
      var cz = camBase.z * zoomFactor;
      camera.position.x = camBase.x + focusX + panX;
      camera.position.y = camBase.y + panY;
      camera.position.z = cz;
      camera.lookAt(lookBase.x + focusX * 0.8 + panX, lookBase.y + panY, lookBase.z);
    }
    function clampView() {
      zoomFactor = Math.min(1.4, Math.max(0.7, zoomFactor));
      panX = Math.min(36, Math.max(-36, panX));
      panY = Math.min(12, Math.max(-8, panY));
    }
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function renderStatic() {
      focusX = focusTarget;  // 静态帧不做缓动，直接到位
      placeCamera(0);
      renderer.render(scene, camera);
    }

    var raycaster = new THREE.Raycaster();
    raycaster.params.Points = { threshold: 0 };
    var ndc = new THREE.Vector2();
    function pickAt(clientX, clientY) {
      var rect = renderer.domElement.getBoundingClientRect();
      ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, camera);
      var hits = raycaster.intersectObjects(hitMeshes, false);
      return hits.length ? hits[0].object.userData.dayIndex : -1;
    }
    var downPos = null, panStart = null;
    renderer.domElement.addEventListener('pointerdown', function (e) {
      downPos = [e.clientX, e.clientY];
      panStart = { x: panX, y: panY, moved: false };
    });
    renderer.domElement.addEventListener('pointerup', function (e) {
      if (!downPos) return;
      var moved = Math.hypot(e.clientX - downPos[0], e.clientY - downPos[1]);
      downPos = null; panStart = null;
      if (moved > 6) return;  // 拖拽平移不算点击
      var idx = pickAt(e.clientX, e.clientY);
      if (idx >= 0 && opts.onPick) opts.onPick(idx);
    });
    // 滚轮缩放（略微）：视角已最远（zoomFactor 上限 1.4）后继续向下滚则放行给
    // 页面滚动（用户决策），其余情况阻止页面滚动
    renderer.domElement.addEventListener('wheel', function (e) {
      if (e.deltaY > 0 && zoomFactor >= 1.4 - 1e-6) return;  // 不 preventDefault → 页面下滚
      e.preventDefault();
      zoomFactor *= Math.exp(e.deltaY * 0.0009);
      clampView();
      if (reduceMotion) renderStatic();
    }, { passive: false });
    var lastHoverPick = 0, lastHoverIdx = null;
    renderer.domElement.addEventListener('pointermove', function (e) {
      var rect = renderer.domElement.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      if (downPos && panStart) {          // 拖拽平移（相机与注视点一起平移）
        var wpp = 2 * camBase.z * zoomFactor * Math.tan(26 * Math.PI / 180) / rect.height;
        panX = panStart.x - (e.clientX - downPos[0]) * wpp;
        panY = panStart.y + (e.clientY - downPos[1]) * wpp;
        clampView();
        if (reduceMotion) renderStatic();
        if (lastHoverIdx != null && opts.onHover) {  // 拖拽中收起 tooltip
          lastHoverIdx = null; opts.onHover(null, mx, my);
        }
        return;
      }
      var now = performance.now();  // 射线拾取节流 50ms；tooltip 位置每次移动都跟手（分开走，不卡）
      if (now - lastHoverPick >= 50) {
        lastHoverPick = now;
        var idx = pickAt(e.clientX, e.clientY);
        var newIdx = idx >= 0 ? idx : null;
        if (newIdx !== lastHoverIdx) {
          lastHoverIdx = newIdx;
          renderer.domElement.style.cursor = newIdx != null ? 'pointer' : '';
          if (opts.onHover) opts.onHover(newIdx, mx, my);
        }
      }
      if (lastHoverIdx != null && opts.onHoverMove) opts.onHoverMove(mx, my);
    });
    renderer.domElement.addEventListener('pointerleave', function () {
      downPos = null; panStart = null; lastHoverIdx = null;
      if (opts.onHover) opts.onHover(null, 0, 0);
    });

    // ——— 选中 ———
    var selected = -1;
    function setSelected(i) {
      if (selected >= 0 && selected < milestones.length) {  // 恢复旧选中路碑的缩放
        var om = milestones[selected];
        om.glow.scale.set(om.baseScale, om.baseScale, 1);
      }
      selected = i;
      if (i < 0 || i >= milestones.length) { selBeam.visible = false; return; }
      var m = milestones[i];
      selBeam.visible = true;
      selBeam.position.set(m.grp.position.x, m.grp.position.y + 7, m.grp.position.z);
      focusTarget = Math.max(-24, Math.min(24, m.x * 0.3));
      if (reduceMotion) renderStatic();  // 减动效下没有渲染循环，点日期也要重绘
    }
    if (typeof opts.selected === 'number') setSelected(opts.selected);

    // WebGL 上下文运行中丢失（GPU 重置/标签页回收）：停循环 + 切 2D 兜底（review 修正）
    renderer.domElement.addEventListener('webglcontextlost', function (e) {
      e.preventDefault();
      running = false; inView = false;
      if (opts.onContextLost) opts.onContextLost();
    });

    // ——— 渲染循环（省电：页面隐藏/滚出视口时暂停；减少动态时只渲静态帧）———
    var running = true, inView = true, clock = new THREE.Clock();
    document.addEventListener('visibilitychange', function () { running = !document.hidden; });
    if ('IntersectionObserver' in window)
      new IntersectionObserver(function (en) { inView = en[0].isIntersecting; }, { threshold: 0.02 })
        .observe(container);

    function frame() {
      requestAnimationFrame(frame);
      if (!running || !inView) return;
      var t = clock.getElapsedTime();
      // 粒子（漂浮与雾衰减都在顶点着色器里，review 修正 CPU 逐帧上传）
      flies.material.uniforms.uTime.value = t;
      stars.material.uniforms.uTime.value = t * 0.6;
      // 灯塔光束
      beamGrp.rotation.y = t * 0.4;
      lampGlow.material.opacity = 0.75 + 0.25 * Math.sin(t * 2.4);
      // 行者：沿小径往返缓行，两端淡出
      if (walker) {
        var u = (t * 0.011 + 0.35) % 1;
        var wx = PATH_X0 + u * (PATH_X1 - PATH_X0);
        var wz = zPath(wx);
        var wy = terrainH(wx, wz) + 0.1 + Math.abs(Math.sin(t * 6)) * 0.12;
        walker.position.set(wx, wy, wz);
        var edge = Math.min(u / 0.06, (1 - u) / 0.06, 1);
        walker.material.opacity = Math.max(0, edge);
        if (walkerHalo) {
          walkerHalo.position.set(wx, wy + 2.4, wz);
          walkerHalo.material.opacity = 0.3 * Math.max(0, edge) + 0.06 * Math.sin(t * 2.8);
        }
      }
      // 选中光柱呼吸 + 选中路碑脉动（只写选中那个，review 修正逐帧冗余）
      if (selBeam.visible) selBeam.material.opacity = 0.11 + 0.05 * Math.sin(t * 2.2);
      if (selected >= 0 && selected < milestones.length) {
        var sm = milestones[selected], ss = sm.baseScale * (1.35 + 0.15 * Math.sin(t * 3));
        sm.glow.scale.set(ss, ss, 1);
      }
      // 相机
      focusX += (focusTarget - focusX) * 0.04;
      placeCamera(t);
      renderer.render(scene, camera);
    }

    if (reduceMotion) {
      setSelected(selected);
      renderStatic();
    } else {
      frame();
    }

    function resize() {
      var w = container.clientWidth, h = container.clientHeight;
      if (!w || !h) return;
      camera.aspect = w / h;
      camZ = Math.max(58, FIT_HALF_W / (Math.tan(26 * Math.PI / 180) * camera.aspect));
      camBase.z = camZ;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
      if (reduceMotion) renderStatic();
    }
    if ('ResizeObserver' in window) new ResizeObserver(resize).observe(container);
    else window.addEventListener('resize', resize);

    api.setSelected = setSelected;
    return true;
  }

  var api = { init: init, setSelected: function () {} };
  window.Journey3D = api;
})();
