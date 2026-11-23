"use strict";

const deckBoxRest = { pitch: -10, yaw: -14, scale: 1 };
const deckBoxViewport = { width: 1.24, height: 1.56 };
let deckBoxModel;
let deckBoxTitleMeasure;

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
    if (deckBoxTitleMeasure.offsetHeight <= height) return "7.6cqi";
    let min = 0;
    let max = maximum;
    while (max - min > .5) {
        const size = (min + max) / 2;
        deckBoxTitleMeasure.style.fontSize = `${size}px`;
        if (deckBoxTitleMeasure.offsetHeight <= height) min = size;
        else max = size;
    }
    return `${min / 5}cqi`;
}

async function loadDeckBoxGrain() {
    const image = new Image();
    image.src = "/deck-box-grain.png";
    await image.decode();
    const size = image.naturalWidth;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    const samples = context.getImageData(0, 0, size, size).data;
    const tones = new Float32Array(size * size);
    for (let i = 0; i < tones.length; i++) tones[i] = samples[i * 4] / 128;
    return { size, tones };
}

function createDeckBoxRenderer(mesh, grain) {
    const width = 384;
    const height = Math.round(width * deckBoxViewport.height / deckBoxViewport.width);
    const rest = document.createElement("canvas");
    rest.width = width;
    rest.height = height;
    const restContext = rest.getContext("2d");
    if (!restContext) return null;

    const vertices = mesh.vertices;
    const indices = mesh.indices;
    const projected = new Float32Array(vertices.length / 2);
    const depth = new Float32Array(width * height);
    const raster = restContext.createImageData(width, height);
    const pixels = raster.data;
    const radians = Math.PI / 180;
    // Choose the two world axes nearest each triangle's tangent plane.
    const textureAxes = new Uint8Array(indices.length / 3);
    if (grain) {
        for (let i = 0; i < indices.length; i += 3) {
            const a = indices[i] * 6, b = indices[i + 1] * 6, c = indices[i + 2] * 6;
            const bx = vertices[b] - vertices[a], by = vertices[b + 1] - vertices[a + 1];
            const bz = vertices[b + 2] - vertices[a + 2];
            const cx = vertices[c] - vertices[a], cy = vertices[c + 1] - vertices[a + 1];
            const cz = vertices[c + 2] - vertices[a + 2];
            const nx = Math.abs(by * cz - bz * cy);
            const ny = Math.abs(bz * cx - bx * cz);
            const nz = Math.abs(bx * cy - by * cx);
            textureAxes[i / 3] = nz >= nx && nz >= ny ? 2 : nx >= ny ? 0 : 1;
        }
    }
    const textureSize = grain?.size ?? 0;
    const repeats = textureSize * 3;
    const textureMask = textureSize - 1; // The seamless PNG tile is 256 × 256.

    function draw(target, { pitch, yaw, scale }) {
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

        depth.fill(-Infinity);
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
            const textured = grain && triangle < mesh.textIndexStart;
            const axis = textureAxes[triangle / 3];
            const u = axis === 0 ? 2 : 0;
            const v = axis === 1 ? 2 : 1;
            const va = a * 2, vb = b * 2, vc = c * 2;
            const ua = vertices[va + u] * repeats, ub = vertices[vb + u] * repeats - ua;
            const uc = vertices[vc + u] * repeats - ua;
            const ta = vertices[va + v] * repeats, tb = vertices[vb + v] * repeats - ta;
            const tc = vertices[vc + v] * repeats - ta;
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
                    if (z <= depth[index]) continue;
                    depth[index] = z;
                    const pixel = index * 4;
                    let light = 255;
                    if (textured) {
                        const tx = Math.floor(ua + weightB * ub + weightC * uc) & textureMask;
                        const ty = Math.floor(ta + weightB * tb + weightC * tc) & textureMask;
                        light *= grain.tones[ty * textureSize + tx];
                    }
                    for (let channel = 0; channel < 3; channel++) {
                        pixels[pixel + channel] = light * (weightA * vertices[va + 3 + channel]
                            + weightB * vertices[vb + 3 + channel] + weightC * vertices[vc + 3 + channel]);
                    }
                    pixels[pixel + 3] = 255;
                }
            }
        }
        target.putImageData(raster, 0, 0);
    }
    draw(restContext, deckBoxRest);
    return { draw, width, height, still: rest.toDataURL() };
}

function loadDeckBoxModel() {
    deckBoxModel ??= Promise.all([
        fetch("/deck-box-mesh.json").then(response => {
            if (!response.ok) throw new Error(`Maillage de deckbox : HTTP ${response.status}`);
            return response.json();
        }),
        loadDeckBoxGrain().catch(error => {
            console.warn("Texture de la deckbox indisponible.", error);
            return null;
        }),
    ]).then(([mesh, grain]) => {
        let renderer = null;
        try { renderer = createDeckBoxRenderer(mesh, grain); }
        catch (error) { console.warn("Rendu 3D de la deckbox indisponible.", error); }
        return { mesh, renderer };
    });
    return deckBoxModel;
}

function mountDeckBox(tile, box, panel, artwork, title, mana) {
    loadDeckBoxModel().then(({ mesh, renderer }) => {
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
        if (!renderer) return;
        const still = document.createElement("img");
        still.className = "deck-box-model";
        still.alt = "";
        still.src = renderer.still;
        still.draggable = false;
        box.prepend(still);
        box.classList.add("deck-box-rendered");

        const motion = matchMedia("(any-hover: hover) and (prefers-reduced-motion: no-preference)");
        let pose = deckBoxRest;
        let target = deckBoxRest;
        let frame = null;
        let canvas = null;
        let context = null;

        function paint() {
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
            still.hidden = pose !== deckBoxRest;
            if (pose === deckBoxRest) {
                canvas?.remove();
                canvas = null;
                context = null;
                return;
            }
            if (!canvas) {
                canvas = document.createElement("canvas");
                canvas.className = "deck-box-model";
                canvas.setAttribute("aria-hidden", "true");
                canvas.width = renderer.width;
                canvas.height = renderer.height;
                context = canvas.getContext("2d");
                still.after(canvas);
            }
            if (context) renderer.draw(context, pose);
        }
        function tick() {
            frame = null;
            if (!tile.isConnected) return;
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
            target = next;
            if (frame === null) frame = requestAnimationFrame(tick);
        }
        function hover(event) {
            if (!motion.matches || event.pointerType === "touch" || event.buttons
                || tile.matches(".personal-deck-dragging,.personal-deck-drag-rest")) return;
            const bounds = box.getBoundingClientRect();
            if (!bounds.width || !bounds.height) return;
            const x = Math.max(-.5, Math.min(.5, (event.clientX - bounds.left) / bounds.width - .5));
            const y = Math.max(-.5, Math.min(.5, (event.clientY - bounds.top) / bounds.height - .5));
            aim({ pitch: -3 + y * 8, yaw: -4 - x * 12, scale: 1.035 });
        }
        for (const event of ["pointerenter", "pointermove"]) tile.addEventListener(event, hover, { passive: true });
        for (const event of ["pointerleave", "pointercancel", "dragstart"]) {
            tile.addEventListener(event, () => aim(deckBoxRest));
        }
        paint();
    }).catch(error => console.warn("Modèle de deckbox indisponible.", error));
}
