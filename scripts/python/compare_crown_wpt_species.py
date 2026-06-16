"""
Compare crown-polygon species (field/segmentation ground truth) against the
waypoint Labelbox annotations, by spatial intersection.

Supports two crown-file schemas (auto-detected, multiple files allowed):
  - "trails"   : species in `tree_specie` as `Scientific name_CODE6_CODE4`,
                 lianas in `liana_specie_1..5` (same encoding).
  - "crownmap" : species code in a bare `mnemonic` column (lowercase),
                 clean taxonomy in `latin`/`genus`/`species`/`family`,
                 NO per-species liana columns (so liana comparison is skipped).

Logic
-----
- We compare ONLY on the 6-letter species code (uppercased). Entries with no
  valid 6-letter code (genus / family only) are dropped.
- Waypoints carry up to three tree annotations (`tree_A/B/C_lb_label`) and four
  "other" annotations (`other_a..d_lb_label`), encoded as `Name-CODE6-CODE4`.
- Points are reprojected to the crown CRS and joined `within` each crown.
- Aggregation is crown-level: every point inside a crown contributes its codes.
  Trees and lianas are compared as two separate questions.

Outputs (written to --output_path, timestamped):
  metrics_overall_<ts>.txt        overall counts / match rates (also printed)
  metrics_site_<source>_<ts>.txt  same metrics, one file per crown source
  disagreements_trees_<ts>.csv    crowns where the tree code does not match
  disagreements_lianas_<ts>.csv   crowns where liana codes disagree (partial/none)
  confusion_trees_matrix_<ts>.csv crown-code x dominant-point-code cross-tab
  confusion_trees_pairs_<ts>.csv  off-diagonal confusion pairs, sorted by count
  comparison_<ts>.gpkg            layers: crowns (polygons), points (intersecting)
"""

import argparse
import os
import re
from datetime import datetime

import geopandas as gpd
import pandas as pd

# ---- column layout of the inputs ----
TREE_SLOTS = ["tree_A", "tree_B", "tree_C"]                  # point tree annotations
OTHER_SLOTS = ["other_a", "other_b", "other_c", "other_d"]   # point liana/other annotations
LIANA_COLS = [f"liana_specie_{i}" for i in range(1, 6)]      # trails-crown liana species


def extract_code(value):
    """Return the 6-letter species code (middle token) or None.

    Handles trails-crown (`Name_CODE6_CODE4`) and point (`Name-CODE6-CODE4`)
    formats. Genus/family-only entries (no middle token) return None.
    """
    if not isinstance(value, str):
        return None
    parts = re.split(r"[-_]", value.strip())
    if len(parts) >= 2 and re.fullmatch(r"[A-Z0-9]{6}", parts[1]):
        return parts[1]
    return None


def name_part(value):
    """Scientific-name portion (first token) of a code-tagged label."""
    if not isinstance(value, str):
        return None
    return re.split(r"[-_]", value.strip())[0].strip() or None


def codes_from_cols(row, label_cols):
    """Ordered list of valid codes pulled from the given label columns of a row."""
    out = []
    for col in label_cols:
        c = extract_code(row.get(col))
        if c:
            out.append(c)
    return out


def dominant_code(code_cov_pairs):
    """Pick a single representative point code from pooled (code, cov) pairs.

    Ranked by occurrence count, then by total mask coverage. Used only to build
    a crown-level confusion matrix (a crown may contain >1 point code).
    """
    if not code_cov_pairs:
        return None
    agg = {}
    for code, cov in code_cov_pairs:
        cnt, scov = agg.get(code, (0, 0.0))
        agg[code] = (cnt + 1, scov + (cov if pd.notna(cov) else 0.0))
    return sorted(agg.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)[0][0]


def load_crown_file(path):
    """Read one crown GPKG and normalize it to the common comparison schema.

    Returns a GeoDataFrame with: crown_source, global_id, area_m2,
    crown_tree_code, crown_tree_name, crown_liana_codes (list), geometry.
    """
    g = gpd.read_file(path)
    src = os.path.splitext(os.path.basename(path))[0]
    out = gpd.GeoDataFrame({"geometry": g.geometry}, geometry="geometry", crs=g.crs)
    out["crown_source"] = src
    out["global_id"] = g.get("global_id")
    out["area_m2"] = g.get("area_m2")

    # readable per-crown id: trails_<tag> (trails file) or <plot>_<tag> (plot maps)
    def _tag(t):
        return "NA" if pd.isna(t) else str(int(float(t)))
    tags = g["tag"].map(_tag) if "tag" in g.columns else pd.Series(["NA"] * len(g))
    if "plot" in g.columns:
        prefixes = g["plot"].astype("string").fillna(src)
        out["crown_tag_id"] = [f"{p}_{t}" for p, t in zip(prefixes, tags)]
    else:
        out["crown_tag_id"] = ["trails_" + t for t in tags]

    if "mnemonic" in g.columns:            # crownmap format: bare lowercase code
        code = g["mnemonic"].astype("string").str.strip().str.upper()
        valid = code.str.fullmatch(r"[A-Z0-9]{6}").fillna(False)
        out["crown_tree_code"] = [c if v else None for c, v in zip(code, valid)]
        out["crown_tree_name"] = list(g.get("latin"))
        out["crown_liana_codes"] = [[] for _ in range(len(g))]   # no liana species here
    elif "tree_specie" in g.columns:       # trails format: encoded string + lianas
        out["crown_tree_code"] = g["tree_specie"].map(extract_code)
        out["crown_tree_name"] = g["tree_specie"].map(name_part)
        liana_cols = [c for c in LIANA_COLS if c in g.columns]
        out["crown_liana_codes"] = g[liana_cols].apply(
            lambda r: sorted({c for c in (extract_code(v) for v in r) if c}), axis=1
        )
    else:
        raise ValueError(f"Unrecognized crown schema in {path}: need 'mnemonic' or 'tree_specie'")

    print(f"  {src}: {len(out)} crowns, {out['crown_tree_code'].notna().sum()} with a tree code")
    return out


def compare(crowns_paths, points_path, output_path, buffer_m=0.0):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(output_path, exist_ok=True)

    # --- load + normalize + concat all crown files (unify to first CRS) ---
    print("Loading crown files:")
    parts = [load_crown_file(p) for p in crowns_paths]
    target_crs = parts[0].crs
    parts = [g if g.crs == target_crs else g.to_crs(target_crs) for g in parts]
    crowns = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=target_crs)
    crowns["crown_idx"] = crowns.index

    points = gpd.read_file(points_path).to_crs(target_crs).copy()
    print(f"Crowns: {len(crowns)} ({crowns.crs})  |  Points: {len(points)} ({points_path})")

    # --- point-side codes (per point, pooled across slots) ---
    points["pt_tree_codes"] = points.apply(
        lambda r: codes_from_cols(r, [f"{s}_lb_label" for s in TREE_SLOTS]), axis=1
    )
    points["pt_tree_codecov"] = points.apply(
        lambda r: [
            (extract_code(r.get(f"{s}_lb_label")), r.get(f"{s}_cov"))
            for s in TREE_SLOTS
            if extract_code(r.get(f"{s}_lb_label"))
        ],
        axis=1,
    )
    points["pt_other_codes"] = points.apply(
        lambda r: codes_from_cols(r, [f"{s}_lb_label" for s in OTHER_SLOTS]), axis=1
    )
    # (slot_letter, code) so we can tell which annotation (A=main, B, C) held a match
    points["pt_tree_slotcodes"] = points.apply(
        lambda r: [
            (s.split("_")[1], extract_code(r.get(f"{s}_lb_label")))
            for s in TREE_SLOTS
            if extract_code(r.get(f"{s}_lb_label"))
        ],
        axis=1,
    )
    # code -> scientific name map (for readable disagreement lists)
    code2name = {}
    for s in TREE_SLOTS + OTHER_SLOTS:
        for code, nm in zip(points[f"{s}_lb_label"].map(extract_code),
                            points[f"{s}_lb_label"].map(name_part)):
            if code and code not in code2name:
                code2name[code] = nm

    # --- spatial join: points within crowns (optionally buffered) ---
    join_geom = crowns.copy()
    if buffer_m > 0:
        join_geom["geometry"] = join_geom.geometry.buffer(buffer_m)
    sj = gpd.sjoin(
        points, join_geom[["crown_idx", "geometry"]],
        predicate="within", how="inner",
    )
    print(f"Point-in-crown pairs: {len(sj)}  |  unique points: {sj.index.nunique()}  |  crowns hit: {sj['crown_idx'].nunique()}")

    # --- aggregate point codes up to the crown ---
    agg = sj.groupby("crown_idx").agg(
        n_points=("crown_idx", "size"),
        point_ids=("point_id", lambda s: sorted(map(str, s))),
        missions=("mission_id", lambda s: sorted(set(map(str, s)))),
        pt_tree_codes=("pt_tree_codes", lambda s: sorted({c for lst in s for c in lst})),
        pt_tree_codecov=("pt_tree_codecov", lambda s: [cc for lst in s for cc in lst]),
        pt_tree_slotcodes=("pt_tree_slotcodes", lambda s: [sc for lst in s for sc in lst]),
        pt_other_codes=("pt_other_codes", lambda s: sorted({c for lst in s for c in lst})),
    )

    comp = crowns.merge(agg, on="crown_idx", how="inner")  # only crowns with >=1 point

    # --- tree comparison (crown's single code vs pooled point tree codes) ---
    def tree_status(row):
        if not row["crown_tree_code"]:
            return "crown_no_code"
        if not row["pt_tree_codes"]:
            return "no_point_code"
        return "match" if row["crown_tree_code"] in row["pt_tree_codes"] else "mismatch"

    comp["tree_status"] = comp.apply(tree_status, axis=1)
    comp["dominant_pt_tree_code"] = comp["pt_tree_codecov"].map(dominant_code)

    # for matches, the best (A<B<C) waypoint slot whose code equals the crown code
    def best_match_slot(row):
        if row["tree_status"] != "match":
            return None
        slots = sorted(letter for letter, code in row["pt_tree_slotcodes"]
                       if code == row["crown_tree_code"])
        return slots[0] if slots else None

    comp["match_slot"] = comp.apply(best_match_slot, axis=1)

    # --- liana comparison (set overlap, both sides multi-valued) ---
    def liana_status(row):
        crown_set = set(row["crown_liana_codes"])
        pt_set = set(row["pt_other_codes"])
        if not crown_set:
            return "crown_no_liana"
        if not pt_set:
            return "no_point_other_code"
        inter = crown_set & pt_set
        if not inter:
            return "mismatch"
        return "match" if crown_set <= pt_set else "partial"

    comp["liana_inter"] = comp.apply(
        lambda r: sorted(set(r["crown_liana_codes"]) & set(r["pt_other_codes"])), axis=1
    )
    comp["liana_status"] = comp.apply(liana_status, axis=1)

    # string versions of list columns for CSV/GPKG export
    for col in ["crown_liana_codes", "pt_tree_codes", "pt_other_codes",
                "liana_inter", "point_ids", "missions"]:
        comp[col + "_str"] = comp[col].map(lambda v: ";".join(map(str, v)))

    # ---------------- outputs ----------------
    centroids = comp.geometry.centroid.to_crs(4326)
    comp["centroid_lon"] = centroids.x
    comp["centroid_lat"] = centroids.y

    # 1) metrics (.txt): overall + one per crown source
    point_codes = {c for lst in points["pt_tree_codes"] for c in lst}

    def pct(n, d):
        return f"({100 * n / d:.2f}%)" if d else "(n/a)"

    def metrics_text(scope, sub, n_pairs, n_unique, crown_files=None, points_total=None):
        ts_c = sub["tree_status"].value_counts()
        ls_c = sub["liana_status"].value_counts()
        comparable = int(ts_c.get("match", 0) + ts_c.get("mismatch", 0))
        matches = int(ts_c.get("match", 0))
        slot_c = sub.loc[sub["tree_status"] == "match", "match_slot"].value_counts()
        crown_codes = set(sub["crown_tree_code"].dropna())
        L = ["=" * 72,
             f"CROWN vs WAYPOINT SPECIES COMPARISON - {scope}",
             f"Run: {ts}   Buffer: {buffer_m} m",
             "=" * 72, "", "CROWNS"]
        if crown_files is not None:
            L.append(f"  crown files                  : {crown_files}")
        if points_total is not None:
            L.append(f"  points total in dataset      : {points_total}")
        L += [f"  crowns with >=1 point        : {len(sub)}",
              f"  max points in a crown        : {int(sub['n_points'].max()) if len(sub) else 0}",
              "", "POINTS INSIDE THESE CROWNS",
              f"  point-in-crown pairs         : {n_pairs}",
              f"  unique points inside         : {n_unique}",
              "", "TREES  (crown species vs point tree_A/B/C)",
              f"  comparable crowns            : {comparable}",
              f"  matches                      : {matches}  {pct(matches, comparable)}",
              f"  mismatches                   : {int(ts_c.get('mismatch', 0))}",
              f"  crown has code, point none   : {int(ts_c.get('no_point_code', 0))}",
              f"  crown has no code            : {int(ts_c.get('crown_no_code', 0))}",
              "", "  Matched at which waypoint slot (was it the MAIN Labelbox label?):"]
        for letter, lbl in [("A", "tree_A (main)"), ("B", "tree_B"), ("C", "tree_C")]:
            n = int(slot_c.get(letter, 0))
            L.append(f"    {lbl:<24}: {n}  {pct(n, matches)}")
        L += ["", "LIANAS  (crown liana_specie_* vs point other_a..d)",
              f"  full match                   : {int(ls_c.get('match', 0))}",
              f"  partial                      : {int(ls_c.get('partial', 0))}",
              f"  mismatch                     : {int(ls_c.get('mismatch', 0))}",
              f"  crown has no liana code      : {int(ls_c.get('crown_no_liana', 0))}",
              f"  point has no other code      : {int(ls_c.get('no_point_other_code', 0))}",
              "", "TREE CODE VOCABULARY",
              f"  distinct crown codes         : {len(crown_codes)}",
              f"  distinct point codes (all)   : {len(point_codes)}",
              f"  shared with point codes      : {len(crown_codes & point_codes)}", ""]
        return "\n".join(L)

    overall = metrics_text("ALL SITES", comp, len(sj), int(sj.index.nunique()),
                           crown_files=len(crowns_paths), points_total=len(points))
    with open(os.path.join(output_path, f"metrics_overall_{ts}.txt"), "w", encoding="utf-8") as f:
        f.write(overall)
    print("\n" + overall)

    # per-site point counts (a point may lie in crowns from >1 source)
    sj_src = (sj.reset_index().rename(columns={"index": "pt_idx"})
                .merge(crowns[["crown_idx", "crown_source"]], on="crown_idx"))
    pairs_by = sj_src.groupby("crown_source").size().to_dict()
    uniq_by = sj_src.groupby("crown_source")["pt_idx"].nunique().to_dict()
    for src, sub in comp.groupby("crown_source"):
        txt = metrics_text(src, sub, pairs_by.get(src, 0), uniq_by.get(src, 0))
        with open(os.path.join(output_path, f"metrics_site_{src}_{ts}.txt"), "w", encoding="utf-8") as f:
            f.write(txt)

    # 2) tree disagreements
    base_cols = ["crown_source", "crown_tag_id", "global_id", "crown_tree_code", "crown_tree_name",
                 "pt_tree_codes_str", "n_points", "point_ids_str", "missions_str",
                 "area_m2", "centroid_lon", "centroid_lat"]
    tree_dis = comp[comp["tree_status"] == "mismatch"].copy()
    tree_dis["pt_tree_names"] = tree_dis["pt_tree_codes"].map(
        lambda lst: ";".join(code2name.get(c, "?") for c in lst)
    )
    tree_dis[base_cols + ["pt_tree_names"]].to_csv(
        os.path.join(output_path, f"disagreements_trees_{ts}.csv"), index=False
    )

    # 3) liana disagreements (partial + mismatch)
    liana_dis = comp[comp["liana_status"].isin(["partial", "mismatch"])].copy()
    liana_dis[[
        "crown_source", "crown_tag_id", "global_id", "liana_status", "crown_liana_codes_str",
        "pt_other_codes_str", "liana_inter_str", "n_points", "point_ids_str",
        "missions_str", "centroid_lon", "centroid_lat",
    ]].to_csv(os.path.join(output_path, f"disagreements_lianas_{ts}.csv"), index=False)

    # 4) tree confusion (comparable crowns only: crown code vs dominant point code)
    conf = comp[comp["tree_status"].isin(["match", "mismatch"])]
    if len(conf):
        matrix = pd.crosstab(conf["crown_tree_code"], conf["dominant_pt_tree_code"])
        matrix.to_csv(os.path.join(output_path, f"confusion_trees_matrix_{ts}.csv"))
        pairs = (
            conf[conf["tree_status"] == "mismatch"]
            .groupby(["crown_tree_code", "dominant_pt_tree_code"])
            .size().reset_index(name="n").sort_values("n", ascending=False)
        )
        pairs["crown_name"] = pairs["crown_tree_code"].map(lambda c: code2name.get(c) or c)
        pairs["point_name"] = pairs["dominant_pt_tree_code"].map(lambda c: code2name.get(c, "?"))
        pairs.to_csv(os.path.join(output_path, f"confusion_trees_pairs_{ts}.csv"), index=False)

    # 5) joined GeoPackage (crowns polygons + intersecting points)
    gpkg = os.path.join(output_path, f"comparison_{ts}.gpkg")
    keep = ["crown_source", "crown_tag_id", "global_id", "crown_tree_code", "crown_tree_name",
            "crown_liana_codes_str", "n_points", "pt_tree_codes_str", "pt_other_codes_str",
            "tree_status", "match_slot", "liana_status", "liana_inter_str",
            "dominant_pt_tree_code", "area_m2", "geometry"]
    comp_out = comp[keep].rename(columns=lambda c: c.replace("_str", ""))
    comp_out.to_file(gpkg, layer="crowns", driver="GPKG")

    pts_out = sj.merge(
        comp[["crown_idx", "crown_source", "crown_tag_id", "global_id", "crown_tree_code",
              "tree_status", "liana_status"]],
        on="crown_idx", how="left",
    )
    pts_keep = ["point_id", "mission_id", "crown_source", "crown_tag_id", "global_id",
                "crown_tree_code", "tree_status", "liana_status", "geometry"]
    pts_out[pts_keep].to_file(gpkg, layer="points", driver="GPKG")

    print(f"\nWrote outputs to: {output_path}")
    print(f"  GeoPackage: {gpkg} (layers: crowns, points)")
    return comp


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare crown vs waypoint species by intersection")
    parser.add_argument("--crowns_path", required=True, nargs="+",
                        help="One or more crown GPKGs (trails or crownmap schema, auto-detected)")
    parser.add_argument("--points_path", required=True, help="Joined waypoint points GPKG")
    parser.add_argument("--output_path", required=True, help="Output directory")
    parser.add_argument("--buffer_m", type=float, default=0.0,
                        help="Optional crown buffer in meters before the within-join (default 0 = strict)")
    args = parser.parse_args()

    compare(args.crowns_path, args.points_path, args.output_path, args.buffer_m)