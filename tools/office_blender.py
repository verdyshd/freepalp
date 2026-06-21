"""
Рендер изометрического воксельного офиса FreePalp в Blender (headless).
Запуск:  blender-launcher.exe --background --python tools/office_blender.py
Выход:   freepalp/web/static/office.png (RGBA, прозрачный фон)
         freepalp/web/static/office.coords.json (пиксельные прямоугольники монитора/головы)
Модель в «грид-единицах»: пол 7×6, ось z — вверх. Истинная изометрия (камера 1,1,1).
"""
import bpy, json, math, os, sys, traceback
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "freepalp", "web", "static")
_LOG = os.path.normpath(os.path.join(OUT_DIR, "office_render.log"))
def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass
try:
    open(_LOG, "w", encoding="utf-8").close()
except Exception:
    pass
def _hook(t, v, tb):
    _log("EXCEPTION:\n" + "".join(traceback.format_exception(t, v, tb)))
sys.excepthook = _hook
_log("START blender " + bpy.app.version_string)
OUT_PNG = os.path.normpath(os.path.join(OUT_DIR, "office.png"))
OUT_JSON = os.path.normpath(os.path.join(OUT_DIR, "office.coords.json"))
RES_X, RES_Y = 1400, 1050

# ── чистим сцену ──
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
for blk in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras):
    for d in list(blk):
        blk.remove(d)

_mats = {}
def mat(name, hexc, rough=0.7, emit=0.0):
    if name in _mats:
        return _mats[name]
    m = bpy.data.materials.new(name); m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    n = int(hexc[1:], 16)
    r, g, b = ((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255
    # sRGB→linear (грубо) для корректного цвета
    lin = lambda c: c ** 2.2
    col = (lin(r), lin(g), lin(b), 1)
    bsdf.inputs["Base Color"].default_value = col
    bsdf.inputs["Roughness"].default_value = rough
    if emit:
        bsdf.inputs["Emission Color"].default_value = col
        bsdf.inputs["Emission Strength"].default_value = emit
    _mats[name] = m
    return m

def box(name, x, y, z, sx, sy, sz, m, bevel=0.0):
    """Куб от (x,y,z) размером (sx,sy,sz)."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x + sx / 2, y + sy / 2, z + sz / 2))
    o = bpy.context.active_object
    o.name = name
    o.scale = (sx, sy, sz)
    if bevel:
        bm = o.modifiers.new("bev", "BEVEL"); bm.width = bevel; bm.segments = 3
    o.data.materials.append(m)
    bpy.ops.object.shade_smooth() if False else None
    return o

def sphere(name, x, y, z, r, m):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=(x, y, z), segments=24, ring_count=16)
    o = bpy.context.active_object; o.name = name
    o.data.materials.append(m); bpy.ops.object.shade_smooth()
    return o

# ── пол (воксельные плитки 7×6) + ковёр ──
floorA, floorB = mat("floorA", "#3b3553"), mat("floorB", "#332f49")
rugA, rugB = mat("rugA", "#6b5aa0"), mat("rugB", "#5a4b8a")
for gx in range(7):
    for gy in range(6):
        rug = 1 <= gx <= 5 and 1 <= gy <= 4
        c = (rugA if (gx + gy) % 2 else rugB) if rug else (floorA if (gx + gy) % 2 else floorB)
        box(f"t{gx}_{gy}", gx, gy, -0.12 if rug else -0.15, 1, 1, 0.12 if rug else 0.15, c)

# ── короткий бэкдроп-стенка с окном (без огромных стен — диорама на градиенте) ──
wall = mat("wall", "#3c3760")
box("wallBack", 0.8, -0.18, 0, 4.6, 0.2, 2.0, wall)
box("window", 3.5, -0.04, 0.7, 1.6, 0.08, 0.95, mat("win", "#8fc0f0", rough=0.2, emit=1.8))
box("winBar", 4.28, -0.05, 0.7, 0.06, 0.1, 0.95, wall)
box("winBarH", 3.5, -0.05, 1.17, 1.6, 0.1, 0.06, wall)

# ── стол ──
deskBody, deskTop = mat("deskBody", "#7a5c44"), mat("deskTop", "#a07a57")
box("deskBody", 2.0, 0.8, 0, 3.2, 1.1, 1.4, deskBody)
box("deskTop", 1.85, 0.65, 1.4, 3.5, 1.4, 0.18, deskTop, bevel=0.02)

# ── монитор (экран = отдельный объект для проекции) ──
monBody = mat("monBody", "#232a38")
box("monStand", 4.0, 1.15, 1.4, 0.3, 0.5, 0.2, monBody)
box("monBody", 3.55, 0.98, 1.58, 1.5, 0.22, 1.15, monBody, bevel=0.02)
screen = box("Screen", 3.62, 0.95, 1.66, 1.36, 0.03, 0.99, mat("screen", "#11151f", rough=0.15, emit=0.35))

# ── клавиатура ──
box("kbd", 2.2, 1.7, 1.58, 1.0, 0.45, 0.06, mat("kbd", "#cbd5e0"), bevel=0.01)

# ── стул ──
chair = mat("chair", "#3f4a5e")
box("seat", 2.5, 3.3, 0, 1.1, 1.0, 0.78, chair, bevel=0.03)
box("back", 2.5, 4.0, 0.78, 1.1, 0.22, 1.0, chair, bevel=0.03)

# ── растение ──
box("pot", 0.3, 4.6, 0, 0.7, 0.7, 0.7, mat("pot", "#7a5230"))
g = mat("plant", "#48bb78")
sphere("leaf1", 0.65, 4.95, 1.35, 0.4, g)
sphere("leaf2", 0.5, 4.8, 1.6, 0.26, mat("plant2", "#38a169"))
sphere("leaf3", 0.82, 5.05, 1.55, 0.24, g)

# ── ОСЬМИНОГ (воксельный, сидит на стуле, лицом к камере) ──
bodyM, headM, armM = mat("octoBody", "#7c4fd6"), mat("octoHead", "#9f7aea"), mat("octoArm", "#b794f4")
bx, by = 2.62, 3.05
box("octoBody", bx, by, 0.78, 0.95, 0.8, 0.85, bodyM, bevel=0.12)
head = box("octoHead", bx - 0.05, by - 0.03, 1.6, 1.05, 0.86, 0.92, headM, bevel=0.16)
# глаза + зрачки на передней грани (-y, к камере)
eyeY = by - 0.06
sphere("eyeL", bx + 0.28, eyeY, 2.05, 0.17, mat("eyeW", "#ffffff"))
sphere("eyeR", bx + 0.72, eyeY, 2.05, 0.17, mat("eyeW2", "#ffffff"))
sphere("pupL", bx + 0.28, eyeY - 0.12, 2.05, 0.08, mat("pupil", "#241a40"))
sphere("pupR", bx + 0.72, eyeY - 0.12, 2.05, 0.08, mat("pupil2", "#241a40"))
# щупальца — стопки наклонных кубиков (воксельный изгиб) вперёд/вниз
def tentacle(name, sx, sy, dirx):
    px, py, pz = sx, sy, 0.78
    w = 0.26
    for i in range(4):
        box(f"{name}{i}", px, py, pz - 0.18, w, 0.26, 0.22, armM, bevel=0.06)
        px += dirx * 0.12
        py += 0.12
        pz -= 0.16
        w = max(0.14, w - 0.03)
tentacle("armL1", bx + 0.05, by + 0.7, -1)
tentacle("armL2", bx + 0.2, by + 0.78, -1)
tentacle("armR1", bx + 0.64, by + 0.7, 1)
tentacle("armR2", bx + 0.5, by + 0.78, 1)

# ── камера (истинная изометрия) ──
target = Vector((3.0, 2.7, 0.85))
bpy.ops.object.empty_add(location=target)
empty = bpy.context.active_object
cam_data = bpy.data.cameras.new("Cam"); cam_data.type = "ORTHO"; cam_data.ortho_scale = 7.4
cam = bpy.data.objects.new("Cam", cam_data)
bpy.context.collection.objects.link(cam)
cam.location = target + Vector((9, -9, 9))
tc = cam.constraints.new("TRACK_TO"); tc.target = empty
tc.track_axis = "TRACK_NEGATIVE_Z"; tc.up_axis = "UP_Y"
bpy.context.scene.camera = cam

# ── свет ──
sun_d = bpy.data.lights.new("Sun", "SUN"); sun_d.energy = 4.6; sun_d.angle = math.radians(10)
sun = bpy.data.objects.new("Sun", sun_d); bpy.context.collection.objects.link(sun)
sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(35))
fill_d = bpy.data.lights.new("Fill", "AREA"); fill_d.energy = 380; fill_d.size = 14
fill = bpy.data.objects.new("Fill", fill_d); bpy.context.collection.objects.link(fill)
fill.location = (-3, -9, 7); fill.rotation_euler = (math.radians(55), 0, math.radians(-28))
# мир — лёгкая фиолетовая подсветка
world = bpy.data.worlds.new("W"); bpy.context.scene.world = world; world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
bg.inputs[0].default_value = (0.10, 0.09, 0.15, 1); bg.inputs[1].default_value = 1.2

# ── рендер ──
sc = bpy.context.scene
sc.render.engine = "BLENDER_EEVEE"
try:
    sc.eevee.taa_render_samples = 64
except Exception:
    pass
sc.render.film_transparent = True
sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "RGBA"
sc.render.filepath = OUT_PNG

# проекция точек в пиксели (для оверлеев)
bpy.context.view_layer.update()
def px(co):
    v = world_to_camera_view(sc, cam, Vector(co))
    return [round(v.x * RES_X, 1), round((1 - v.y) * RES_Y, 1)]
coords = {
    "res": [RES_X, RES_Y],
    # 4 угла плоскости экрана (грань -y монитора, обращённая к камере)
    "screen": {
        "tl": px((3.62, 0.95, 1.66 + 0.99)),
        "tr": px((3.62 + 1.36, 0.95, 1.66 + 0.99)),
        "br": px((3.62 + 1.36, 0.95, 1.66)),
        "bl": px((3.62, 0.95, 1.66)),
    },
    "headTop": px((bx + 0.5, by - 0.06, 2.55)),
}
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(coords, f, ensure_ascii=False, indent=2)

_log("engine=" + sc.render.engine + " starting render…")
bpy.ops.render.render(write_still=True)
_log("RENDER DONE " + OUT_PNG + " exists=" + str(os.path.exists(OUT_PNG)))
print("RENDER DONE", OUT_PNG)
