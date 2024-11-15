import os
import csv
from widget.lib.widget_base import WidgetBase
import sqlite3
import json
import time
from typing import List, Dict, Union, Tuple, Optional, Any
from contextlib import contextmanager
import hashlib

@contextmanager
def db_connection(db_path):
  conn = sqlite3.connect(db_path)
  try:
    yield conn
  finally:
    conn.close()

class GND:
  def __init__(self, db: str, query_range: str, scale_factor: float, window: int, query: Optional[str], uniref_id: str, id_type: Any, log_file: str):
    self.db = db
    self.output = {
      "message": "",
      "error": False,
      "eod": False,
      "totaltime": time.time()
    }
    self.scale_factor = scale_factor
    self.query_range = query_range
    self.window = window
    self.query = query
    self.uniref_id = uniref_id
    self.id_type = id_type

    self.query_cache = {}
    self.log_file = log_file
    self._ensure_log_file()

    self.set_uniref_table_names()

  def set_uniref_table_names(self):
    # if this is the get_stats call, then id_type is not passed, it is 50 or 90
    # the program should not use these variables for non-uniref jobs at all
    if self.id_type == "":
      self.UNIREF_CLUSTER_INDEX = "uniref50_cluster_index" if self.check_table_exists("uniref50_cluster_index") else "uniref90_cluster_index" if self.check_table_exists("uniref90_cluster_index") else "cluster_index"
      self.UNIREF_RANGE = "uniref50_range" if self.check_table_exists("uniref50_cluster_index") else "uniref90_range" if self.check_table_exists("uniref90_cluster_index") else ""
      self.UNIREF_INDEX = "uniref50_index" if self.check_table_exists("uniref50_cluster_index") else "uniref90_index" if self.check_table_exists("uniref90_cluster_index") else ""
    else:
      self.UNIREF_CLUSTER_INDEX = "uniref50_cluster_index" if self.id_type == "50" else "uniref90_cluster_index" if self.id_type == "90" else "cluster_index"
      self.UNIREF_RANGE = "uniref50_range" if self.id_type == "50" else "uniref90_range" if self.id_type == "90" else ""
      self.UNIREF_INDEX = "uniref50_index" if self.id_type == "50" else "uniref90_index" if self.id_type == "90" else ""

  def error_output(self, message: str) -> None:
    self.output["message"] = message
    self.output["error"] = True
    self.output["eod"] = True

  def _ensure_log_file(self):
    with open(self.log_file, 'w', newline='') as f:
      csv.writer(f).writerow(['Timestamp', 'Query', 'Params', 'Time', 'Rows Returned', 'Rows Scanned', 'Scan Ratio', 'Index Used'])

  def log_query(self, query, params, exec_time, rows_returned, rows_scanned, index_used):
    scan_ratio = rows_scanned / rows_returned if rows_returned > 0 else float('inf')
    with open(self.log_file, 'a', newline='') as f:
      csv.writer(f).writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"),
        query,
        str(params),
        f"{exec_time:.4f}",
        rows_returned,
        rows_scanned,
        f"{scan_ratio:.2f}",
        index_used or 'None'
      ])

  def _extract_info_from_plan(self, plan):
    index_used = None
    rows_scanned = 0
    for row in plan:
      if 'USING INDEX' in str(row):
        index_used = str(row).split('USING INDEX')[-1].split()[0]
      elif 'SCAN TABLE' in str(row):
        index_used = "No index used (table scan)"
      if 'SCAN' in str(row):
        parts = str(row).split()
        for i, part in enumerate(parts):
          if part == '~':
            rows_scanned = int(parts[i+1])
            break
    return index_used, rows_scanned

  def fetch_data(self, query: str, params: Optional[Tuple] = None) -> List[Tuple]:
    start_time = time.time()
    cache_key = hashlib.md5((query + str(params)).encode()).hexdigest()
    
    if cache_key in self.query_cache:
      return self.query_cache[cache_key]
    
    with db_connection(self.db) as conn:
      cursor = conn.cursor()
      
      # Get query plan
      if params:
        cursor.execute("EXPLAIN QUERY PLAN " + query, params)
      else:
        cursor.execute("EXPLAIN QUERY PLAN " + query)
      try:
        plan = cursor.fetchall()
        index_used, rows_scanned = self._extract_info_from_plan(plan)
      except sqlite3.Error:
        index_used, rows_scanned = "Unable to determine", 0
      
      # Execute actual query
      if params:
        cursor.execute(query, params)
      else:
        cursor.execute(query)
      result = cursor.fetchall()

      execution_time = time.time() - start_time
      self.query_cache[cache_key] = result
      self.log_query(query, params, execution_time, len(result), rows_scanned, index_used)
      return result

  def check_table_exists(self, table_name: str) -> bool: 
    query = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
    result = self.fetch_data(query, (table_name,), )
    return len(result) > 0
  
  def check_column_exists(self, column, table):
    query = f"PRAGMA table_info({table})"
    columns = [row[1] for row in self.fetch_data(query)]
    return column in columns

  # here, everything is either a direct (ncluding uniref) job or a gnn job
  def is_direct_job(self) -> bool:
    return (
      (self.check_table_exists("metadata") and self.fetch_data("SELECT type FROM metadata")[0][0] != "gnn")
      or self.uniref_id != ""
      or self.id_type == "uniprot"
    )
  
  def is_gnn_job(self) -> bool:
    return (self.check_table_exists("metadata") and self.fetch_data("SELECT type FROM metadata")[0][0] == "gnn")
  
  def get_cluster_num_from_query(self) -> int:
    index_range = self.query_range.split("-")
    query = f"SELECT cluster_num FROM {self.UNIREF_CLUSTER_INDEX} WHERE start_index <= ? AND end_index >= ?"
    result = self.fetch_data(query, (index_range[0], index_range[1]))
    return result[0][0] if result else None
  
  def get_stats(self) -> None:
    stats = {}

    if self.uniref_id == "":
      start_index = self.fetch_data(f"SELECT start_index FROM {self.UNIREF_CLUSTER_INDEX} WHERE cluster_num = ? LIMIT 1", (self.query, ))[0][0]
      end_index = self.fetch_data(f"SELECT end_index FROM {self.UNIREF_CLUSTER_INDEX} WHERE cluster_num = ? LIMIT 1", (self.query, ))[0][0]
    else:
      start_index = self.fetch_data(f"SELECT start_index FROM {self.UNIREF_RANGE} WHERE uniref_id = ? LIMIT 1", (self.uniref_id, ))[0][0]
      end_index = self.fetch_data(f"SELECT end_index FROM {self.UNIREF_RANGE} WHERE uniref_id = ? LIMIT 1", (self.uniref_id, ))[0][0]

    max_index = end_index - start_index
    # total diagram number, so max_index + 1, since it's zero-indexed
    num_checked = max_index + 1
    # assumes it starts at 0 and ends at max_index
    index_range = [[start_index, end_index]]
    # TODO: min and max bp are actually calculated in a different, much more complicated way, do if there is time
    # get the minimum value of the rel_start column in neighbors
    min_bp = self.fetch_data("SELECT MIN(rel_start) FROM neighbors LIMIT 1")[0][0]
    # get the maximum value of the rel_stop column in neighbors
    max_bp = self.fetch_data("SELECT MAX(rel_stop) FROM neighbors LIMIT 1")[0][0]
    # total width for the legend
    legend_scale = max_bp - min_bp
    # get the maximum difference between rel_stop and rel_start in attributes
    query_width = self.fetch_data("SELECT MAX(abs(rel_stop - rel_start)) AS max_diff FROM attributes")[0][0]
    # max absolute value of min_bp and max_bp times 2 plus query_width
    # max_side = max(abs(max_bp), abs(min_bp))
    actual_max_width = abs(max_bp) if abs(max_bp) > abs(min_bp) else abs(min_bp) * 2 + query_width
    # scale factor starts as 7.5 by default, zoom in and zoom out should multiply or divide by 4, respectively
    scale_factor = self.scale_factor
    # time data is populated with the numbers, but didn't include times because my process of fetching and querying is different
    time_data = "#Ids: " + str(num_checked) + ", #Queries: " + str(num_checked * 2) + ", QueryTime: 0, #Fetch: " + str(self.fetch_data("SELECT COUNT(*) FROM attributes")[0][0] + self.fetch_data("SELECT COUNT(*) FROM neighbors")[0][0]) + ", FetchTime: 0, Total: 0 PROC=0 PARSE=0"

    # This code tells the GND whether or not to display the plus buttons and additional info uniref jobs have
    if self.check_table_exists("uniref50_index"):
      has_uniref = 50
    elif self.check_table_exists("uniref90_index"):
      has_uniref = 90
    else:
      has_uniref = False

    stats = {
      "max_index": max_index, 
      "scale_factor": scale_factor, 
      "legend_scale": legend_scale, 
      "min_bp": min_bp, 
      "max_bp": max_bp, 
      "query_width": query_width, 
      "actual_max_width": actual_max_width, 
      "time_data": time_data, 
      "num_checked": num_checked, 
      "index_range": index_range, 
      "has_uniref": has_uniref
    }

    self.output["stats"] = stats
  
  def get_family_values(self, family_str: str, ipro_family_str: str, family_desc_str: str, ipro_family_desc_str: str) -> Dict[str, List[str]]:
    if family_str == "":
      family = ["none-query"]
    elif family_str == "none":
      family = ["none"]
    else:
      family = family_str.split("-")

    family_desc = family_desc_str.split(";") if family_desc_str else [""]
    if len(family_desc) == 1:
      family_desc = family_desc_str.split("-") if family_desc_str else [""]
    if family == ["none-query"]:
      family_desc = ["Query without family"]

    if ipro_family_str == "":
      ipro_family = ["none-query"]
    elif ipro_family_str == "none":
      ipro_family = ["none"]
    else:
      ipro_family = ipro_family_str.split("-")

    ipro_family_desc = ipro_family_desc_str.split(";") if ipro_family_desc_str else [""]
    if len(ipro_family_desc) == 1:
      ipro_family_desc = ipro_family_desc_str.split("-") if ipro_family_desc_str else [""]
    if ipro_family == ["none-query"]:
      ipro_family_desc = ["Query without family"]

    return {
      "family": family,
      "ipro_family": ipro_family,
      "family_desc": family_desc,
      "ipro_family_desc": ipro_family_desc
    }
  
  def get_attributes(self, idx: int) -> Dict[str, Union[str, int, List[str], float, bool]]:
    attributes = {}
    # get values from the required row based on id and store it in a result array
    query = """
    SELECT accession, id, num, family, ipro_family, start, stop, rel_start, rel_stop, 
          strain, direction, type, seq_len, organism, taxon_id, anno_status, desc, 
          evalue, family_desc, ipro_family_desc, color, sort_order, is_bound, cluster_num 
    FROM attributes 
    WHERE cluster_index = ?
    """
    result = self.fetch_data(query, (idx,))[0]
    family_values = self.get_family_values(result[3], result[4], result[18], result[19])
    attributes = {
      "accession": result[0],
      "id": result[1],
      "num": result[2],
      "family": family_values["family"],
      "ipro_family": family_values["ipro_family"],
      "start": result[5],
      "stop": result[6],
      "rel_start_coord": result[7],
      "rel_stop_coord": result[8],
      "strain": result[9],
      "direction": result[10],
      "type": result[11],
      "seq_len": result[12],
      "organism": result[13].rstrip('.') if result[13] else "",
      "taxon_id": result[14],
      "anno_status": result[15],
      "desc": result[16],
      "family_desc": family_values["family_desc"],
      "ipro_family_desc": family_values["ipro_family_desc"],
      "pfam": family_values["family"],
      "interpro": family_values["ipro_family"],
      "pfam_desc": family_values["family_desc"],
      "interpro_desc": family_values["ipro_family_desc"],
      "color": result[20].split(",") if result[20] else [""],
      "sort_order": result[21],
      "is_bound": result[22],
      "pid": -1,
      "rel_start": 0,
      "rel_width": 0,
    }
    if result[17] != None:
      attributes["evalue"] = result[17]
    if result[23] != None and self.is_gnn_job():
      attributes["cluster_num"] = result[23]
    if self.check_column_exists("uniref90_size", "attributes"):
      attributes["uniref90_size"] = self.fetch_data(f"SELECT uniref90_size FROM attributes WHERE cluster_index = ?", (idx, ))[0][0]
    if self.check_column_exists("uniref50_size", "attributes"):
      attributes["uniref50_size"] = self.fetch_data(f"SELECT uniref50_size FROM attributes WHERE cluster_index = ?", (idx, ))[0][0]
    return attributes
  
  def get_neighbors(self, n: int, idx: int) -> List[Dict[str, Union[str, int, List[str], float]]]:
    neighbors = []
    query = "SELECT accession, id, num, family, ipro_family, start, stop, rel_start, rel_stop, direction, type, seq_len, anno_status, desc, family_desc, ipro_family_desc, color FROM neighbors WHERE gene_key = ? AND num BETWEEN ? AND ? ORDER BY num"
    
    rows = self.fetch_data(query, (idx + 1, n - self.window, n + self.window))
    for row in rows:
      neighbor = {}
      family_values = self.get_family_values(row[3], row[4], row[14], row[15])

      neighbor = {
      "accession": row[0],
      "id": row[1],
      "num": row[2],
      "family": family_values["family"],
      "ipro_family": family_values["ipro_family"],
      "start": row[5],
      "stop": row[6],
      "rel_start_coord": row[7],
      "rel_stop_coord": row[8],
      "direction": row[9],
      "type": row[10],
      "seq_len": row[11],
      "anno_status": row[12],
      "desc": row[13],
      "family_desc": family_values["family_desc"],
      "ipro_family_desc": family_values["ipro_family_desc"],
      "pfam": family_values["family"],
      "interpro": family_values["ipro_family"],
      "pfam_desc": family_values["family_desc"],
      "interpro_desc": family_values["ipro_family_desc"],
      "color": row[16].split(",") if row[16] else [""],
      "rel_start": 0,
      "rel_width": 0
      }
      neighbors.append(neighbor)
    return neighbors

  def is_cluster_child(self, attr: Dict[str, Any]) -> bool:
    if "uniref90_size" in attr and "uniref50_size" not in attr:
      return attr["uniref90_size"] == 0
    if "uniref50_size" in attr and "uniref90_size" not in attr:
      return attr["uniref50_size"] == 0
    if "uniref50_size" in attr and "uniref90_size" in attr:
      return attr["uniref50_size"] == 0 and attr["uniref90_size"] == 0
    return False
  
  def lowest_nesting_level(self) -> bool:
    return self.id_type == "uniprot" or (self.id_type == "90" and self.uniref_id != "")
  
  def retrieve_and_process(self) -> None:
    self.output["data"] = []
    start_index = int(self.query_range.split("-")[0])
    end_index = int(self.query_range.split("-")[1])
    indices = list(range(start_index, end_index + 1))
    # if it is not a direct job, we have to translate from uniref_index to cluster_index using the uniref_range table
    if not self.is_direct_job():
      indices = [index[0] for index in self.fetch_data(f"SELECT cluster_index FROM {self.UNIREF_RANGE} WHERE uniref_index BETWEEN ? AND ?", (start_index, end_index))]
    for idx in indices:
      # if it's a uniref_id, we have to translate from member_index to cluster_index using the uniref_index table
      if self.uniref_id != "" and self.id_type != "uniprot":
        idx = self.fetch_data(f"SELECT cluster_index FROM {self.UNIREF_INDEX} WHERE member_index = ?", (idx, ))[0][0]
      
      elem = {}
      elem["attributes"] = self.get_attributes(idx)
      elem["neighbors"] = self.get_neighbors(elem["attributes"]['num'], idx)
      # if it is a cluster child (uniref_sizes of 0) and we are not at the lowest nesting level, dont display this diagram
      if self.is_cluster_child(elem["attributes"]) and not self.lowest_nesting_level(): continue
      self.output["data"].append(elem)

    if not self.lowest_nesting_level():
      if self.id_type == "90" or (self.id_type == "50" and self.uniref_id != ""):
        self.output["data"].sort(key=lambda x: x["attributes"].get("uniref90_size", 0), reverse=True)
      else:
        self.output["data"].sort(key=lambda x: x["attributes"].get("uniref50_size", 0), reverse=True)
    
  def compute_rel_coords(self) -> None:
    max_width = 300000 / self.scale_factor
    max_query_width = 0
    max_side = max_width / 2
    legend_scale = max_width
    min_bp = -max_side
    max_bp = max_side + max_query_width
    min_pct = 2;
    max_pct = -2;

    for elem in self.output["data"]:
      start = elem["attributes"]["rel_start_coord"]
      stop = elem["attributes"]["rel_stop_coord"]
      ac_start = 0.5
      ac_width = (stop - start) / max_width
      offset = 0.5 - (start - min_bp) / max_width
      elem["attributes"]["rel_start"] = ac_start
      elem["attributes"]["rel_width"] = ac_width
      acEnd = ac_start + ac_width
      if (acEnd > max_pct):
        max_pct = acEnd
      if (ac_start < min_pct):
        min_pct = ac_start

      for neighbor in elem["neighbors"]:
        nb_start_bp = neighbor["rel_start_coord"]
        nb_width_bp = neighbor["rel_stop_coord"] - nb_start_bp
        nb_start = (nb_start_bp - min_bp) / max_width
        nb_width = nb_width_bp / max_width
        nb_start += offset
        nb_end = nb_start + nb_width
        neighbor["rel_start"] = nb_start
        neighbor["rel_width"] = nb_width
        if (nb_end > max_pct):
          max_pct = nb_end
        if (nb_start < min_pct):
          min_pct = nb_start
    
    self.output["legend_scale"] = legend_scale
    self.output["min_pct"] = min_pct
    self.output["max_pct"] = max_pct
    self.output["max_bp"] = max_bp
    self.output["min_bp"] = min_bp
    self.output["scale_factor"] = self.scale_factor
      
  def get_arrow_data(self) -> None:
    queries = int(self.query_range.split("-")[1]) - int(self.query_range.split("-")[0]) + 1
    self.output.update({
      "scale_factor": self.scale_factor,
      "time": f"#Q={queries} TQ=0 #N={queries} TN=0 PROC=0 PARSE=0 Total=0",
      "counts": {
        "max": queries,
        "invalid": [],
        "displayed": 0
      },
    })
    self.retrieve_and_process()
    self.compute_rel_coords()

  def generate_json(self) -> bytes:
    try:
      if self.query_range == "":
        self.get_stats()
      else:
        self.get_arrow_data()
    except Exception as e:
      self.error_output(str(e))
    self.output["totaltime"] = time.time() - self.output["totaltime"]
    json_data = json.dumps(self.output).encode('utf-8')
    return json_data

class Widget(WidgetBase):
  def context(self) -> Dict[str, str]:
    return {
      "message":
      """
        Welcome to the base data endpoint. Use params to to specify the data you want to see.
        <br>
        <br>
        Examples of possible URLs with params:
        <ul>
          <li><a href="?direct-id=30093&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=10&query=1&stats=1">Initial call for 30093</a></li>
          <li><a href="?direct-id=30093&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=10&scale-factor=7.5&range=140-159&id-type=uniprot">Sample range call for 30093</a></li>
          <li><a href="?direct-id=30095&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=10&query=1&stats=1">Initial call for 30095</a></li>
          <li><a href="?direct-id=30095&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=20&scale-factor=7.5&range=0-17&id-type=uniprot">Sample range call for 30095</a></li>
          <li><a href="?gnn-id=7671&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=20&query=2&stats=1">Initial call for 7671 cluster job, query = 2</a></li>
          <li><a href="?gnn-id=7671&key=52eb593c2fed778dcfd6a2cf16d1f5ced3f3f617&window=20&scale-factor=7.5&range=0-19&id-type=90">Sample range call for 7671 cluster job, query = 2</a></li>
        </ul>
      """
    }
    
  def render(self) -> Union[str, bytes]:
    id_query = "gnn-id" if self.has_param("gnn-id") else "direct-id" if self.has_param("direct-id") else "upload-id"
    uniref_id = self.get_param("uniref-id") if self.has_param("uniref-id") else ""
    id_type = self.get_param("id-type") if self.has_param("id-type") else ""
    if self.has_param('query'):
        my_gnd = GND(db=self.get_param(id_query) + ".sqlite", query_range="", scale_factor=7.5, window=int(self.get_param('window')), query=self.get_param('query'), uniref_id=uniref_id, id_type=id_type, log_file="query_metrics.csv")
        json_data = my_gnd.generate_json()
        return json_data
    elif self.has_param('range'):
        my_gnd = GND(db=self.get_param(id_query) + ".sqlite", query_range=self.get_param('range'), scale_factor=float(self.get_param('scale-factor')), window=int(self.get_param('window')), query=None, uniref_id=uniref_id, id_type=id_type, log_file="query_metrics.csv")
        json_data = my_gnd.generate_json()
        return json_data
    return super().render()

