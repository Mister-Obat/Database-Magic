"use strict";

const deckBoxRest = { pitch: -10, yaw: -14, scale: 1 };
const deckBoxViewport = { width: 1.24, height: 1.56 };
const deckBoxFantasy = [
    ["obsidian", "Obsidienne"], ["braise", "Braise"], ["abyssal", "Abysses"],
    ["canopy", "Canopée"], ["glacier", "Glacier"],
    ["amethyst", "Améthyste"],
];
const deckBoxSciFi = [
    ["orbital", "Orbital"], ["reactor", "Réacteur"], ["biotech", "Biotech"],
    ["hyperspace", "Hyperespace"], ["cryovault", "Cryostase"],
];
const deckBoxPanoramas = new Map([...deckBoxFantasy, ...deckBoxSciFi]);
const deckBoxDesignGroups = [
    ["Couleurs unies", [
        ["standard", "Standard"], ["plains", "Plaine"], ["island", "Île"],
        ["swamp", "Marais"], ["mountain", "Montagne"], ["forest", "Forêt"],
        ["silver", "Argent"], ["copper", "Cuivre"], ["indigo", "Indigo"],
    ]],
    ["Fantasy", deckBoxFantasy],
    ["Science-fiction", deckBoxSciFi],
];
const deckBoxDesigns = deckBoxDesignGroups.flatMap(([, designs]) => designs);
// Both pieces use the same pigment under the mesh's baked lighting.
const deckBoxColors = new Map([
    ["plains", { color: [174, 157, 129], lightMaterial: true }],
    ["island", { color: [39, 91, 142] }],
    ["swamp", { color: [28, 25, 34] }],
    ["mountain", { color: [142, 57, 44] }],
    ["forest", { color: [48, 98, 59] }],
    ["silver", { color: [156, 166, 179], lightMaterial: true }],
    ["copper", { color: [147, 87, 55] }],
    ["indigo", { color: [72, 67, 147] }],
]);
const deckBoxFinishes = new Map();
let deckBoxMesh;
let deckBoxModel;
let deckBoxTitleMeasure;
let resetActiveDeckBox;
let deckBoxMotion;

function fitDeckBoxTitle(name) {
    if (!deckBoxTitleMeasure) {
        deckBoxTitleMeasure = document.createElement("span");
        deckBoxTitleMeasure.className = "deck-box-title-measure";
        deckBoxTitleMeasure.setAttribute("aria-hidden", "true");
        document.body.append(deckBoxTitleMeasure);
    }
    // Measure on a reference vignette of 500px so the title scales with its box.
    deckBoxTitleMeasure.textContent = name;
    const height = 97;
    const maximum = 38;
    deckBoxTitleMeasure.style.fontSize = `${maximum}px`;
    const fits = () => deckBoxTitleMeasure.scrollWidth <= deckBoxTitleMeasure.clientWidth
        && deckBoxTitleMeasure.offsetHeight <= height;
    if (fits()) return "7.6cqi";
    let min = 0;
    let max = maximum;
    while (max - min > .5) {
        const size = (min + max) / 2;
        deckBoxTitleMeasure.style.fontSize = `${size}px`;
        if (fits()) min = size;
        else max = size;
    }
    return `${min / 5}cqi`;
}

async function loadDeckBoxTexture(url) {
    const image = new Image();
    image.src = url;
    await image.decode();
    const width = image.naturalWidth, height = image.naturalHeight;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    return context.getImageData(0, 0, width, height);
}
async function loadDeckBoxGrain() {
    const { width: size, data } = await loadDeckBoxTexture("/deck-box-grain.png");
    const tones = new Float32Array(size * size);
    for (let i = 0; i < tones.length; i++) tones[i] = data[i * 4] / 128;
    return { size, tones };
}

function hydrateDeckBoxMesh(mesh) {
    const vertices = Float32Array.from(mesh.vertices);
    const jointShadow = new Float32Array(vertices.length / 6);
    for (const [vertex, shade] of mesh.jointShadow || []) jointShadow[vertex] = shade;
    return { ...mesh, vertices, jointShadow };
}

function deckBoxPanoramaU(x, z, [back, front]) {
    const middle = (back + front) / 2, side = front - back;
    const relativeZ = z - middle;
    const scale = Math.max(Math.abs(x) * 2, Math.abs(relativeZ) * 2 / side);
    if (!scale) return .5;
    const edgeX = x / scale, edgeZ = middle + relativeZ / scale;
    // Project rounded corners onto the unwrapped perimeter without creating
    // a UV jump between the vertices of the front and side bevels.
    if (Math.abs(edgeX) < .499999) {
        return relativeZ >= 0 ? .5 + edgeX / (1 + 2 * side) : .5 + edgeX;
    }
    return .5 + Math.sign(edgeX) * (.5 + front - edgeZ) / (1 + 2 * side);
}

function createDeckBoxRenderer(mesh, grain, finish = null) {
    // The bitmap is smaller during pointer motion; the cached rest pose stays at 512 px.
    const width = 256;
    const height = Math.round(width * deckBoxViewport.height / deckBoxViewport.width);
    const rest = document.createElement("canvas");
    rest.width = 512;
    rest.height = Math.round(rest.width * deckBoxViewport.height / deckBoxViewport.width);
    const restContext = rest.getContext("2d");
    if (!restContext) return null;

    const vertices = mesh.vertices;
    const indices = mesh.indices;
    const projected = new Float32Array(vertices.length / 2);
    const depth = new Float32Array(width * height);
    const raster = restContext.createImageData(width, height);
    const radians = Math.PI / 180;
    // Choose the two world axes nearest each triangle's tangent plane.
    const textureAxes = new Uint8Array(indices.length / 3);
    const reliefLight = new Float32Array(indices.length / 3);
    const decoration = finish?.texture;
    const lidIndexCount = mesh.lidIndexCount ?? 0;
    const jointHeights = new Float32Array(vertices.length / 6);
    for (let i = 0; i < vertices.length; i += 6) {
        const rise = Math.max(0, Math.min(.158, (.005 - vertices[i + 2]) * .425));
        jointHeights[i / 6] = vertices[i + 1] - rise;
    }
    for (let i = 0; i < indices.length; i += 3) {
        const a = indices[i] * 6, b = indices[i + 1] * 6, c = indices[i + 2] * 6;
        const bx = vertices[b] - vertices[a], by = vertices[b + 1] - vertices[a + 1];
        const bz = vertices[b + 2] - vertices[a + 2];
        const cx = vertices[c] - vertices[a], cy = vertices[c + 1] - vertices[a + 1];
        const cz = vertices[c + 2] - vertices[a + 2];
        const nx = Math.abs(by * cz - bz * cy);
        const normalY = bz * cx - bx * cz, ny = Math.abs(normalY);
        const nz = Math.abs(bx * cy - by * cx);
        textureAxes[i / 3] = nz >= nx && nz >= ny ? 2 : nx >= ny ? 0 : 1;
        const length = Math.hypot(nx, ny, nz);
        if (length && i < mesh.textIndexStart) reliefLight[i / 3] = (nx / length) ** 2;
        if (length && i >= mesh.textIndexStart) {
            reliefLight[i / 3] = finish?.lightMaterial
                ? 17 * (Math.max(0, normalY) / length) ** 1.4
                : 65 * (1 - nz / length);
        }
    }
    const textureSize = grain?.size ?? 0;
    const repeats = textureSize * 3;
    const textureMask = textureSize - 1; // The seamless PNG tile is 256 × 256.
    const grainStrength = finish?.lightMaterial ? .12 : decoration ? .14 : .34;
    const markFace = finish?.lightMaterial ? 9 : 255;
    const panorama = decoration ? new Float32Array(vertices.length / 3) : null;
    if (panorama) {
        const [back, front] = mesh.depthBounds;
        const side = front - back;
        const topAllowance = .15;
        const crownBevel = .035;
        for (let source = 0, dest = 0; source < vertices.length; source += 6, dest += 2) {
            const x = vertices[source], y = vertices[source + 1], z = vertices[source + 2];
            const contourU = deckBoxPanoramaU(x, z, mesh.depthBounds);
            const capU = .5 + x / (1 + 2 * side);
            const vertical = .5 - y / mesh.height;
            const capWeight = Math.max(0, 1 - vertical * mesh.height / crownBevel);
            const distanceFromFront = Math.max(0, Math.min(1, (front - z) / side));
            // Reserve the upper strip for the top. Both mappings meet at the
            // front edge, where the top's depth offset is zero.
            panorama[dest] = (contourU + (capU - contourU) * capWeight) * decoration.width;
            panorama[dest + 1] = (topAllowance + vertical * (1 - topAllowance)
                - distanceFromFront * topAllowance * capWeight) * decoration.height;
        }
    }

    function draw(target, { pitch, yaw, scale }, frame = raster, zBuffer = depth) {
        const { width, height, data: pixels } = frame;
        const sinP = Math.sin(pitch * radians);
        const cosP = Math.cos(pitch * radians);
        const sinY = Math.sin(yaw * radians);
        const cosY = Math.cos(yaw * radians);
        const zoom = width * scale / deckBoxViewport.width;
        for (let source = 0, dest = 0; source < vertices.length; source += 6, dest += 3) {
            const x = vertices[source], y = vertices[source + 1], z = vertices[source + 2];
            const turned = cosY * z - sinY * x;
            projected[dest] = width / 2 + zoom * (cosY * x + sinY * z);
            projected[dest + 1] = height / 2 - zoom * (cosP * y + sinP * turned);
            projected[dest + 2] = -sinP * y + cosP * turned;
        }

        zBuffer.fill(-Infinity);
        pixels.fill(0);
        for (let triangle = 0; triangle < indices.length; triangle += 3) {
            const a = indices[triangle] * 3;
            const b = indices[triangle + 1] * 3;
            const c = indices[triangle + 2] * 3;
            const ax = projected[a], ay = projected[a + 1];
            const bx = projected[b] - ax, by = projected[b + 1] - ay;
            const cx = projected[c] - ax, cy = projected[c + 1] - ay;
            const area = bx * cy - by * cx;
            if (area >= -0.00001) continue; // Outside-facing triangles wind counterclockwise.

            const left = Math.max(0, Math.floor(ax + Math.min(0, bx, cx)));
            const right = Math.min(width - 1, Math.floor(ax + Math.max(0, bx, cx)));
            const top = Math.max(0, Math.floor(ay + Math.min(0, by, cy)));
            const bottom = Math.min(height - 1, Math.floor(ay + Math.max(0, by, cy)));
            const bStep = cy / area;
            const cStep = -by / area;
            const za = projected[a + 2], zb = projected[b + 2] - za, zc = projected[c + 2] - za;
            const surface = triangle < mesh.textIndexStart;
            // A broad satin reflection follows the rotated shell normals.
            let sheen = 0;
            if (surface) {
                const nx = (zb * cy - by * zc) * zoom;
                const ny = (zb * cx - bx * zc) * zoom;
                const facing = (-.23 * nx + .43 * ny - .87 * area) / Math.hypot(nx, ny, area);
                sheen = 6 * Math.max(0, facing) ** 10;
            }
            const contourLight = reliefLight[triangle / 3];
            const lid = triangle < lidIndexCount;
            const textured = grain && surface;
            const material = surface && finish?.color;
            const decorated = surface && decoration;
            const axis = textureAxes[triangle / 3];
            const u = axis === 0 ? 2 : 0;
            const v = axis === 1 ? 2 : 1;
            const va = a * 2, vb = b * 2, vc = c * 2;
            const sa = mesh.jointShadow[a / 3], sb = mesh.jointShadow[b / 3] - sa, sc = mesh.jointShadow[c / 3] - sa;
            const ha = jointHeights[a / 3], hb = jointHeights[b / 3] - ha, hc = jointHeights[c / 3] - ha;
            const depthA = vertices[va + 2], depthB = vertices[vb + 2] - depthA, depthC = vertices[vc + 2] - depthA;
            const ua = vertices[va + u] * repeats, ub = vertices[vb + u] * repeats - ua;
            const uc = vertices[vc + u] * repeats - ua;
            const ta = vertices[va + v] * repeats, tb = vertices[vb + v] * repeats - ta;
            const tc = vertices[vc + v] * repeats - ta;
            let pa, pb, pc, qa, qb, qc;
            if (decorated) {
                const ai = a / 3 * 2, bi = b / 3 * 2, ci = c / 3 * 2;
                pa = panorama[ai];
                pb = panorama[bi] - pa;
                pc = panorama[ci] - pa;
                qa = panorama[ai + 1];
                qb = panorama[bi + 1] - qa;
                qc = panorama[ci + 1] - qa;
            }
            for (let y = top; y <= bottom; y++) {
                const dy = y + .5 - ay;
                const dx = left + .5 - ax;
                let weightB = (dx * cy - dy * cx) / area;
                let weightC = (dy * bx - dx * by) / area;
                for (let x = left; x <= right; x++, weightB += bStep, weightC += cStep) {
                    const weightA = 1 - weightB - weightC;
                    if (weightA < 0 || weightB < 0 || weightC < 0) continue;
                    const index = y * width + x;
                    const z = za + weightB * zb + weightC * zc;
                    if (z <= zBuffer[index]) continue;
                    zBuffer[index] = z;
                    const pixel = index * 4;
                    if (!surface) {
                        // The bevel carries the relief while the face stays black or white.
                        const mark = finish?.lightMaterial ? markFace + contourLight : markFace - contourLight;
                        pixels[pixel] = mark;
                        pixels[pixel + 1] = mark;
                        pixels[pixel + 2] = mark;
                        pixels[pixel + 3] = 255;
                        continue;
                    }
                    let light = 255 * (1 - sa - weightB * sb - weightC * sc);
                    if (contourLight) {
                        const h = ha + weightB * hb + weightC * hc;
                        const localZ = depthA + weightB * depthB + weightC * depthC;
                        const corner = Math.max(0, Math.min(1, (.055 - localZ) / .075));
                        const ease = corner * corner * (3 - 2 * corner);
                        // Join the dipped lower lip at the front instead of
                        // terminating the higher side shadow at the bevel.
                        const center = .235 + .026 * ease;
                        const halfWidth = .004 + .006 * ease;
                        const band = Math.max(0, Math.min(1, (halfWidth - Math.abs(h - center)) / (halfWidth / 2)));
                        const strength = lid
                            ? .65 * (.60 + .40 * ease)
                            : .28 * Math.max(0, Math.min(1, (localZ - .003) / .014));
                        light *= 1 - contourLight * strength * band;
                    }
                    if (textured) {
                        const tx = Math.floor(ua + weightB * ub + weightC * uc) & textureMask;
                        const ty = Math.floor(ta + weightB * tb + weightC * tc) & textureMask;
                        light *= 1 + grainStrength * (grain.tones[ty * textureSize + tx] - 1);
                    }
                    if (material || decorated) {
                        let colors = material, sample = 0;
                        if (decorated) {
                            const tx = Math.max(0, Math.min(decoration.width - 1, Math.floor(pa + weightB * pb + weightC * pc)));
                            const ty = Math.max(0, Math.min(decoration.height - 1, Math.floor(qa + weightB * qb + weightC * qc)));
                            sample = (ty * decoration.width + tx) * 4;
                            colors = decoration.data;
                        }
                        // Preserve the baked relief and neutral surface illumination.
                        const shade = (weightA * vertices[va + 4] + weightB * vertices[vb + 4]
                            + weightC * vertices[vc + 4]) / .16;
                        const lit = shade * light / 255;
                        for (let channel = 0; channel < 3; channel++) {
                            pixels[pixel + channel] = lit * colors[sample + channel] + sheen;
                        }
                    } else {
                        for (let channel = 0; channel < 3; channel++) {
                            pixels[pixel + channel] = light * (weightA * vertices[va + 3 + channel]
                                + weightB * vertices[vb + 3 + channel] + weightC * vertices[vc + 3 + channel]) + sheen;
                        }
                    }
                    pixels[pixel + 3] = 255;
                }
            }
        }
        target.putImageData(frame, 0, 0);
    }

    // A larger one-off raster smooths stationary silhouettes and the join.
    // Moving boxes reuse the smaller buffers above.
    const supersampled = document.createElement("canvas");
    supersampled.width = width * 2;
    supersampled.height = height * 2;
    const highContext = supersampled.getContext("2d");
    if (highContext) {
        const highFrame = highContext.createImageData(supersampled.width, supersampled.height);
        draw(highContext, deckBoxRest, highFrame, new Float32Array(highFrame.width * highFrame.height));
        restContext.imageSmoothingQuality = "high";
        restContext.drawImage(supersampled, 0, 0, rest.width, rest.height);
    } else {
        rest.width = width;
        rest.height = height;
        draw(restContext, deckBoxRest);
    }
    return { draw, width, height, still: rest.toDataURL() };
}

function loadDeckBoxMesh() {
    deckBoxMesh ??= fetch("/deck-box-mesh.json").then(response => {
        if (!response.ok) throw new Error(`Maillage de deckbox : HTTP ${response.status}`);
        return response.json();
    }).then(hydrateDeckBoxMesh);
    return deckBoxMesh;
}

function loadDeckBoxModel() {
    deckBoxModel ??= Promise.all([
        loadDeckBoxMesh(),
        loadDeckBoxGrain().catch(error => {
            console.warn("Texture de la deckbox indisponible.", error);
            return null;
        }),
    ]).then(([mesh, grain]) => {
        let renderer = null;
        try { renderer = createDeckBoxRenderer(mesh, grain); }
        catch (error) { console.warn("Rendu 3D de la deckbox indisponible.", error); }
        return { mesh, grain, renderer };
    });
    return deckBoxModel;
}

function loadDeckBoxFinish(design) {
    const standard = loadDeckBoxModel();
    if (!deckBoxPanoramas.has(design) && !deckBoxColors.has(design)) return standard;
    if (!deckBoxFinishes.has(design)) {
        deckBoxFinishes.set(design, standard.then(async model => {
            try {
                let finish = deckBoxColors.get(design);
                if (deckBoxPanoramas.has(design)) {
                    const texture = await loadDeckBoxTexture(`/deck-box-${design}.webp`);
                    finish = { texture };
                }
                const renderer = createDeckBoxRenderer(model.mesh, model.grain, finish);
                return renderer ? { ...model, renderer } : model;
            } catch (error) {
                console.warn("Finition de deckbox indisponible.", error);
                return model;
            }
        }));
    }
    return deckBoxFinishes.get(design);
}

let deckBoxFallback;

function prepareDeckBoxFallback(box) {
    if (!deckBoxFallback) {
        const image = new Image();
        image.src = "/deck-box-fallback.webp";
        deckBoxFallback = image.decode().then(() => true, () => false);
    }
    return deckBoxFallback.then(available => {
        if (!available) box.classList.add("deck-box-fallback-unavailable");
    });
}

async function mountDeckBox(tile, box, panel, artwork, title, mana, design = "standard", { preview = false } = {}) {
    try {
        const still = document.createElement("img");
        still.className = "deck-box-model";
        still.alt = "";
        still.draggable = false;
        const [mesh, renderer] = await Promise.all([
            loadDeckBoxMesh(),
            preview ? loadDeckBoxPreview(still, design) : loadDeckBoxFinish(design).then(model => model.renderer),
        ]);
        if (!tile.isConnected) return;
        const { width } = deckBoxViewport;
        const percent = value => `${value * 100}%`;
        panel.style.width = percent(1 / width);
        panel.style.height = percent(mesh.height / deckBoxViewport.height);
        for (const [element, rect] of [[artwork, mesh.artwork], [title, mesh.title], [mana, mesh.mana]]) {
            const [x, y, w, h, z] = rect;
            element.style.left = percent(x);
            element.style.top = percent(y / mesh.height);
            element.style.width = percent(w);
            element.style.height = percent(h / mesh.height);
            element.style.transform = `translateZ(calc(${z * 100 / width} * 1cqi))`;
        }
        if (!preview) {
            if (!renderer) return prepareDeckBoxFallback(box);
            still.src = renderer.still;
            await still.decode();
        }
        if (!tile.isConnected) return;
        box.prepend(still);
        box.classList.add("deck-box-rendered");

        let pose = deckBoxRest;

        function positionPanel() {
            const radians = Math.PI / 180;
            const pitch = pose.pitch * radians;
            const yaw = pose.yaw * radians;
            const [back, front] = mesh.depthBounds;
            const depth = (back + front) / 2;
            const x = Math.sin(yaw) * depth * pose.scale / width;
            const y = (Math.cos(pitch) * mesh.height / 2 - Math.sin(pitch) * Math.cos(yaw) * depth)
                * pose.scale / deckBoxViewport.height;
            box.style.setProperty("--deck-shadow-x", percent(.5 + x));
            box.style.setProperty("--deck-shadow-y", percent(.515 + y));
            box.style.setProperty("--deck-shadow-width", percent(.94 * pose.scale / width));
            box.style.setProperty("--deck-shadow-height", percent(.065 + .12 * Math.abs(Math.sin(pitch))));
            panel.style.transform = `scale(${pose.scale}) rotateX(${pose.pitch}deg) rotateY(${pose.yaw}deg)`;
        }
        positionPanel();
        if (preview) return;

        if (!deckBoxMotion) {
            deckBoxMotion = matchMedia("(any-hover: hover) and (prefers-reduced-motion: no-preference)");
            deckBoxMotion.addEventListener("change", () => {
                if (!deckBoxMotion.matches) resetActiveDeckBox?.();
            });
        }
        let target = deckBoxRest;
        let frame = null;
        let canvas = null;
        let context = null;
        let returning = null;

        function paint() {
            positionPanel();
            if (!canvas) {
                canvas = document.createElement("canvas");
                canvas.className = "deck-box-model";
                canvas.setAttribute("aria-hidden", "true");
                canvas.width = renderer.width;
                canvas.height = renderer.height;
                context = canvas.getContext("2d");
                still.after(canvas);
                still.hidden = true;
            }
            if (context) renderer.draw(context, pose);
        }
        function reset() {
            if (resetActiveDeckBox === reset) resetActiveDeckBox = null;
            if (frame !== null) cancelAnimationFrame(frame);
            frame = null;
            returning = null;
            canvas?.remove();
            canvas = null;
            context = null;
            pose = deckBoxRest;
            positionPanel();
            still.hidden = false;
        }
        function returnToRest() {
            if (resetActiveDeckBox !== reset || returning) return;
            if (!canvas || !tile.isConnected || !deckBoxMotion.matches) {
                reset();
                return;
            }
            returning = { from: pose, start: performance.now() };
            if (frame === null) frame = requestAnimationFrame(tick);
        }
        function tick(now) {
            frame = null;
            if (!tile.isConnected) {
                reset();
                return;
            }
            if (returning) {
                if (pose === deckBoxRest) {
                    reset();
                    return;
                }
                const elapsed = Math.max(0, now - returning.start);
                const { from } = returning;
                const distance = Math.max(0, 1 - elapsed / 150) ** 3;
                // Paint the exact resting geometry before replacing the live canvas next frame.
                pose = distance === 0 ? deckBoxRest : {
                    pitch: deckBoxRest.pitch + (from.pitch - deckBoxRest.pitch) * distance,
                    yaw: deckBoxRest.yaw + (from.yaw - deckBoxRest.yaw) * distance,
                    scale: deckBoxRest.scale + (from.scale - deckBoxRest.scale) * distance,
                };
                paint();
                frame = requestAnimationFrame(tick);
                return;
            }
            const close = Math.abs(target.pitch - pose.pitch) < .08
                && Math.abs(target.yaw - pose.yaw) < .08
                && Math.abs(target.scale - pose.scale) < .001;
            pose = close ? target : {
                pitch: pose.pitch + (target.pitch - pose.pitch) * .19,
                yaw: pose.yaw + (target.yaw - pose.yaw) * .19,
                scale: pose.scale + (target.scale - pose.scale) * .19,
            };
            paint();
            if (!close) frame = requestAnimationFrame(tick);
        }
        function aim(next) {
            if (resetActiveDeckBox !== reset) {
                resetActiveDeckBox?.();
                resetActiveDeckBox = reset;
            }
            returning = null;
            target = next;
            if (frame === null) frame = requestAnimationFrame(tick);
        }
        function hover(event) {
            if (!deckBoxMotion.matches || event.pointerType === "touch" || event.buttons
                || tile.classList.contains("personal-deck-dragging")) return;
            const bounds = box.getBoundingClientRect();
            if (!bounds.width || !bounds.height) return;
            const x = Math.max(-.5, Math.min(.5, (event.clientX - bounds.left) / bounds.width - .5));
            const y = Math.max(-.5, Math.min(.5, (event.clientY - bounds.top) / bounds.height - .5));
            aim({ pitch: -3 + y * 8, yaw: -4 - x * 12, scale: 1.035 });
        }
        for (const event of ["pointerenter", "pointermove"]) tile.addEventListener(event, hover, { passive: true });
        for (const event of ["pointerleave", "pointercancel"]) {
            tile.addEventListener(event, () => {
                if (!tile.classList.contains("personal-deck-dragging")) returnToRest();
            });
        }
        tile.addEventListener("deckboxlift", () => {
            if (deckBoxMotion.matches) aim({ pitch: -9, yaw: -17, scale: 1.045 });
        });
        tile.addEventListener("deckboxrelease", returnToRest);
    } catch (error) {
        console.warn("Modèle de deckbox indisponible.", error);
        return prepareDeckBoxFallback(box);
    }
}

async function loadDeckBoxPreview(still, design) {
    still.src = `/deck-box-preview-${design}.webp`;
    try {
        await still.decode();
    } catch (error) {
        if (design === "standard") throw error;
        still.src = "/deck-box-preview-standard.webp";
        await still.decode();
    }
}
