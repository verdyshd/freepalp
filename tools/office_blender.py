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

def hex2lin(hexc):
    n = int(hexc[1:], 16)
    f = lambda c: (c / 255) ** 2.2
    return (f((n >> 16) & 255), f((n >> 8) & 255), f(n & 255), 1)

def mat_grad(name, hex_bot, hex_top, rough=0.42):
    """Материал с вертикальным градиентом (низ→верх) — оранжево-жёлтая голова маскота."""
    if name in _mats:
        return _mats[name]
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; bsdf = nt.nodes.get("Principled BSDF")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    nt.links.new(tc.outputs["Generated"], sep.inputs[0])
    nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
    ramp.color_ramp.elements[0].position = 0.18; ramp.color_ramp.elements[0].color = hex2lin(hex_bot)
    ramp.color_ramp.elements[1].position = 0.82; ramp.color_ramp.elements[1].color = hex2lin(hex_top)
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = rough
    _mats[name] = m
    return m

def ellipsoid(name, loc, scale, quat, m):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=tuple(loc), segments=22, ring_count=14)
    o = bpy.context.active_object; o.name = name
    o.scale = scale
    if quat is not None:
        o.rotation_mode = "QUATERNION"; o.rotation_quaternion = quat
    o.data.materials.append(m); bpy.ops.object.shade_smooth()
    return o

# направление камеры (зеркальный «наоборот» изо-вид) — задаётся ОДИН раз, юзают осьминог+камера
CAM_OFFSET = Vector((-9, -9, 9))
camdir = Vector(CAM_OFFSET); camdir.normalize()

# доп. поворот всей комнаты вокруг центра («ещё повернуть»). Экран/лицо компенсируем,
# чтобы после поворота они по-прежнему смотрели в камеру.
ROOM_C = Vector((3.8, 3.0, 0.0))
ROT_Z = math.radians(0)            # стол у дальней стены — поворот не нужен, ставим раскладкой
def _rotz_about(p, ang, c=ROOM_C):
    v = Vector(p) - c
    ca, sa = math.cos(ang), math.sin(ang)
    return (c.x + v.x * ca - v.y * sa, c.y + v.x * sa + v.y * ca, p[2])
# направление к камере В ЛОКАЛЕ ДО поворота сцены (обратная компенсация)
eyedir = Vector(_rotz_about(camdir + ROOM_C, -ROT_Z)) - ROOM_C
eyedir.normalize()

def cyl(name, x, y, z, r, h, m, rot=None):
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=h, location=(x, y, z))
    o = bpy.context.active_object; o.name = name
    if rot is not None:
        o.rotation_euler = rot
    o.data.materials.append(m); bpy.ops.object.shade_smooth()
    return o

# ── пол 8×7 + ковёр ──
floorA, floorB = mat("floorA", "#3b3553"), mat("floorB", "#332f49")
rugA, rugB = mat("rugA", "#6b5aa0"), mat("rugB", "#5a4b8a")
for gx in range(8):
    for gy in range(7):
        rug = 1 <= gx <= 5 and 1 <= gy <= 4
        c = (rugA if (gx + gy) % 2 else rugB) if rug else (floorA if (gx + gy) % 2 else floorB)
        box(f"t{gx}_{gy}", gx, gy, -0.12 if rug else -0.15, 1, 1, 0.12 if rug else 0.15, c)

# ── задняя стена с окном (дальняя сторона, y≈6 — «верх» сцены) ──
wall = mat("wall", "#3c3760")
box("wallBack", 0.3, 6.0, 0, 7.4, 0.2, 2.5, wall)
box("window", 1.4, 6.02, 1.2, 1.6, 0.08, 0.95, mat("win", "#8fc0f0", rough=0.2, emit=1.8))
box("winBar", 2.18, 6.03, 1.2, 0.06, 0.1, 0.95, wall)
box("winBarH", 1.4, 6.03, 1.67, 1.6, 0.1, 0.06, wall)

bookCols = ["#e2574c", "#4a90d9", "#f6b73c", "#6ab04c", "#9b59b6", "#e67e22", "#16a085"]

# ── книжный шкаф со множеством книг (у дальней стены, левее) ──
shelfM = mat("shelf", "#6a4a2c")
box("shelf", 0.4, 5.4, 0, 1.6, 0.6, 2.5, shelfM)
for z in (0.9, 1.7):
    box(f"shp{z}", 0.42, 5.42, z, 1.56, 0.56, 0.07, mat(f"shp{z}", "#50361c"))
for r, z in enumerate((0.95, 1.75, 2.5)):          # 3 ряда книг (верхний — на крыше)
    for i in range(6):
        box(f"bk{r}_{i}", 0.52 + i * 0.21, 5.5, z, 0.17, 0.4, 0.46 + ((i + r) % 3) * 0.08,
            mat(f"bk{r}{i}", bookCols[(i + r) % 7]))

# ── гитара, прислонённая к дальней стене (правый угол) ──
gBody = mat("guitarBody", "#c98a44", rough=0.35); gNeck = mat("guitarNeck", "#2f2113")
ellipsoid("guitarBody", (7.1, 5.5, 1.0), (0.5, 0.5, 0.66), None, gBody)
ellipsoid("guitarWaist", (7.1, 5.5, 1.55), (0.36, 0.36, 0.42), None, gBody)
box("guitarNeck", 7.02, 5.52, 1.95, 0.16, 0.12, 1.5, gNeck)
box("guitarHead", 7.0, 5.53, 3.4, 0.22, 0.12, 0.3, gNeck)
cyl("guitarHole", 7.1, 5.38, 1.05, 0.13, 0.04, mat("ghole", "#3a2a18"), rot=(math.radians(90), 0, 0))

# ── стол у дальней стены (вверх кадра) ──
deskBody, deskTop = mat("deskBody", "#7a5c44"), mat("deskTop", "#a07a57")
box("deskBody", 2.6, 4.7, 0, 3.6, 1.0, 1.4, deskBody)
box("deskTop", 2.45, 4.55, 1.4, 3.9, 1.35, 0.18, deskTop, bevel=0.02)

# ── монитор (экран на -y грани — к камере; стоит на дальнем краю стола) ──
monBody = mat("monBody", "#232a38")
box("monStand", 4.9, 5.05, 1.4, 0.3, 0.45, 0.2, monBody)
box("monBody", 4.45, 5.0, 1.58, 1.5, 0.22, 1.15, monBody, bevel=0.02)
screen = box("Screen", 4.52, 4.93, 1.66, 1.36, 0.05, 0.99, mat("screen", "#11151f", rough=0.15, emit=0.35))

# ── клавиатура (ближе к переднему краю стола) ──
box("kbd", 4.15, 4.62, 1.58, 1.0, 0.42, 0.06, mat("kbd", "#cbd5e0"), bevel=0.01)

# ── стул (перед столом, спинкой к столу) ──
chair = mat("chair", "#3f4a5e")
box("seat", 2.35, 3.05, 0, 1.1, 1.0, 0.78, chair, bevel=0.03)
box("back", 2.35, 3.83, 0.78, 1.1, 0.22, 1.05, chair, bevel=0.03)

g = mat("plant", "#48bb78")
# ── растение №1 (на крыше шкафа, у дальней стены) ──
box("pot", 0.6, 5.45, 2.5, 0.6, 0.5, 0.5, mat("pot", "#7a5230"))
sphere("leaf1", 0.9, 5.7, 3.3, 0.36, g)
sphere("leaf2", 0.75, 5.55, 3.55, 0.24, mat("plant2", "#38a169"))
sphere("leaf3", 1.05, 5.78, 3.5, 0.22, g)

# ── растение №2 (высокое, передний правый угол) ──
box("pot2", 6.7, 1.4, 0, 0.66, 0.66, 0.66, mat("pot2b", "#8a5a36"))
sphere("p2a", 7.03, 1.73, 1.2, 0.4, g)
sphere("p2b", 6.88, 1.55, 1.55, 0.32, mat("plant3", "#3aa05f"))
sphere("p2c", 7.15, 1.8, 1.5, 0.28, g)

# ── торшер (напольная лампа, у дальней стены справа) ──
lampM = mat("lampM", "#2d3748")
cyl("lampBase", 6.2, 5.5, 0.04, 0.3, 0.08, lampM)
cyl("lampPole", 6.2, 5.5, 1.3, 0.05, 2.5, lampM)
ellipsoid("lampShade", (6.2, 5.5, 2.45), (0.34, 0.34, 0.3), None, mat("lampShade", "#ffd36a", emit=1.4))

# ── зона отдыха: круглый коврик + два пуфика (передний открытый пол) ──
cyl("roundRug", 4.6, 1.9, -0.07, 1.1, 0.05, mat("roundRug", "#c2724f"))
ellipsoid("cushion", (4.6, 1.9, 0.26), (0.66, 0.66, 0.26), None, mat("cushion", "#3aa6a0"))
ellipsoid("cushion2", (5.4, 2.5, 0.2), (0.46, 0.46, 0.2), None, mat("cushion2", "#d96ba0"))

# ── кружка на столе + стопка книг ──
cyl("mug", 5.7, 4.8, 1.69, 0.13, 0.22, mat("mugM", "#e2574c"))
cyl("mugIn", 5.7, 4.8, 1.74, 0.09, 0.18, mat("mugIn", "#7a2018"))
for i in range(3):
    box(f"deskBook{i}", 3.0, 4.85, 1.58 + i * 0.09, 0.5, 0.34, 0.09, mat(f"dbk{i}", bookCols[(i + 1) % 7]))

# ── ОСЬМИНОГ-МАСКОТ (как лого FreePalp): круглая оранжево-жёлтая голова + розовые щупальца ──
hc = Vector((2.9, 3.45, 1.74))                           # центр головы (перед столом, слева от монитора)
headM = mat_grad("octoHead", "#ef8a28", "#ffd24a")       # низ оранжевый → верх жёлтый
ellipsoid("octoHead", hc, (0.70, 0.66, 0.64), None, headM)
# глаза на ПОВЕРХНОСТИ головы, к камере (eyedir = camdir с компенсацией поворота комнаты)
eyeDark = mat("eyeDark", "#241208", rough=0.32)
shineM = mat("eyeShine", "#ffffff", rough=0.2)
front = hc + eyedir * 0.58 + Vector((0, 0, -0.04))
perp = Vector((eyedir.y, -eyedir.x, 0)); perp.normalize()   # горизонталь экрана
up = Vector((0, 0, 1))
for side, nm in ((-1, "L"), (1, "R")):
    ec = front + perp * (0.17 * side)
    ellipsoid("eye" + nm, ec, (0.12, 0.12, 0.15), None, eyeDark)
    sh = ec + eyedir * 0.07 + up * 0.05 - perp * (0.035 * side)
    sphere("shine" + nm, sh.x, sh.y, sh.z, 0.035, shineM)
# щупальца — 6 пухлых розовых, с подкрученными кончиками (2 сегмента)
armTop = mat("octoArmTop", "#ff6bb6", rough=0.36)
armBot = mat("octoArmBot", "#e84fa0", rough=0.42)
baseZ = hc.z - 0.46
for i in range(6):
    a = math.radians(i * 60 + 28)
    outw = Vector((math.cos(a), math.sin(a), 0))
    base = Vector((hc.x + 0.34 * math.cos(a), hc.y + 0.34 * math.sin(a), baseZ))
    d1 = (outw + Vector((0, 0, -1.15))); d1.normalize()      # сегмент вниз-наружу
    s1 = base + d1 * 0.26
    ellipsoid(f"arm{i}a", s1, (0.18, 0.18, 0.34), d1.to_track_quat("Z", "Y"), armTop if i % 2 == 0 else armBot)
    tipBase = base + d1 * 0.46
    d2 = (outw * 1.2 + Vector((0, 0, 0.55))); d2.normalize()  # кончик подкручен вверх
    s2 = tipBase + d2 * 0.16
    ellipsoid(f"arm{i}b", s2, (0.13, 0.13, 0.2), d2.to_track_quat("Z", "Y"), armTop if i % 2 == 0 else armBot)

# ── повернуть всю комнату вокруг центра (камеры/света ещё нет — берём только меши) ──
bpy.ops.object.select_all(action="DESELECT")
bpy.ops.object.empty_add(location=ROOM_C)
room_root = bpy.context.active_object; room_root.name = "RoomRoot"
for o in list(bpy.context.scene.objects):
    if o.type == "MESH":
        o.select_set(True)
room_root.select_set(True)
bpy.context.view_layer.objects.active = room_root
bpy.ops.object.parent_set(type="OBJECT", keep_transform=True)
room_root.rotation_euler = (0, 0, ROT_Z)
bpy.context.view_layer.update()

# ── камера (истинная изометрия, зеркальный «наоборот» вид; стол у дальней стены) ──
target = Vector((4.0, 3.6, 0.9))
bpy.ops.object.empty_add(location=target)
empty = bpy.context.active_object
cam_data = bpy.data.cameras.new("Cam"); cam_data.type = "ORTHO"; cam_data.ortho_scale = 9.7
cam = bpy.data.objects.new("Cam", cam_data)
bpy.context.collection.objects.link(cam)
cam.location = target + CAM_OFFSET
tc = cam.constraints.new("TRACK_TO"); tc.target = empty
tc.track_axis = "TRACK_NEGATIVE_Z"; tc.up_axis = "UP_Y"
bpy.context.scene.camera = cam

# ── свет ──
sun_d = bpy.data.lights.new("Sun", "SUN"); sun_d.energy = 5.4; sun_d.angle = math.radians(10)
sun = bpy.data.objects.new("Sun", sun_d); bpy.context.collection.objects.link(sun)
sun.rotation_euler = (math.radians(48), math.radians(-12), math.radians(-35))   # светит со стороны новой камеры
fill_d = bpy.data.lights.new("Fill", "AREA"); fill_d.energy = 600; fill_d.size = 16
fill = bpy.data.objects.new("Fill", fill_d); bpy.context.collection.objects.link(fill)
fill.location = (-9, -9, 8); fill.rotation_euler = (math.radians(52), 0, math.radians(28))
fill2_d = bpy.data.lights.new("Fill2", "AREA"); fill2_d.energy = 320; fill2_d.size = 14
fill2 = bpy.data.objects.new("Fill2", fill2_d); bpy.context.collection.objects.link(fill2)
fill2.location = (10, -6, 7); fill2.rotation_euler = (math.radians(55), 0, math.radians(-70))
# мир — лёгкая фиолетовая подсветка
world = bpy.data.worlds.new("W"); bpy.context.scene.world = world; world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
bg.inputs[0].default_value = (0.13, 0.12, 0.18, 1); bg.inputs[1].default_value = 1.7

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
def pxr(co):                                   # проекция точки С УЧЁТОМ поворота комнаты
    return px(_rotz_about(co, ROT_Z))
coords = {
    "res": [RES_X, RES_Y],
    # 4 угла плоскости экрана (грань -y монитора, обращённая к камере) после поворота
    "screen": {
        "tl": pxr((4.52, 4.93, 1.66 + 0.99)),
        "tr": pxr((4.52 + 1.36, 4.93, 1.66 + 0.99)),
        "br": pxr((4.52 + 1.36, 4.93, 1.66)),
        "bl": pxr((4.52, 4.93, 1.66)),
    },
    "headTop": pxr((hc.x, hc.y, hc.z + 0.66)),
}
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(coords, f, ensure_ascii=False, indent=2)

_log("engine=" + sc.render.engine + " starting render…")
bpy.ops.render.render(write_still=True)
_log("RENDER DONE " + OUT_PNG + " exists=" + str(os.path.exists(OUT_PNG)))
print("RENDER DONE", OUT_PNG)
