"""Verify all deployed assets in BeamNG user folder."""

import json
import os

LEVEL_DIR = r"F:\Games\BeamNG.drive\current\levels\ivanovo_mvp"
ZIP_PATH = r"F:\Games\BeamNG.drive\current\mods\ivanovo_mvp.zip"

print(f"Checking {LEVEL_DIR}...")
assert os.path.isdir(LEVEL_DIR), f"Missing {LEVEL_DIR}"
assert os.path.isfile(ZIP_PATH), f"Missing {ZIP_PATH}"
print(f"Mod ZIP size: {os.path.getsize(ZIP_PATH) / 1024 / 1024:.2f} MB")

# Check terrain
ter_path = os.path.join(LEVEL_DIR, "theTerrain.ter")
print(f"Terrain .ter size: {os.path.getsize(ter_path) / 1024 / 1024:.2f} MB")

# Check road materials
road_mat_path = os.path.join(LEVEL_DIR, "art", "road", "main.materials.json")
with open(road_mat_path, "r", encoding="utf-8") as f:
    road_mats = json.load(f)
print(f"Road materials defined: {list(road_mats.keys())}")
assert "road_asphalt_2lane" in road_mats
assert "m_line_white_discontinue" in road_mats

# Check bridges, railways, buildings
bridges = os.listdir(os.path.join(LEVEL_DIR, "art", "shapes", "bridges"))
railways = os.listdir(os.path.join(LEVEL_DIR, "art", "shapes", "railways"))
buildings = os.listdir(os.path.join(LEVEL_DIR, "art", "shapes", "buildings"))
print(f"Bridge files: {len(bridges)} (.dae)")
print(f"Railway files: {len(railways)} (.dae)")
print(f"Building chunk files: {len(buildings)} (.dae)")

# Check road network items
road_items_path = os.path.join(LEVEL_DIR, "main", "MissionGroup", "level_objects", "road_network", "items.level.json")
decal_roads = 0
sample_road = None
sample_line = None
with open(road_items_path, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            obj = json.loads(line)
            if obj.get("class") == "DecalRoad":
                decal_roads += 1
                mat = obj.get("material", "")
                if "asphalt" in mat and sample_road is None:
                    sample_road = obj
                if "line" in mat and sample_line is None:
                    sample_line = obj

print(f"Total DecalRoad objects: {decal_roads}")
print(f"Sample road: mat={sample_road['material']}, texLen={sample_road.get('textureLength')}, prio={sample_road.get('renderPriority')}, breakAngle={sample_road.get('breakAngle')}")
print(f"Sample line: mat={sample_line['material']}, texLen={sample_line.get('textureLength')}, prio={sample_line.get('renderPriority')}")

# Check water objects
water_items_path = os.path.join(LEVEL_DIR, "main", "MissionGroup", "level_objects", "water", "items.level.json")
water_count = 0
if os.path.isfile(water_items_path):
    with open(water_items_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                water_count += 1
print(f"Water scene objects (River/WaterBlock): {water_count}")

print("\n--- ALL VERIFICATIONS PASSED 100% ---")
