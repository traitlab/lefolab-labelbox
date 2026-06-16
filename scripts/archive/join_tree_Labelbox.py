import geopandas as gpd
import json
import numpy as np
import os
import requests
import time
import argparse
import pandas as pd
from pygbif import species
from dotenv import load_dotenv
import json, time, math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from geodataset.utils.utils import mask_to_polygon
from tqdm import tqdm
import labelbox as lb

from datetime import datetime
from io import BytesIO
from PIL import Image
import re
import pandas as pd

def winners_to_long(winners_df):
    #"mask_coverage", "mask_polygon"
    prefixes = ["plant_name", "plant_value", "mask_url", "mask_path", "mask_coverage"]

    # find available annotation indices k from plant_value_k
    ks = sorted({
        int(m.group(1))
        for c in winners_df.columns
        for m in [re.match(r"^plant_value_(\d+)$", c)]
        if m
    })
    if not ks:
        return winners_df.copy()

    # base columns (keep everything that is not a per-annotation column)
    per_ann = set()
    for p in prefixes:
        for k in ks:
            col = f"{p}_{k}"
            if col in winners_df.columns:
                per_ann.add(col)
    base_cols = [c for c in winners_df.columns if c not in per_ann]

    parts = []
    for k in ks:
        tmp = winners_df[base_cols].copy()
        tmp["annotation_index"] = k
        tmp["plant_name"]    = winners_df.get(f"plant_name_{k}")
        tmp["plant_value"]   = winners_df.get(f"plant_value_{k}")
        tmp["mask_url"]      = winners_df.get(f"mask_url_{k}")
        # opted to remove coverage and mask_polygon
        tmp["mask_coverage"] = winners_df.get(f"mask_coverage_{k}")
        #tmp["mask_polygon"]  = winners_df.get(f"mask_polygon_{k}")
        tmp["mask_path"]     = winners_df.get(f"mask_path_{k}")
        parts.append(tmp)

    long_df = pd.concat(parts, ignore_index=True)

    # keep only real annotation rows (plant_value present)
    
    long_df = long_df[
        long_df["plant_value"].notna()
        & long_df["plant_value"].astype(str).str.strip().ne("")
        & ~long_df["plant_value"].astype(str).str.strip().isin(["Null", "None"])
    ].copy()
    
    return long_df


## GBIF RELATED CODE
# ---- Optional: persistent cache on disk ----
CACHE_PATH = "F:/LEFO/AGOL/LabelboxID/new_version/gbif_taxonomy_cache.json"
mask_url = 'http://example.com/mask_image.png'
headers = {'User-Agent': 'Mozilla/5.0'}

load_dotenv()

#function for getting the gbif_taxonomy
def get_gbif_taxonomy(gbif_id):
    try:
        info = species.name_usage(key=gbif_id)
        return {
            "scientificName": info.get("scientificName"),
            "canonicalName": info.get("canonicalName"),
            "rank": info.get("rank"),
            "genus": info.get("genus"),
            "family": info.get("family")
        }
    except Exception as e:
        print(f"Error for GBIF ID {gbif_id}: {e}")
        return {}
    
def load_cache(path=CACHE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # normalize keys to strings
        return {str(k): v for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[WARN] Could not load cache: {e}")
        return {}

def save_cache(cache, path=CACHE_PATH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save cache: {e}")

# ---- GBIF lookup with memoization + retries ----
def get_gbif_taxonomy_cached(gbif_id, cache, retries=3, backoff=1.5):
    """
    gbif_id can be int/str. Returns a dict with the 5 fields (or {} on failure).
    """
    # normalize key
    if gbif_id is None or (isinstance(gbif_id, float) and math.isnan(gbif_id)):
        return {}
    key = str(int(gbif_id)) if str(gbif_id).isdigit() else str(gbif_id)

    if key in cache:                 # <-- reuse previous result
        return cache[key] or {}

    for attempt in range(retries):
        try:
            info = species.name_usage(key=key)  # your existing GBIF call
            out = {
                "scientificName": info.get("scientificName"),
                "canonicalName":  info.get("canonicalName"),
                "rank":           info.get("rank"),
                "genus":          info.get("genus"),
                "family":         info.get("family"),
            }
            cache[key] = out         # <-- store in cache
            return out
        except Exception as e:
            if attempt == retries - 1:
                print(f"[GBIF] Failed for {key}: {e}")
                cache[key] = {}      # cache the failure to avoid hammering GBIF
                return {}
            time.sleep(backoff ** attempt)

# ---- Main updater: add/refresh columns on your GeoDataFrame ----
def enrich_with_gbif_taxonomy(gdf, gbif_col, persist=True):
    """
    Adds/updates columns: scientificName, canonicalName, rank, genus, family.
    Keeps geometry intact.
    """
    gbif_cache = load_cache() if persist else {}

    needed_cols = ["scientificName", "canonicalName", "rank", "genus", "family"]
    # ensure columns exist (so assign works even if some rows fail)
    for c in needed_cols:
        if c not in gdf.columns:
            gdf[c] = pd.NA

    # Do lookups only for rows missing any of the fields (saves time if partially filled)
    mask_need = gdf[needed_cols].isna().any(axis=1)
    if mask_need.any():
        # map → dicts → expand to DataFrame
        looked = gdf.loc[mask_need, gbif_col].map(lambda x: get_gbif_taxonomy_cached(x, gbif_cache))
        expanded = looked.apply(pd.Series)

        # assign back only to those rows/columns
        gdf.loc[mask_need, needed_cols] = expanded.reindex(columns=needed_cols).values

    if persist:
        save_cache(gbif_cache)

    return gdf

## Labelbox related code 

def retrieve_mask_array(mask_url, api_key):
    """Downloads the mask and returns it as a NumPy array."""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    max_attempts = 3
    attempt = 0
    while attempt < max_attempts:
        try:
            response = requests.get(mask_url, headers=headers)
            
            if response.status_code == 401:
                print("Authentication failed - check your API key")
                print(f"Link: {mask_url}")
                return None
            elif response.status_code == 403:
                print("Access forbidden - check permissions")
                print(f"Link: {mask_url}")
                return None
            elif response.status_code != 200:
                print(f"HTTP error: {response.status_code} (attempt {attempt+1} of {max_attempts})")
                print(f"Link: {mask_url}")
                attempt += 1
                if attempt < max_attempts:
                    time.sleep(2)
                    continue
                else:
                    return None
            
            # Check content type
            content_type = response.headers.get('content-type', '').lower()
            if 'image' not in content_type:
                print(f"Expected image but got: {content_type}")
                print(f"Response content: {response.text[:200]}")
                print(f"Link: {mask_url}")
                return None, None
            
            # Process the image
            mask_image = Image.open(BytesIO(response.content))
            mask_array = np.array(mask_image.convert('L'))

            '''
            total_pixels = mask_array.size
            masked_pixels = np.sum(mask_array > 128)
            coverage_percentage = (masked_pixels / total_pixels) * 100
            '''
            return mask_array
            #return coverage_percentage, mask_array.shape
        except Exception as e:
            print(f"Error: {e} (attempt {attempt+1} of {max_attempts})")
            attempt += 1
            if attempt < max_attempts:
                time.sleep(2)
                continue
            else:
                return None
    response = requests.get(mask_url, headers=headers)
    mask_image = Image.open(BytesIO(response.content))
    mask_array = np.array(mask_image.convert('L'))
    
    # Return the array for use in other functions
    return mask_array
    

# # Usage
# api_key = "YOUR_LABELBOX_API_KEY_HERE"
# mask_url = "https://api.labelbox.com/api/v1/projects/cmbgnzmhu0bed07027vmxezzd/annotations/cmbjf8gzr000x356uwmuba0jp/index/1/mask"

# coverage, dimensions = calculate_mask_coverage(mask_url, api_key)

# if coverage is not None:
#     print(f"Mask covers {coverage:.2f}% of the image")
#     print(f"Image dimensions: {dimensions}")
# else:
#     print("Failed to process mask - check authentication")

def join_gpkg_with_json(gpkg_path, json_path, output_path=None, labelbox_api_key=None, csv_filter_path=None, images_path=None, masks_path=None,wcvp_file=None, debug=False):
    """
    Join GPKG file with JSON data based on zoom_url field matching data_row.row_data
    
    Parameters:
    gpkg_path (str): Path to the GPKG file
    json_path (str): Path to the JSON file  
    output_path (str): Optional output path for the joined GPKG. Default to current directory if None.
    labelbox_api_key (str): Optional Labelbox API key for mask processing
    csv_filter_path (str): Optional path to CSV file for filtering by gbif_taxonID
    debug (bool): If True, save intermediate CSV files with timestamps for debugging
    """
    
    # Start timing
    start_time = datetime.now()
    print(f"Processing started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Read the GPKG file
    gdf = gpd.read_file(gpkg_path)
    print(gdf.shape, flush=True)
    
    # Load CSV filter if provided
    valid_taxon_ids = set()
    if csv_filter_path:
        try:
            csv_df = pd.read_csv(csv_filter_path)
            valid_taxon_ids = set(csv_df['gbif_taxonID'].astype(str))
            print(f"Loaded {len(valid_taxon_ids)} valid taxon IDs from {csv_filter_path}")
        except Exception as e:
            print(f"Warning: Could not load CSV filter file: {e}")
            print("Proceeding without filtering")
    
    # Read and parse the JSON file
    json_data = []
    with open(json_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data = json.loads(line.strip())
                json_data.append(data)
            except json.JSONDecodeError:
                continue

    #json_data = json_data[5156:6000]
    # Create a DataFrame from JSON data with relevant fields
    json_records = []
    processed_urls = set()  # Track URLs that have already been processed
    print(f"Length of json_data",len(json_data), flush=True)

    for record in json_data:
        # Extract key information from the JSON structure
        data_row = record.get('data_row', {})
        row_data_url = data_row.get('row_data', '')
        
        # Get project information
        projects = record.get('projects', {})
        project_info = {}
        labels_info = []

        
        for project_id, project_data in projects.items():
            project_info = {
                'project_id': project_id,
                'project_name': project_data.get('name', ''),
                'workflow_status': project_data.get('project_details', {}).get('workflow_status', ''),
                'task_name': project_data.get('project_details', {}).get('task_name', ''),
                #'batch_name': project_data.get('project_details', {}).get('batch_name', ''),
                #'priority': project_data.get('project_details', {}).get('priority', None),
                'consensus_expected_label_count': project_data.get('project_details', {}).get('consensus_expected_label_count', None)
            }
            
            # Extract labels information
            
            labels = project_data.get('labels', [])
            for label in labels:
                # Get annotation objects (plant classifications)
                annotations = label.get('annotations', {}).get('objects', [])
                
                label_info = {
                    'label_id': label.get('id', ''),
                    'created_by': label.get('label_details', {}).get('created_by', ''),
                    'created_at': label.get('label_details', {}).get('created_at', ''),
                    'skipped': label.get('performance_details', {}).get('skipped', None),
                    'consensus_score': label.get('performance_details', {}).get('consensus_score', None),
                    'annotation_count': len(annotations)
                }
                # Extract plant classifications and mask information
                annotation_index = 0
                
                for annotation in annotations:
                    annotation_index += 1
                    
                    # Extract plant classifications for this annotation
                    classifications = annotation.get('classifications', [])
                    plant_name = ''
                    plant_value = ''
                    
                    for classification in classifications:
                        checklist_answers = classification.get('checklist_answers', [])
                        for answer in checklist_answers:
                            plant_name = answer.get('name', 'Null')
                            plant_value = answer.get('value', 'Null')
                            break
                        if plant_name and plant_value:
                            break
                    
                    # Apply CSV filter at annotation level if provided
                    if valid_taxon_ids:
                        # Only include this annotation if plant_value matches valid taxon IDs
                        if str(plant_value) not in valid_taxon_ids:
                            continue  # Skip this annotation
                    
                    # Store plant information in separate columns
                    label_info[f'plant_name_{annotation_index}'] = plant_name
                    label_info[f'plant_value_{annotation_index}'] = plant_value
                    
                    # Extract mask URL and calculate coverage for this annotation
                    mask_info = annotation.get('mask', {})
                    if mask_info and 'url' in mask_info:
                        single_mask_url = mask_info['url']
                        label_info[f'mask_url_{annotation_index}'] = single_mask_url
                        
                        
                        # Calculate mask coverage if API key is available
                        if labelbox_api_key:
                            safe_data_row_id = str(data_row.get('id', 'no_data_row_id'))
                            safe_label_id = str(label.get('id', 'no_label_id'))
                            mask_filename = f"mask_{safe_data_row_id}_{safe_label_id}_{annotation_index}.png"
                            full_mask_path = os.path.join(masks_path, mask_filename)

                            # If we already have this mask saved, skip the network call
                            if os.path.exists(full_mask_path):
                                mask_rel_path = os.path.join("masks", mask_filename)
                                label_info[f"mask_path_{annotation_index}"] = mask_rel_path

                                # optional: if you also want polygon but didn’t store it, you can load and polygonize
                                # (still costs CPU, but avoids Labelbox)
                                
                                try:
                                    mask_data = np.array(Image.open(full_mask_path).convert("L"))

                                    segmentation = mask_to_polygon(mask_data, simplify_tolerance=1.0)
                                    label_info[f"mask_polygon_{annotation_index}"] = segmentation
                                    total_pixels = mask_data.size
                                    masked_pixels = np.sum(mask_data > 128)
                                    label_info[f"mask_coverage_{annotation_index}"] =  str(round((masked_pixels/total_pixels)*100, 2))
                                    
                                except Exception:
                                    label_info[f"mask_polygon_{annotation_index}"] = None
                                    label_info[f"mask_coverage_{annotation_index}"] = None

                            else:
                                full_mask_path = os.path.join(masks_path, mask_rel_path)

                                # only now do Labelbox download
                                try:
                                    mask_data = retrieve_mask_array(single_mask_url, labelbox_api_key)
                                    if mask_data is not None:

                                        Image.fromarray(mask_data).save(full_mask_path)
                                        label_info[f"mask_path_{annotation_index}"] = mask_rel_path

                                        segmentation = mask_to_polygon(mask_data, simplify_tolerance=1.0)
                                        label_info[f"mask_polygon_{annotation_index}"] = segmentation
                                        total_pixels = mask_data.size
                                        masked_pixels = np.sum(mask_data > 128)
                                        label_info[f"mask_coverage_{annotation_index}"] =  str(round((masked_pixels/total_pixels)*100, 2))

                                    else:
                                        label_info[f'mask_polygon_{annotation_index}'] = None
                                        label_info[f'mask_path_{annotation_index}'] = None
        
                                except Exception:
                                    label_info[f"mask_polygon_{annotation_index}"] = None
                                    label_info[f"mask_coverage_{annotation_index}"] = None
                                
                                    '''
                                    # 1. Retrieve the Mask Array
                                    mask_filename = f"mask_{safe_data_row_id}_{safe_label_id}_{annotation_index}.png"
                                    mask_path = os.path.join("masks", "original_masks", mask_filename)
                                    full_mask_path = os.path.join(images_path, mask_path)
                                    if os.path.exists(full_mask_path):
                                        label_info[f'mask_path_{annotation_index}'] = mask_path
                                        # load mask, polygonize if needed
                                        mask_data = np.array(Image.open(full_mask_path).convert("L"))

                                    else:
                                        mask_data = retrieve_mask_array(single_mask_url, labelbox_api_key)

                                    if mask_data is not None:
                                        # 2. Calculate Coverage (optional, but shows separation)
                                        total_pixels = mask_data.size
                                        masked_pixels = np.sum(mask_data > 128) # Reuse the threshold logic
                                        coverage = (masked_pixels / total_pixels) * 100
                                        
                                        # 3. Polygon Conversion
                                        segmentation = mask_to_polygon(mask_data, simplify_tolerance=1.0)
                                    if coverage is not None:
                                        label_info[f'mask_coverage_{annotation_index}'] = str(round(coverage, 2))
                                    else:
                                        label_info[f'mask_coverage_{annotation_index}'] = None

                                    if segmentation is not None:
                                        print("has segmentation")
                                        label_info[f'mask_polygon_{annotation_index}'] = segmentation
                                        #label_info[f'mask_array_{annotation_index}'] = mask_data

                                        safe_data_row_id = str(data_row.get('id', 'no_data_row_id'))
                                        safe_label_id = str(label.get('id', 'no_label_id'))
                                        mask_filename = f"mask_{safe_data_row_id}_{safe_label_id}_{annotation_index}.png"
                                        mask_path = os.path.join("masks", "original_masks", mask_filename)
                                        full_mask_path = os.path.join(images_path, mask_path)
                                        os.makedirs(os.path.dirname(full_mask_path), exist_ok=True)
                                        Image.fromarray(mask_data).save(full_mask_path)

                                        label_info[f'mask_path_{annotation_index}'] = mask_path


                                    else:
                                        label_info[f'mask_polygon_{annotation_index}'] = None
                                        label_info[f'mask_path_{annotation_index}'] = None
                                    '''
                        else:
                            print("labelbox isn't working", flush=True)
                            label_info[f'mask_path_{annotation_index}'] = None
                            label_info[f'mask_coverage_{annotation_index}'] = None
                    else:
                        label_info[f'mask_url_{annotation_index}'] = ''
                        label_info[f'mask_coverage_{annotation_index}'] = None
                        
                labels_info.append(label_info)
        
        if labels_info:
            selected_label_id = project_data.get('project_details', {}).get('selected_label_id', None)

            if selected_label_id:
                selected_label = None
                for label_info in labels_info:
                    if label_info.get('label_id') == selected_label_id:
                        selected_label = label_info
                        break
                
                if selected_label:
                    labels_to_process = [selected_label]
                else:
                    labels_to_process = labels_info
            else:
                labels_to_process = labels_info
            
            labels_count = len(labels_to_process)
            for label_info in labels_to_process:
                # Skip if the label was skipped
                if label_info.get('skipped') is True:
                    continue
                
                # Skip if zoom_url is already present in json_records
                # TO CHANGE IN THE FUTURE WHEN WINNING LABELS ARE AVAILABLE
                '''
                if row_data_url in processed_urls:
                    continue
                '''
                    
                json_record = {
                    'zoom_url': row_data_url,
                    'data_row_id': data_row.get('id', ''),
                    'global_key': data_row.get('global_key', ''),
                    'dataset_id': data_row.get('details', {}).get('dataset_id', ''),
                    'dataset_name': data_row.get('details', {}).get('dataset_name', ''),
                    **project_info,
                    **label_info
                }
                json_records.append(json_record)
                processed_urls.add(row_data_url)  # Add URL to processed set
                labels_count -= 1 
        else:
            # Skip if zoom_url is already present in json_records
            if row_data_url not in processed_urls:
                json_record = {
                    'zoom_url': row_data_url,
                    'data_row_id': data_row.get('id', ''),
                    'global_key': data_row.get('global_key', ''),
                    'dataset_id': data_row.get('details', {}).get('dataset_id', ''),
                    'dataset_name': data_row.get('details', {}).get('dataset_name', ''),
                    **project_info
                }
                json_records.append(json_record)
                processed_urls.add(row_data_url)  # Add URL to processed set

      
    # Convert to DataFrame (this is still necessary)
    json_df = pd.DataFrame(json_records)
    

    # this handles the images that are labelled and have no annotations done. 
    # we decide to omit them
    json_df = json_df[json_df['global_key'].notna()]

    output_dir = output_path if output_path else "."
    os.makedirs(output_dir, exist_ok=True)

    # 1. Prepare Data and Mark Validity
    # Convert created_at to datetime objects for comparison
    json_df['created_at_dt'] = pd.to_datetime(json_df['created_at'])

    # image-level presence in export (no validity filtering)
    json_img = (
        json_df.sort_values("created_at_dt", na_position="last")  # optional
            .drop_duplicates("zoom_url", keep="last")
            [["zoom_url", "data_row_id", "global_key", "dataset_id", "dataset_name"]]
            .copy()
    )

    json_img = json_img.rename(columns={
        "data_row_id": "lb_data_row_id_raw",
        "global_key": "base_image_raw",
    })

    # Identify columns that contain species values
    plant_value_cols = [col for col in json_df.columns if col.startswith('plant_value_')]

    # A label is 'valid' if it wasn't skipped AND contains at least one species value
    has_valid_species = json_df[plant_value_cols].notna().any(axis=1)
    json_df['is_valid'] = (json_df['skipped'] != True) & (has_valid_species)

    # 2. Select the Latest Valid Label
    valid_labels_df = json_df[json_df['is_valid'] == True].copy()

    if not valid_labels_df.empty:
        # **KEY CHANGE:** Find the *latest* created_at (maximum time) for each unique 'zoom_url'
        winner_idx = valid_labels_df.groupby("zoom_url")["created_at_dt"].idxmax()
        winners = valid_labels_df.loc[winner_idx].copy()

        annotations_long = winners_to_long(winners)
        annotations_long = annotations_long.rename(columns={
            "plant_value": "gbif_id",
            "plant_name": "lb_label",
            #": "lb_mask_polygon",
            "final_plant_name":"lb_label",
            "label_id":"lb_label_id", 
            "mask_coverage": "annotation_mask_coverage",
            "mask_path": "lb_mask_path",          # <-- THIS is the key
            "data_row_id": "lb_data_row_id",      # if you want consistency with your gpkg
            "global_key": "base_image",
        })

        # Count filter annotation count per image (only if CSV filter was applied, otherwise this would be redundant with annotation_count)
        if csv_filter_path is not None:
            annotations_per_image = annotations_long.groupby("zoom_url").size().reset_index(name="filter_annotation_count")
            annotations_long = annotations_long.merge(annotations_per_image, on="zoom_url", how="left")

        # enrich taxonomy for each annotation row
        annotations_long = enrich_with_gbif_taxonomy(annotations_long, gbif_col="gbif_id", persist=True)

        annotations_long['gbif_id'] = (
            annotations_long['gbif_id']
                .replace(r'^\s*$', np.nan, regex=True)
                .astype(float)          # needed to allow NaN
                .fillna(-1)
                .astype(np.int64)
        )
        json_df_processed = annotations_long
        json_df_processed['labeled'] = json_df_processed['lb_label'].notna()
        # join to gpkg (this will create multiple rows per zoom_url if multiple annotations)
        #joined_gdf = gdf.merge(annotations_long, on="zoom_url", how="left")
    
    else:
        # If no valid labels were found in the whole dataset
        print("[WARN] No valid labels found after filtering.")
        json_df_processed = json_df.drop_duplicates(subset=['zoom_url', 'data_row_id', 'label_id']).copy()
        
        json_df_processed['labeled'] = False
        json_df_processed['final_plant_value'] = ''
        json_df_processed['final_plant_name'] = ''
        #json_df_processed['final_mask_coverage'] = None
        #json_df_processed['lb_mask_polygon'] = None
        
    # Remove all temporary label/plant/mask columns
    # Define prefixes for columns to drop (the intermediate/temporary ones)
    drop_prefixes = ['plant_', 'mask_', 'skipped', 'consensus_']

    # Drop columns starting with 'label_' BUT EXCLUDE 'label_id'
    cols_to_drop = [
        col for col in json_df_processed.columns 
        if any(col.startswith(p) for p in drop_prefixes) or 
        (col.startswith('label_') and col != 'label_id')
    ]
    cols_to_drop = list(set(cols_to_drop)) # Unique list
    json_df_processed = json_df_processed.drop(columns=cols_to_drop, errors='ignore')

    json_df_processed_rename = json_df_processed
   
    #bad = json_df_processed_rename[json_df_processed_rename["base_image"].isna()]
    #print("num NaN photo_name rows:", len(bad))

    # Debug output (keep this block)
    if debug:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_basename = os.path.splitext(os.path.basename(json_path))[0]
        csv_processed_filename = os.path.join(output_dir, f"{json_basename}_processed_{timestamp}.csv")
        json_df_processed_rename.to_csv(csv_processed_filename, index=False)
   
    joined_gdf = gdf.merge(json_img, on="zoom_url", how="left")

    # robust base_image: prefer global_key if present, else derive from zoom_url
    joined_gdf["base_image"] = joined_gdf["base_image_raw"]
    joined_gdf["base_image"] = joined_gdf["base_image"].fillna(
        joined_gdf["zoom_url"].astype(str).str.split("/").str[-1]
    )
    
    joined_gdf = joined_gdf.merge(json_df_processed_rename, on="zoom_url", how="left")

    # Prefer raw global_key, then winner/global_key, then filename from zoom_url
    joined_gdf["base_image"] = (
        joined_gdf.get("base_image_raw")
        .fillna(joined_gdf.get("base_image_y"))   # from processed winners (if present)
        .fillna(joined_gdf.get("base_image_x"))   # if exists from some earlier step
        .fillna(joined_gdf["zoom_url"].astype(str).str.split("/").str[-1])
    )

    # (optional) drop the messy duplicates to avoid confusion
    joined_gdf = joined_gdf.drop(
        columns=[c for c in ["base_image_raw", "base_image_x", "base_image_y"] if c in joined_gdf.columns],
        errors="ignore"
    )

    # adding in the WCVP labels
    if wcvp_file is not None:    
        df = pd.read_csv(wcvp_file)
        df = df.rename(columns={"gbif_taxonID":"gbif_id"})
        df = df[['wcvp_accepted_name', 'gbif_id', 'habit', 'wcvp_url']]
        joined_gdf = joined_gdf.merge(df, on='gbif_id', how='left')

    valid_name = (
        joined_gdf["scientificName"].notna()
        & joined_gdf["scientificName"].astype(str).str.strip().ne("")
    )

    # final_mask_polygon must not be empty / missing
    valid_mask = (
        (joined_gdf["lb_mask_path"].notna())  
    )
 
    joined_gdf["wide_image"] = joined_gdf["wide_url"].str.split("/").str[-1]
    joined_gdf = joined_gdf.drop(['is_valid', 'annotation_count', 'created_at_dt', 'created_at', 'created_by', 'created_at_dt'], axis=1)


    # getting annotations with a mask and a scientificName
    filtered_gdf = joined_gdf[valid_name & valid_mask]

    # first_rows = (
    #     joined_gdf
    #     .dropna(subset=["base_image"])          # remove rows where base_image is NaN
    #     .drop_duplicates(subset=["base_image"], keep="first")
    # )

    # print("Starting sequential image download/verification...")
    # for _, image_row in tqdm(first_rows.iterrows(), total=len(first_rows), desc="Verifying/Downloading Images"):

    #     # zoom url
    #     image_name = image_row["base_image"]
    #     zoom_image_path = os.path.join(images_path, image_name)
    #     zoom_url = image_row['zoom_url']

    #     if not os.path.exists(zoom_image_path):
    #         # This is your robust download logic, now safe from race conditions
    #         response = requests.get(zoom_url, stream=True)
    #         if response.status_code == 200:
    #             with open(zoom_image_path, "wb") as file:
    #                 file.write(response.content)
    #         else:
    #             print(f"FAILED to download {image_name}. Skipping related annotations.")
            
    #     # wide url
    #     wide_url = image_row['wide_url']
    #     wide_image_name = image_row['wide_image']
    #     wide_image_path = os.path.join(images_path, wide_image_name)
        
    #     if not os.path.exists(wide_image_path): 
    #         response = requests.get(wide_url, stream=True)
    #         if response.status_code == 200:
    #             with open(wide_image_path, "wb") as file:
    #                 file.write(response.content)
    
    # Display some statistics
    print(f"Original GPKG records: {len(gdf)}")
    print(f"JSON records: {len(json_df)}")
    print(f"Joined records: {len(joined_gdf)}")
    print(f"Records with valid names and masks records: {len(filtered_gdf)}")
    
    print(f"Records with matches: {len(joined_gdf.dropna(subset=['lb_data_row_id']))}")
    print(f"Records with valid species (labeled=True): {len(joined_gdf[joined_gdf['labeled'] == True])}")
    
    # Calculate and display elapsed time
    end_time = datetime.now()
    elapsed_time = end_time - start_time
    print(f"Processing completed at: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total time taken: {elapsed_time}")
    
    gpkg_basename = os.path.splitext(os.path.basename(gpkg_path))[0]
    gpkg_filename = os.path.join(output_dir, f"{gpkg_basename}_joined_{timestamp}.gpkg")
    gpkg_filename_filtered = os.path.join(output_dir, f"{gpkg_basename}_joined_{timestamp}_filtered.gpkg")

    joined_gdf.to_file(gpkg_filename, driver='GPKG')
    filtered_gdf.to_file(gpkg_filename_filtered, driver='GPKG')
    print(f"Joined data saved to: {gpkg_filename}")
    print(f"Joined data with only valid identification and masks saved to: {gpkg_filename_filtered}")
    
    return joined_gdf

# Usage example
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create Geodataframe for Annotations")
    parser.add_argument('--gpkg_path', type=str, required=True, help='Path to gpkg file')
    parser.add_argument('--json_path', type=str, required=True, help='Path to annotation (.json)')
    parser.add_argument('--wcvp_file', type=str, required=False, help='Path to the wcvp file that connects gbif to wcvp (.csv)')
    parser.add_argument('--output_path', type=str, required=True, help='Path to output directory, images/ and masks/ subfolders will be created automatically')
    parser.add_argument('--csv_filter_path', type=str, help='Path to CSV file for filtering by gbif_taxonID (optional)')

    args = parser.parse_args()

    images_path = os.path.join(args.output_path, "images")
    masks_path = os.path.join(args.output_path, "masks")
    os.makedirs(images_path, exist_ok=True)
    os.makedirs(masks_path, exist_ok=True)

    # Add your Labelbox API key here to enable mask processing
    labelbox_api_key=os.getenv('LABEL_BOX_API_KEY')

    debug = True
    
    # Perform the join
    result = join_gpkg_with_json(args.gpkg_path, args.json_path, args.output_path, labelbox_api_key, args.csv_filter_path, images_path, masks_path, args.wcvp_file, debug)