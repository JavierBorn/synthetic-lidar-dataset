import c4d
import c4d.modules.mograph as mo
import struct
import math
import os
import json

# ============================================================
#  ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================
matrix_objects = []          # список (obj, class_id)
class_map = {}               # имя объекта -> class_id (для JSON)
cam = None
initialized = False
last_saved_frame = -100

# ---- Параметры FOV ----
HFOV_DEG = 54.4322
VFOV_DEG = 32.2688

# ---- ПАРАМЕТРЫ LIDAR ----
NUM_CHANNELS = 64
HORIZONTAL_RESOLUTION = 0.2
NUM_COLUMNS = int(360.0 / HORIZONTAL_RESOLUTION)

VERTICAL_ANGLES = []
for i in range(NUM_CHANNELS):
    if i < 32:
        angle = -24.8 + i * 0.8
    else:
        angle = -0.8 + (i - 32) * 0.5
    VERTICAL_ANGLES.append(round(angle, 2))

# ---- РЕЕСТР СЕМАНТИКИ ----
CLASSES = {
    "sensor":   0,
    "vehicle":  1,
    "car":      1,
    "building": 2,
    "tree":     3,
    "sign":     4,
    "road":     5,
    "pole":     6,
    "person":   7,
    "bike":     8,
    "terrain":  9,
}
CLASS_SENSOR = CLASSES["sensor"]


# ============================================================
#  МАТЕМАТИКА ЛИДАРА
# ============================================================
def find_nearest_channel(elevation_deg):
    nearest_idx = 0
    min_diff = float('inf')
    for i, angle in enumerate(VERTICAL_ANGLES):
        diff = abs(elevation_deg - angle)
        if diff < min_diff:
            min_diff = diff
            nearest_idx = i
    return nearest_idx


def find_column(azimuth_deg):
    azimuth_deg = azimuth_deg % 360.0
    if azimuth_deg < 0:
        azimuth_deg += 360.0
    col = int(azimuth_deg / HORIZONTAL_RESOLUTION)
    if col >= NUM_COLUMNS:
        col = NUM_COLUMNS - 1
    return col


def semantic_from_name(name):
    key = name.split("_")[0].strip().lower()
    if key in CLASSES:
        return key, CLASSES[key]
    return None, None


# ============================================================
#  MATERIAL READING
#  Читаем Roughness и Reflection Strength из Legacy-материала.
#  В Legacy они лежат внутри слоя отражения (Reflectance Layer 0).
#  Никаких захардкоженных значений.
# ============================================================

def _find_material_on_object(obj):
    """Материал через Texture tag на самом объекте."""
    tag = obj.GetTag(c4d.Ttexture)
    if tag is None:
        return None
    mat = tag.GetMaterial()
    if mat is None:
        return None
    return mat


def _find_material_on_parent(obj):
    """Материал через Texture tag у родителей вверх по иерархии."""
    parent = obj.GetUp()
    depth = 0
    while parent is not None and depth < 5:
        tag = parent.GetTag(c4d.Ttexture)
        if tag is not None:
            mat = tag.GetMaterial()
            if mat is not None:
                return mat
        parent = parent.GetUp()
        depth += 1
    return None


def _find_material_by_name(obj, doc):
    """
    Fallback: ищет материал, имя которого совпадает
    с первым словом имени объекта (до первого '_').
    """
    key = obj.GetName().split("_")[0].strip().lower()

    mat = doc.GetFirstMaterial()
    while mat is not None:
        mat_name = mat.GetName().strip().lower()
        if mat_name == key:
            return mat
        if mat_name.startswith(key):
            return mat
        mat = mat.GetNext()

    return None


def _find_material(obj, doc):
    """
    Возвращает (mat, found_on).
    Порядок: obj -> parent -> by_name
    """
    mat = _find_material_on_object(obj)
    if mat is not None:
        return mat, "obj"

    mat = _find_material_on_parent(obj)
    if mat is not None:
        return mat, "parent"

    mat = _find_material_by_name(obj, doc)
    if mat is not None:
        return mat, "by_name"

    return None, None


def _read_material_params(mat):
    """
    Читает Roughness и Reflection Strength.
    Основной путь — через Reflectance Layer 0 (Legacy).
    Возвращает (roughness, reflection_strength) или (None, None).
    """
    if mat is None:
        return None, None

    # --- Способ 1: через слой отражения (правильный для Legacy) ---
    try:
        layer = mat.GetReflectionLayerIndex(0)
        if layer is not None:
            layer_id = layer.GetDataID()

            roughness = None
            reflection = None

            try:
                roughness = float(
                    mat[layer_id + c4d.REFLECTION_LAYER_MAIN_VALUE_ROUGHNESS]
                )
            except Exception:
                pass

            try:
                reflection = float(
                    mat[layer_id + c4d.REFLECTION_LAYER_MAIN_VALUE_REFLECTION]
                )
            except Exception:
                pass

            if roughness is not None or reflection is not None:
                return roughness, reflection
    except Exception as e:
        print(f"    (layer error: {e})")

    # --- Способ 2: старые прямые ID (для некоторых сборок) ---
    IDS_ROUGHNESS = [
        getattr(c4d, "MATERIAL_REFLECTION_ROUGHNESS", None),
    ]
    IDS_REFLECTION = [
        getattr(c4d, "MATERIAL_REFLECTION_REFLECTIONSTRENGTH", None),
        getattr(c4d, "MATERIAL_REFLECTION_STRENGTH", None),
    ]

    roughness = None
    for desc_id in IDS_ROUGHNESS:
        if desc_id is None:
            continue
        try:
            roughness = float(mat[desc_id])
            break
        except Exception:
            continue

    reflection = None
    for desc_id in IDS_REFLECTION:
        if desc_id is None:
            continue
        try:
            reflection = float(mat[desc_id])
            break
        except Exception:
            continue

    return roughness, reflection


def _collect_materials(matrix_objects_list, doc):
    """Собирает материалы по всем Matrix Object'ам."""
    result = {}

    for obj, cid in matrix_objects_list:
        obj_name = obj.GetName()
        mat, found_on = _find_material(obj, doc)

        roughness, reflection = _read_material_params(mat)

        if roughness is not None and reflection is not None:
            proxy = reflection * (1.0 - 0.5 * roughness)
            proxy = round(proxy, 4)
        else:
            proxy = None

        entry = {
            "object": obj_name,
            "material": mat.GetName() if mat is not None else None,
            "found_on": found_on,
            "roughness": round(roughness, 4) if roughness is not None else None,
            "reflection_strength": round(reflection, 4) if reflection is not None else None,
            "reflectance_proxy": proxy,
        }

        result.setdefault(str(cid), []).append(entry)

        if roughness is None or reflection is None:
            print(f"  ⚠️ {obj_name}: параметры не прочитаны "
                  f"(mat={entry['material']}, on={found_on})")
        else:
            print(f"  ✓ {obj_name}: rough={roughness:.3f} "
                  f"refl={reflection:.3f} proxy={proxy:.3f} (on={found_on})")

    return result


# ============================================================
#  ИНИЦИАЛИЗАЦИЯ
# ============================================================
def init():
    global matrix_objects, cam, initialized, class_map

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return False

    matrix_objects = []
    class_map = {}
    unknown = []

    for obj in doc.GetObjects():
        if not obj.IsInstanceOf(c4d.Omgmatrix):
            continue

        name = obj.GetName()
        semantic, cid = semantic_from_name(name)

        if cid is None:
            unknown.append(name)
            print(f"❌ Неизвестная семантика: '{name}'")
            continue

        md = mo.GeGetMoData(obj)
        if md is None:
            continue
        matrices = md.GetArray(c4d.MODATA_MATRIX)
        if not matrices:
            continue

        matrix_objects.append((obj, cid))
        class_map[name] = cid
        print(f"✓ {name} -> '{semantic}' (class {cid}, точек: {len(matrices)})")

    if unknown:
        print(f"⚠️ Пропущено объектов: {len(unknown)}.")

    if not matrix_objects:
        print("❌ Ни одного распознанного Matrix Object")
        return False

    # ---- Камера LiDAR ----
    cam = None
    for obj in doc.GetObjects():
        if obj.GetName().lower() == "lidar":
            cam = obj
            break
    if cam is None and hasattr(doc, 'GetActiveCamera'):
        cam = doc.GetActiveCamera()
    if cam is None:
        print("❌ Камера LiDAR не найдена")
        return False

    # ---- Папка рядом с проектом ----
    project_path = doc.GetDocumentPath() or os.path.expanduser("~/Desktop")
    frames_folder = os.path.join(project_path, "frames")
    os.makedirs(frames_folder, exist_ok=True)

    # ---- Сбор материалов ----
    print("")
    print("Материалы:")
    materials_by_class = _collect_materials(matrix_objects, doc)

    # ---- classes.json ----
    classes_file = os.path.join(frames_folder, "classes.json")
    with open(classes_file, "w", encoding="utf-8") as f:
        json.dump({
            "classes_by_id": {str(v): k for k, v in CLASSES.items()},
            "objects_in_scene": class_map,
            "materials_by_class": materials_by_class,
            "metadata": {
                "export_version": "2.0",
                "has_materials": any(
                    e["material"] is not None
                    for lst in materials_by_class.values() for e in lst
                ),
                "nir_wavelength_nm": 905,
            },
        }, f, indent=2, ensure_ascii=False)

    print("")
    print(f"Камера: {cam.GetName()}")
    print(f"Семантических объектов: {len(matrix_objects)}")
    print(f"LiDAR: {NUM_CHANNELS} каналов × {NUM_COLUMNS} колонок")
    print(f"classes.json: {classes_file}")

    initialized = True
    return True


# ============================================================
#  ПРОЕКЦИЯ ТОЧЕК ОДНОГО КЛАССА В ОБЩУЮ ТАБЛИЦУ
# ============================================================
def project_to_lidar(points, class_id, lidar_table, inv_cam_mg):
    hfov_half = math.radians(HFOV_DEG / 2.0)
    vfov_half = math.radians(VFOV_DEG / 2.0)

    for p in points:
        local_p = inv_cam_mg * p

        z_dist = local_p.z
        if z_dist <= 0:
            continue

        x_angle = abs(math.atan2(local_p.x, z_dist))
        y_angle = abs(math.atan2(local_p.y, z_dist))
        if x_angle > hfov_half or y_angle > vfov_half:
            continue

        azimuth = math.degrees(math.atan2(local_p.x, z_dist))
        elevation = math.degrees(math.atan2(
            local_p.y, math.sqrt(local_p.x ** 2 + local_p.z ** 2)))

        channel = find_nearest_channel(elevation)
        column = find_column(azimuth)
        dist = local_p.GetLength()

        key = (channel, column)
        old = lidar_table.get(key)
        if old is None or dist < old[0]:
            lidar_table[key] = (dist, p.x, p.y, p.z, class_id)


# ============================================================
#  ЭКСПОРТ ОДНОГО КАДРА (НЕ ТРОНУТ)
# ============================================================
def export_frame(frame_number, cam_pos, cam_mg):
    global matrix_objects, cam

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return False

    try:
        inv_cam_mg = ~cam_mg
    except:
        inv_cam_mg = cam_mg.GetInverse()

    lidar_table = {}

    for obj, cid in matrix_objects:
        md = mo.GeGetMoData(obj)
        if md is None:
            continue
        matrices = md.GetArray(c4d.MODATA_MATRIX)
        if not matrices:
            continue

        obj_mg = obj.GetMg()
        points = [obj_mg * m.off for m in matrices]
        project_to_lidar(points, cid, lidar_table, inv_cam_mg)

    if not lidar_table:
        print(f"⚠️ Кадр {frame_number:04d}: нет точек")
        return False

    filtered_points = []
    distances = []
    for (channel, column), (dist, x, y, z, cid) in lidar_table.items():
        filtered_points.append((channel, column, x, y, z, dist, cid))
        distances.append(dist)

    filtered_points.sort(key=lambda p: (p[0], p[1]))

    min_dist = min(distances)
    max_dist = max(distances)
    if max_dist == min_dist:
        norm_factor = 1.0
    else:
        norm_factor = 1.0 / (max_dist - min_dist)

    final_points = []
    for channel, column, x, y, z, dist, cid in filtered_points:
        if max_dist == min_dist:
            norm = 0.0
        else:
            norm = (dist - min_dist) * norm_factor
        intensity = 0.8 * (1.0 - norm) + 0.1 * norm
        intensity = max(0.1, min(0.95, intensity))
        final_points.append((x, y, z, intensity, cid))

    final_points.append((cam_pos.x, cam_pos.y, cam_pos.z, 1.0, CLASS_SENSOR))

    project_path = doc.GetDocumentPath()
    if not project_path:
        project_path = os.path.expanduser("~/Desktop")

    frames_folder = os.path.join(project_path, "frames")
    os.makedirs(frames_folder, exist_ok=True)
    filename = os.path.join(frames_folder,
                            f"my_lidar_frame_{frame_number:04d}.bin")

    try:
        with open(filename, "wb") as f:
            for x, y, z, intensity, cid in final_points:
                f.write(struct.pack("ffffi", z, x, y, intensity, cid))
        print(f"✅ Кадр {frame_number:04d} "
              f"({len(final_points)} точек)")
        return True
    except Exception as e:
        print(f"❌ Ошибка кадра {frame_number}: {e}")
        return False


# ============================================================
#  ГЛАВНАЯ ФУНКЦИЯ PYTHON TAG
# ============================================================
def main():
    global initialized, last_saved_frame, cam

    if not initialized:
        if not init():
            return

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return

    if cam is None:
        return

    cam_pos = cam.GetMg().off
    cam_mg = cam.GetMg()

    frame = doc.GetTime().GetFrame(doc.GetFps())

    if frame - last_saved_frame >= 2:
        if export_frame(frame, cam_pos, cam_mg):
            last_saved_frame = frame
            c4d.EventAdd()