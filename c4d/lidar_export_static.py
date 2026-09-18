import c4d
import c4d.modules.mograph as mo
import struct
import os
import json


# ============================================================
# GLOBAL
# ============================================================

matrix_objects = []
class_map = {}

cam = None
initialized = False
last_saved_frame = -100


# ============================================================
# SEMANTIC CLASSES
# ============================================================

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

CLASS_SENSOR = 0


# ============================================================
# SEMANTIC FROM OBJECT NAME
# ============================================================

def semantic_from_name(name):
    key = name.split("_")[0].strip().lower()
    if key in CLASSES:
        return key, CLASSES[key]
    return None, None


# ============================================================
# MATERIAL READING (Legacy)
# ============================================================

def _find_material_on_object(obj):
    tag = obj.GetTag(c4d.Ttexture)
    if tag is None:
        return None
    mat = tag.GetMaterial()
    return mat


def _find_material_on_parent(obj):
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
    key = obj.GetName().split("_")[0].strip().lower()
    mat = doc.GetFirstMaterial()
    while mat is not None:
        mat_name = mat.GetName().strip().lower()
        if mat_name == key or mat_name.startswith(key):
            return mat
        mat = mat.GetNext()
    return None


def _find_material(obj, doc):
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
    Legacy material — Roughness и Reflection Strength
    читаются через Reflectance Layer 0.
    """
    if mat is None:
        return None, None

    # --- через слой отражения ---
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

    # --- старые прямые ID ---
    IDS_ROUGHNESS = [getattr(c4d, "MATERIAL_REFLECTION_ROUGHNESS", None)]
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
# INITIALIZATION
# ============================================================

def init():
    global matrix_objects
    global cam
    global initialized
    global class_map

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return False

    matrix_objects = []
    class_map = {}
    unknown = []

    # --------------------------------------------------------
    # FIND MATRIX OBJECTS
    # --------------------------------------------------------

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
        if matrices is None:
            continue

        matrix_objects.append((obj, cid))
        class_map[name] = cid
        print(f"✓ {name} -> '{semantic}' (class {cid}, точек: {len(matrices)})")

    if unknown:
        print(f"⚠️ Пропущено объектов: {len(unknown)}")

    if not matrix_objects:
        print("❌ Ни одного распознанного Matrix Object")
        return False

    # --------------------------------------------------------
    # FIND LIDAR CAMERA
    # --------------------------------------------------------

    cam = None
    for obj in doc.GetObjects():
        if obj.GetName().lower() == "lidar":
            cam = obj
            break

    if cam is None:
        if hasattr(doc, "GetActiveCamera"):
            cam = doc.GetActiveCamera()

    if cam is None:
        print("⚠️ Камера LiDAR не найдена")

    # --------------------------------------------------------
    # FRAMES FOLDER
    # --------------------------------------------------------

    project_path = doc.GetDocumentPath()
    if not project_path:
        project_path = os.path.expanduser("~/Desktop")

    frames_folder = os.path.join(project_path, "frames2")
    os.makedirs(frames_folder, exist_ok=True)

    # --------------------------------------------------------
    # COLLECT MATERIALS
    # --------------------------------------------------------

    print("")
    print("Материалы:")
    materials_by_class = _collect_materials(matrix_objects, doc)

    # --------------------------------------------------------
    # SAVE classes.json
    # --------------------------------------------------------

    classes_file = os.path.join(frames_folder, "classes.json")

    with open(classes_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "classes_by_id": {
                    str(v): k
                    for k, v in CLASSES.items()
                },
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
            },
            f,
            indent=2,
            ensure_ascii=False
        )

    # --------------------------------------------------------
    # INFO
    # --------------------------------------------------------

    print("")
    print("====================================")
    print("FULL POINT CLOUD EXPORT")
    print("====================================")
    print(f"Matrix Objects: {len(matrix_objects)}")

    if cam:
        print(f"Camera: {cam.GetName()}")

    print("LiDAR projection: DISABLED")
    print("FOV filtering: DISABLED")
    print("Ray selection: DISABLED")
    print("Nearest-point filtering: DISABLED")
    print("Semantic classes: ENABLED")
    print("Materials: ENABLED")
    print(f"classes.json: {classes_file}")
    print("====================================")
    print("")

    initialized = True
    return True


# ============================================================
# EXPORT ALL MATRIX POINTS
# ============================================================

def export_frame(frame_number, cam_pos=None):
    global matrix_objects

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return False

    all_points = []

    for obj, cid in matrix_objects:
        md = mo.GeGetMoData(obj)
        if md is None:
            continue
        matrices = md.GetArray(c4d.MODATA_MATRIX)
        if matrices is None or len(matrices) == 0:
            continue

        obj_mg = obj.GetMg()
        print(f"  {obj.GetName()}: {len(matrices)} points")

        for m in matrices:
            world_pos = obj_mg * m.off
            x = world_pos.x
            y = world_pos.y
            z = world_pos.z
            intensity = 1.0
            all_points.append((x, y, z, intensity, cid))

    if cam_pos is not None:
        all_points.append(
            (cam_pos.x, cam_pos.y, cam_pos.z, 1.0, CLASS_SENSOR)
        )

    if not all_points:
        print(f"⚠️ Кадр {frame_number:04d}: нет точек")
        return False

    project_path = doc.GetDocumentPath()
    if not project_path:
        project_path = os.path.expanduser("~/Desktop")

    frames_folder = os.path.join(project_path, "frames2")
    os.makedirs(frames_folder, exist_ok=True)

    filename = os.path.join(
        frames_folder,
        f"my_lidar_frame_{frame_number:04d}.bin"
    )

    try:
        with open(filename, "wb") as f:
            for x, y, z, intensity, cid in all_points:
                f.write(struct.pack("ffffi", z, x, y, intensity, cid))

        class_counts = {}
        for point in all_points:
            cid = point[4]
            class_counts[cid] = class_counts.get(cid, 0) + 1

        print("")
        print(f"✅ КАДР {frame_number:04d}")
        print(f"   Всего точек: {len(all_points)}")
        print(f"   Классов: {len(class_counts)}")

        for cid, count in sorted(class_counts.items()):
            print(f"      class {cid}: {count}")

        print(f"   File: {filename}")
        print("")
        return True

    except Exception as e:
        print(f"❌ Ошибка записи: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    global initialized
    global last_saved_frame
    global cam

    if not initialized:
        if not init():
            return

    doc = c4d.documents.GetActiveDocument()
    if doc is None:
        return

    fps = doc.GetFps()
    frame = doc.GetTime().GetFrame(fps)

    if frame - last_saved_frame >= 2:
        cam_pos = None
        if cam is not None:
            cam_pos = cam.GetMg().off

        if export_frame(frame, cam_pos):
            last_saved_frame = frame
            c4d.EventAdd()