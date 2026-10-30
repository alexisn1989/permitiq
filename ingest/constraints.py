"""Site constraints: city GIS layers that limit what a site can do, measured
per parcel (screening estimates, never a determination).

  polygon_share  acres and share of the parcel inside the layer's polygons
                 (optionally: distance to the nearest polygon within near_ft)
  line_crossing  length of the layer's lines inside the parcel, and the
                 distance to the nearest line within near_ft, with that
                 line's allowlisted attributes (e.g. a VDOT count's ADT)
  point_near     distance to the nearest point within near_ft, with its
                 attributes

Sources: the city's GIS, USFWS NWI, NPS National Register, VDOT (ArcGIS
services, read complete by OBJECTID) and USDA Soil Data Access (ingest/sda.py).

The Chesapeake Bay RPA layer is a line layer: profiled on the real data
(2026-10-28) its 2,060 untyped lines are the RPA boundary, not closed buffer
areas (1 closed ring; polygonizing yields 72 fragments a median ~49 ft wide).
So no RPA acres are ever claimed -- only "the RPA boundary crosses / lies
within N ft of this parcel".

Distances and areas use a local projection in feet around Virginia Beach
(the same equirectangular approach as ingest/parcels.py; < 0.5% error at
the city's scale), not survey-grade geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely import STRtree, union_all
from shapely.geometry import mapping, shape

from ingest.flood import polygonal

FT_PER_DEG = 364_567.2  # feet per degree of latitude
COS0 = math.cos(math.radians(36.8))  # Virginia Beach
SQFT_PER_ACRE = 43_560
NEAR_FT = 100  # default: a line within this distance of a parcel is reported as near
# Thresholds (from the user or a source; never invented): shallow water table under 46 cm (18 in, the
# user's figure); hydric map unit when at least half its components are hydric (hydclprs >= 50).
WATER_TABLE_CM = 46
HYDRIC_MIN_PCT = 50
# Virginia Beach, generously: area filter for national layers (parcels decide what is measured)
VB_ENVELOPE = {"geometry": '{"xmin":-76.23,"ymin":36.54,"xmax":-75.86,"ymax":36.95,"spatialReference":{"wkid":4326}}',
               "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}

ENV = "https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/Environmental/MapServer"
PLAN = "https://geo.vbgov.com/mapservices/rest/services/Planning_and_Development/Planning_and_Development/MapServer"
UTIL = "https://geo.vbgov.com/mapservices/rest/services/Public_Utilities/Other_Utility_Information/MapServer"
NWI = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer"
CHES = "https://gis.cityofchesapeake.net/mapping/rest/services"
CHES_ENVELOPE = {"geometry": '{"xmin":-76.50,"ymin":36.54,"xmax":-76.05,"ymax":36.87,"spatialReference":{"wkid":4326}}',
                 "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
NRHP = "https://mapservices.nps.gov/arcgis/rest/services/cultural_resources/nrhp_locations/MapServer"
VDOT = "https://services.arcgis.com/p5v98VHDX9Atv3l7/arcgis/rest/services"


@dataclass(frozen=True)
class Layer:
    key: str
    url: str
    kind: str  # polygon_share | line_crossing | point_near
    label: str
    meaning: str
    attrs: tuple[str, ...] = field(default_factory=tuple)  # source fields kept; nothing else is stored
    group_by: str | None = None  # an attribute to break the overlap down by (e.g. soil type)
    classification: bool = False  # covers the whole city (soils): report what is mapped, not "inside the layer"
    near_ft: float | None = None  # distance searched for the nearest feature (lines default to NEAR_FT)
    where: str = "1=1"
    bbox: bool = False  # filter the source to the Virginia Beach envelope
    source: str = "arcgis"  # arcgis | sda
    fmt: str | None = None  # how the nearest feature is described, from its allowlisted attributes
    locality: str = "virginia_beach"  # the city whose parcels this layer is measured on


LAYERS = {l.key: l for l in (
    Layer("rpa", f"{ENV}/1", "line_crossing", "Chesapeake Bay RPA boundary",
          "Chesapeake Bay Preservation Act Resource Protection Area boundary (the city's line). Land in the RPA "
          "(within 100 ft of tidal shores, tidal wetlands and perennial streams) cannot be cleared, graded or "
          "built on without an exception."),
    Layer("southern_watersheds_buffer", f"{ENV}/2", "polygon_share", "Southern Watersheds buffer area",
          "Buffer area of the Southern Watersheds Management Ordinance (development limits apply)."),
    Layer("special_floodplain", f"{ENV}/3", "polygon_share", "Floodplains subject to special restrictions",
          "City floodplain areas with development restrictions beyond FEMA's.", ("FLD_ZONE", "FLOODWAY")),
    Layer("shrink_swell_soils", f"{ENV}/4", "polygon_share", "Shrink-swell soils",
          "Soils that expand and shrink with moisture: foundation design and earthwork cost.",
          ("SOIL_NAME", "SOIL_DESCR", "SOIL_TYPE"), "SOIL_NAME"),
    Layer("historic_overlay", f"{PLAN}/24", "polygon_share", "Historic and Cultural Overlay District",
          "Work needs Historical Review Board review in addition to the standard process.", ("Name",), "Name"),
    Layer("historic_kempsville", f"{PLAN}/25", "polygon_share", "Historic Kempsville Area Overlay District",
          "Design standards and review apply in the overlay.", ("Name",), "Name"),
    # Utility project-file index (Public Utilities): areas of past or planned city projects, dates mostly not
    # recorded. A signal that utility work happened or is planned here -- never pipe capacity.
    Layer("city_sewer_projects", f"{UTIL}/3", "polygon_share", "City sewer project area",
          "Area of a city sewer project on file (project-file index; not pipe locations or capacity).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("city_water_projects", f"{UTIL}/6", "polygon_share", "City water project area",
          "Area of a city water project on file (project-file index; not pipe locations or capacity).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("water_sewer_projects", f"{UTIL}/9", "polygon_share", "City water and sewer project area",
          "Area of a combined city water and sewer project on file (project-file index).",
          ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME"),
    Layer("sewer_pump_station_projects", f"{UTIL}/18", "polygon_share", "Sewer pump station project",
          "Site of a city sewer pump station project on file. Distance is to the project site, not a service "
          "area; capacity is not published.", ("PU_NUM", "PROJECT_NAME"), "PROJECT_NAME", near_ft=1000),
    Layer("dam_break", f"{PLAN}/23", "polygon_share", "Dam break inundation zone",
          "Mapped inundation area if a dam fails (state dam-safety rules can limit development).",
          ("NAME", "HAZARD_LEVEL"), "NAME"),
    # USDA SSURGO, survey area VA810 (replaces the city's older soil map: 2 of its 45 units are no longer in
    # the current survey). Screening at ~1:12,000 -- not a substitute for borings.
    Layer("usda_soils", "VA810", "polygon_share", "Soils (USDA soil survey)",
          f"USDA soil map units. Shallow water table: minimum annual depth under {WATER_TABLE_CM} cm (18 in). "
          f"Hydric: map unit at least {HYDRIC_MIN_PCT}% hydric components. Mapped at ~1:12,000; not a "
          "substitute for borings.", ("mukey", "musym", "muname", "hydclprs", "wtdepannmin", "hydgrpdcd",
                                      "drclassdcd"), "muname", classification=True, source="sda"),
    Layer("nwi_wetlands", f"{NWI}/0", "polygon_share", "Mapped wetlands (USFWS National Wetlands Inventory)",
          "Wetlands mapped from imagery by USFWS. Not a jurisdictional delineation: a wetland delineation "
          "decides what is regulated. Open estuarine / marine deepwater (the bay and ocean, one 2.75-million-acre "
          "polygon the service cannot return) is excluded.", ("ATTRIBUTE", "WETLAND_TYPE"), "WETLAND_TYPE", bbox=True,
          where="WETLAND_TYPE <> 'Estuarine and Marine Deepwater'"),
    Layer("nrhp_polygons", f"{NRHP}/1", "polygon_share", "National Register of Historic Places property",
          "Listed property boundary (NPS). Many boundaries are approximate (circumscribed); federally funded or "
          "permitted work triggers Section 106 review.", ("RESNAME", "NRIS_Refnum", "ResType", "BND_TYPE"),
          "RESNAME", where="County = 'Virginia Beach'"),
    Layer("nrhp_points", f"{NRHP}/0", "point_near", "National Register of Historic Places site",
          "Listed site location (NPS point, not a boundary).", ("RESNAME", "NRIS_Refnum", "ResType"),
          near_ft=500, where="County = 'Virginia Beach'", fmt="{RESNAME} ({ResType})"),
    Layer("vdot_adt", f"{VDOT}/VDOT_Bidirectional_Traffic_Volume_2025/FeatureServer/0", "line_crossing",
          "VDOT traffic count (ADT)", "Annual average daily traffic on the nearest VDOT-counted segment within "
          "300 ft (counted routes only; local streets often have no count). Quality code as published by VDOT.",
          ("ROUTE_COMMON_NAME", "ADT", "ADT_QUALITY", "DATA_DATE", "START_LABEL", "END_LABEL"), near_ft=300,
          where="FROM_JURISDICTION LIKE '%Virginia Beach%' OR TO_JURISDICTION LIKE '%Virginia Beach%'",
          fmt="{ROUTE_COMMON_NAME}: ADT {ADT} ({DATA_DATE}, quality code {ADT_QUALITY})"),
    Layer("vdot_syip_lines", f"{VDOT}/SYIP_Approved_Projects/FeatureServer/1", "line_crossing",
          "VDOT Six-Year Improvement Program project", "Approved SYIP project within 1,000 ft (the state's "
          "funded road program).", ("UPC", "DESCRIPTION", "SCOPE_OF_WORK_DESC", "CN_START_DATE",
                                    "TOTAL_ALLOCATIONS_CURRENT"), near_ft=1000,
          where="JURISDICTION_DESC LIKE '%Virginia Beach%'",
          fmt="UPC {UPC} {DESCRIPTION} ({SCOPE_OF_WORK_DESC}; construction start {CN_START_DATE})"),
    Layer("vdot_syip_points", f"{VDOT}/SYIP_Approved_Projects/FeatureServer/0", "point_near",
          "VDOT Six-Year Improvement Program project (point)", "Approved SYIP project located at a point, within "
          "1,000 ft.", ("UPC", "DESCRIPTION", "SCOPE_OF_WORK_DESC", "CN_START_DATE", "TOTAL_ALLOCATIONS_CURRENT"),
          near_ft=1000, where="JURISDICTION_DESC LIKE '%Virginia Beach%'",
          fmt="UPC {UPC} {DESCRIPTION} ({SCOPE_OF_WORK_DESC}; construction start {CN_START_DATE})"),
)}

# Norfolk: the city's parcel-layer extent (2026-10-29) with a small margin
NF_ENVELOPE = {"geometry": '{"xmin":-76.35,"ymin":36.81,"xmax":-76.16,"ymax":36.98,"spatialReference":{"wkid":4326}}',
               "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
# Portsmouth: the VGIN parcels' extent for FIPS 51740 (2026-10-30) with a small margin
PT_ENVELOPE = {"geometry": '{"xmin":-76.43,"ymin":36.78,"xmax":-76.28,"ymax":36.93,"spatialReference":{"wkid":4326}}',
               "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
# Suffolk: the city parcel layer's extent (2026-10-30) with a small margin
SF_ENVELOPE = {"geometry": '{"xmin":-76.94,"ymin":36.54,"xmax":-76.39,"ymax":36.94,"spatialReference":{"wkid":4326}}',
               "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
# Hampton: the HRGEO parcels' extent for FIPS 650 (2026-10-30) with a small margin
HT_ENVELOPE = {"geometry": '{"xmin":-76.46,"ymin":36.97,"xmax":-76.26,"ymax":37.12,"spatialReference":{"wkid":4326}}',
               "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}
ENVELOPES = {"virginia_beach": VB_ENVELOPE, "chesapeake": CHES_ENVELOPE, "norfolk": NF_ENVELOPE,
             "portsmouth": PT_ENVELOPE, "suffolk": SF_ENVELOPE, "hampton": HT_ENVELOPE}

# Chesapeake (city GIS profiled 2026-10-28; layers measured only on Chesapeake parcels). Its RPA buffer is
# drawn as polygons (100 / 50 ft from the city's perennial-water layer), so RPA acres are measured here.
_CA, _CU = f"{CHES}/Accela/Accela_special_districts_pro/MapServer", f"{CHES}/Accela/Accela_Inspections_Utilities_pro/MapServer"
_CF, _CO = f"{CHES}/Development_Permits/Permits_Front_Counter/MapServer", f"{CHES}/OpenData/OpenData/MapServer"
_SEWER_FMT = "{DIAMETER} in {MATERIAL}, installed {EST_YEAR}"
CHESAPEAKE_LAYERS = (
    Layer("ches_rpa_buffer", f"{_CA}/25", "polygon_share", "Chesapeake Bay RPA buffer",
          "Resource Protection Area buffer drawn by the city from perennial water bodies (Distance in ft). "
          "Clearing, grading and building need an exception.", ("Distance",), "Distance", locality="chesapeake"),
    Layer("ches_cbpa", f"{_CA}/2", "polygon_share", "Chesapeake Bay Preservation Area (city layer)",
          "City Chesapeake Bay Preservation Area features; buffer distances as published.",
          ("FROMBUFDST", "TOBUFDIST"), locality="chesapeake"),
    Layer("ches_proffers", f"{_CF}/21", "polygon_share", "Proffered rezoning area",
          "Land under proffered (conditional) zoning: the case's proffers bind development.", ("APPNO",), "APPNO",
          locality="chesapeake"),
    Layer("ches_zoning", f"{_CO}/26", "polygon_share", "Zoning (city layer)", "Zoning district(s) of the parcel.",
          ("CLASS", "APPNO"), "CLASS", classification=True, locality="chesapeake"),
    Layer("ches_local_historic", f"{_CA}/14", "polygon_share", "Local historic district",
          "Exterior work needs architectural review.", ("DISTRICT", "STATUS"), "DISTRICT", locality="chesapeake"),
    Layer("ches_great_bridge_gateway", f"{_CA}/23", "polygon_share", "Great Bridge Historic Gateway Overlay District",
          "Overlay design standards apply.", locality="chesapeake"),
    Layer("ches_watershed_protection", f"{_CA}/17", "polygon_share", "Watershed Protection District",
          "Development limits to protect the watershed.", ("SUBBASIN",), locality="chesapeake"),
    Layer("ches_fentress_apz", f"{_CA}/4", "polygon_share", "Fentress Accident Potential Zone",
          "Navy airfield accident potential zone: land-use and density limits.", ("ZONE_CODE", "ZONE_DESC"),
          "ZONE_DESC", locality="chesapeake"),
    Layer("ches_fentress_overlay", f"{_CA}/3", "polygon_share", "Fentress Overlay District",
          "Airfield overlay district (noise / safety rules).", locality="chesapeake"),
    Layer("ches_fentress_easement", f"{_CA}/5", "polygon_share", "Fentress restricted easement",
          "Land under a Navy restrictive easement.", ("EASEMENTTYPE",), "EASEMENTTYPE", locality="chesapeake"),
    Layer("ches_shrink_swell", f"{_CU}/6", "polygon_share", "Shrink-swell soils (city layer)",
          "Soils that expand and shrink with moisture: foundation design and earthwork cost.", ("MUSYM",), "MUSYM",
          locality="chesapeake"),
    Layer("ches_hrsd_service", f"{_CU}/0", "polygon_share", "HRSD service area",
          "Area served by the regional sewer authority (HRSD).", locality="chesapeake"),
    Layer("ches_pump_station_areas", f"{_CU}/2", "polygon_share", "Sewer pump station service area",
          "The city pump station whose service area covers the parcel, and its HRSD connection, as published. "
          "Capacity is not published.", ("PUMP_STATI", "HRSDCONNCT"), "PUMP_STATI", classification=True,
          locality="chesapeake"),
    Layer("ches_unserved_sewer", f"{_CU}/4", "polygon_share", "Area without city sewer service",
          "Published unserved sewer area: a connection may need an extension or a septic system.", ("LABEL",),
          "LABEL", locality="chesapeake"),
    Layer("ches_unserved_water", f"{_CU}/5", "polygon_share", "Area without city water service",
          "Published unserved water area.", ("LABEL",), "LABEL", locality="chesapeake"),
    Layer("ches_gravity_sewer", f"{_CF}/5", "line_crossing", "City gravity sewer main",
          "Nearest published city gravity sewer main within 300 ft (size and material as published; capacity "
          "not published).", ("DIAMETER", "MATERIAL", "EST_YEAR", "PUMPSTA", "OWNEDBY"), near_ft=300,
          fmt=_SEWER_FMT, locality="chesapeake"),
    Layer("ches_water_lines", f"{_CF}/6", "line_crossing", "City water line",
          "Nearest published city water line within 300 ft.", ("DIAMETER", "MATL", "INST_YEAR", "STATUS"),
          near_ft=300, fmt="{DIAMETER} in {MATL}, installed {INST_YEAR}", locality="chesapeake"),
    # federal and state layers, Chesapeake's extent / survey area
    Layer("ches_usda_soils", "VA550", "polygon_share", "Soils (USDA soil survey)",
          LAYERS["usda_soils"].meaning, LAYERS["usda_soils"].attrs, "muname", classification=True, source="sda",
          locality="chesapeake"),
    Layer("ches_nwi_wetlands", LAYERS["nwi_wetlands"].url, "polygon_share", LAYERS["nwi_wetlands"].label,
          LAYERS["nwi_wetlands"].meaning, LAYERS["nwi_wetlands"].attrs, "WETLAND_TYPE", bbox=True,
          where=LAYERS["nwi_wetlands"].where, locality="chesapeake"),
    Layer("ches_nrhp_polygons", LAYERS["nrhp_polygons"].url, "polygon_share", LAYERS["nrhp_polygons"].label,
          LAYERS["nrhp_polygons"].meaning, LAYERS["nrhp_polygons"].attrs, "RESNAME", where="County = 'Chesapeake'",
          locality="chesapeake"),
    Layer("ches_nrhp_points", LAYERS["nrhp_points"].url, "point_near", LAYERS["nrhp_points"].label,
          LAYERS["nrhp_points"].meaning, LAYERS["nrhp_points"].attrs, near_ft=500, where="County = 'Chesapeake'",
          fmt=LAYERS["nrhp_points"].fmt, locality="chesapeake"),
    Layer("ches_vdot_adt", LAYERS["vdot_adt"].url, "line_crossing", LAYERS["vdot_adt"].label,
          LAYERS["vdot_adt"].meaning, LAYERS["vdot_adt"].attrs, near_ft=300,
          where="FROM_JURISDICTION LIKE '%Chesapeake%' OR TO_JURISDICTION LIKE '%Chesapeake%'",
          fmt=LAYERS["vdot_adt"].fmt, locality="chesapeake"),
    Layer("ches_vdot_syip_lines", LAYERS["vdot_syip_lines"].url, "line_crossing", LAYERS["vdot_syip_lines"].label,
          LAYERS["vdot_syip_lines"].meaning, LAYERS["vdot_syip_lines"].attrs, near_ft=1000,
          where="JURISDICTION_DESC LIKE '%Chesapeake%'", fmt=LAYERS["vdot_syip_lines"].fmt, locality="chesapeake"),
    Layer("ches_vdot_syip_points", LAYERS["vdot_syip_points"].url, "point_near", LAYERS["vdot_syip_points"].label,
          LAYERS["vdot_syip_points"].meaning, LAYERS["vdot_syip_points"].attrs, near_ft=1000,
          where="JURISDICTION_DESC LIKE '%Chesapeake%'", fmt=LAYERS["vdot_syip_points"].fmt, locality="chesapeake"),
)
LAYERS.update({l.key: l for l in CHESAPEAKE_LAYERS})

# Norfolk (gisshare.norfolk.gov OpenData, profiled 2026-10-29; measured only on Norfolk parcels). Its CBPA layer
# is polygons with DESIGNATION RPA / IDA, so RPA acres are measured. No sewer / water layer is published.
NF = "https://gisshare.norfolk.gov/pubserver/rest/services/OpenData"
_NP, _NZ, _NE = f"{NF}/Planning/MapServer", f"{NF}/Planning_and_Zoning_Group_OpenData/MapServer", f"{NF}/Environmental/MapServer"
NORFOLK_LAYERS = (
    Layer("nf_cbpa", f"{_NP}/0", "polygon_share", "Chesapeake Bay Preservation Area (Norfolk)",
          "RPA: Resource Protection Area (clearing, grading and building need an exception). IDA: Intensely "
          "Developed Area (redevelopment rules apply).", ("DESIGNATION",), "DESIGNATION", locality="norfolk"),
    Layer("nf_zoning", f"{_NP}/1", "polygon_share", "Zoning (city layer)", "Zoning district(s) of the parcel.",
          ("ZONE", "TYPE"), "ZONE", classification=True, locality="norfolk"),
    Layer("nf_conditional_zoning", f"{_NP}/3", "polygon_share", "Conditional zoning (by ordinance)",
          "Land rezoned with conditions: the ordinance's conditions bind development.", ("ITEMNO", "ORDINANCE"),
          "ORDINANCE", locality="norfolk"),
    Layer("nf_zoning_overlays", f"{_NP}/6", "polygon_share", "Zoning overlay",
          "Overlay districts (airfield noise and accident potential, neighbourhood and design overlays, landmarks): "
          "extra standards apply.", ("NAME",), "NAME", locality="norfolk"),
    Layer("nf_aicuz_apz", f"{_NZ}/30", "polygon_share", "Navy airfield Accident Potential Zone (AICUZ)",
          "Accident potential zone: land-use and density limits.", ("ZONES",), "ZONES", locality="norfolk"),
    Layer("nf_aicuz_noise", f"{_NZ}/31", "polygon_share", "Navy airfield noise zone (AICUZ)",
          "Day-night average noise level contour: noise attenuation and use limits.", ("NOISE_LEV_",), "NOISE_LEV_",
          locality="norfolk"),
    Layer("nf_wave_action", f"{_NZ}/35", "line_crossing", "Limit of Moderate Wave Action",
          "FEMA's limit of moderate wave action (1.5 ft waves): coastal construction standards seaward of it.",
          near_ft=300, locality="norfolk"),
    # the layer codes upland as "U" (most of the city) and includes open deepwater (E1 / M1 subtidal); neither is a
    # wetland on a parcel, the same rule as the NWI layer's deepwater exclusion
    Layer("nf_wetlands", f"{_NE}/1", "polygon_share", "Wetlands (city layer)",
          "Mapped wetland (NWI-style code), not a delineation.", ("ATTRIBUTE",), "ATTRIBUTE",
          where="ATTRIBUTE <> 'U' AND ATTRIBUTE NOT LIKE 'E1%' AND ATTRIBUTE NOT LIKE 'M1%'", locality="norfolk"),
    # federal and state layers, Norfolk's extent / survey area
    Layer("nf_usda_soils", "VA715", "polygon_share", "Soils (USDA soil survey)",
          LAYERS["usda_soils"].meaning, LAYERS["usda_soils"].attrs, "muname", classification=True, source="sda",
          locality="norfolk"),
    Layer("nf_nwi_wetlands", LAYERS["nwi_wetlands"].url, "polygon_share", LAYERS["nwi_wetlands"].label,
          LAYERS["nwi_wetlands"].meaning, LAYERS["nwi_wetlands"].attrs, "WETLAND_TYPE", bbox=True,
          where=LAYERS["nwi_wetlands"].where, locality="norfolk"),
    # County = 'Norfolk' alone also matches Norfolk County, Massachusetts
    Layer("nf_nrhp_polygons", LAYERS["nrhp_polygons"].url, "polygon_share", LAYERS["nrhp_polygons"].label,
          LAYERS["nrhp_polygons"].meaning, LAYERS["nrhp_polygons"].attrs, "RESNAME",
          where="County = 'Norfolk' AND State = 'VIRGINIA'", locality="norfolk"),
    Layer("nf_nrhp_points", LAYERS["nrhp_points"].url, "point_near", LAYERS["nrhp_points"].label,
          LAYERS["nrhp_points"].meaning, LAYERS["nrhp_points"].attrs, near_ft=500,
          where="County = 'Norfolk' AND State = 'VIRGINIA'", fmt=LAYERS["nrhp_points"].fmt, locality="norfolk"),
    # 'City of Norfolk' exactly: "Norfolk Maintenance Area" is VDOT's maintenance region, not the city
    Layer("nf_vdot_adt", LAYERS["vdot_adt"].url, "line_crossing", LAYERS["vdot_adt"].label,
          LAYERS["vdot_adt"].meaning, LAYERS["vdot_adt"].attrs, near_ft=300,
          where="FROM_JURISDICTION = 'City of Norfolk' OR TO_JURISDICTION = 'City of Norfolk'",
          fmt=LAYERS["vdot_adt"].fmt, locality="norfolk"),
    Layer("nf_vdot_syip_lines", LAYERS["vdot_syip_lines"].url, "line_crossing", LAYERS["vdot_syip_lines"].label,
          LAYERS["vdot_syip_lines"].meaning, LAYERS["vdot_syip_lines"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Norfolk'", fmt=LAYERS["vdot_syip_lines"].fmt, locality="norfolk"),
    Layer("nf_vdot_syip_points", LAYERS["vdot_syip_points"].url, "point_near", LAYERS["vdot_syip_points"].label,
          LAYERS["vdot_syip_points"].meaning, LAYERS["vdot_syip_points"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Norfolk'", fmt=LAYERS["vdot_syip_points"].fmt, locality="norfolk"),
)
LAYERS.update({l.key: l for l in NORFOLK_LAYERS})

# Portsmouth: federal and state layers only -- the city's own GIS server refuses this environment's connections
# (2026-10-30), so its zoning / CBPA / historic layers are not measured. Names confirmed against each service.
PORTSMOUTH_LAYERS = (
    Layer("pt_usda_soils", "VA715", "polygon_share", "Soils (USDA soil survey)",
          LAYERS["usda_soils"].meaning, LAYERS["usda_soils"].attrs, "muname", classification=True, source="sda",
          locality="portsmouth"),
    Layer("pt_nwi_wetlands", LAYERS["nwi_wetlands"].url, "polygon_share", LAYERS["nwi_wetlands"].label,
          LAYERS["nwi_wetlands"].meaning, LAYERS["nwi_wetlands"].attrs, "WETLAND_TYPE", bbox=True,
          where=LAYERS["nwi_wetlands"].where, locality="portsmouth"),
    Layer("pt_nrhp_polygons", LAYERS["nrhp_polygons"].url, "polygon_share", LAYERS["nrhp_polygons"].label,
          LAYERS["nrhp_polygons"].meaning, LAYERS["nrhp_polygons"].attrs, "RESNAME",
          where="County = 'Portsmouth' AND State = 'VIRGINIA'", locality="portsmouth"),
    Layer("pt_nrhp_points", LAYERS["nrhp_points"].url, "point_near", LAYERS["nrhp_points"].label,
          LAYERS["nrhp_points"].meaning, LAYERS["nrhp_points"].attrs, near_ft=500,
          where="County = 'Portsmouth' AND State = 'VIRGINIA'", fmt=LAYERS["nrhp_points"].fmt, locality="portsmouth"),
    Layer("pt_vdot_adt", LAYERS["vdot_adt"].url, "line_crossing", LAYERS["vdot_adt"].label,
          LAYERS["vdot_adt"].meaning, LAYERS["vdot_adt"].attrs, near_ft=300,
          where="FROM_JURISDICTION = 'City of Portsmouth' OR TO_JURISDICTION = 'City of Portsmouth'",
          fmt=LAYERS["vdot_adt"].fmt, locality="portsmouth"),
    Layer("pt_vdot_syip_lines", LAYERS["vdot_syip_lines"].url, "line_crossing", LAYERS["vdot_syip_lines"].label,
          LAYERS["vdot_syip_lines"].meaning, LAYERS["vdot_syip_lines"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Portsmouth'", fmt=LAYERS["vdot_syip_lines"].fmt, locality="portsmouth"),
    Layer("pt_vdot_syip_points", LAYERS["vdot_syip_points"].url, "point_near", LAYERS["vdot_syip_points"].label,
          LAYERS["vdot_syip_points"].meaning, LAYERS["vdot_syip_points"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Portsmouth'", fmt=LAYERS["vdot_syip_points"].fmt, locality="portsmouth"),
)
LAYERS.update({l.key: l for l in PORTSMOUTH_LAYERS})

# Suffolk (suffolkgis.suffolk-va.net, profiled 2026-10-30; measured only on Suffolk parcels). Its RPA is the 100-ft
# boundary line (like Virginia Beach's): "crosses / lies N ft from", never RPA acres. Sewer and water mains are
# published: size and material as published, capacity never inferred.
SF = "https://suffolkgis.suffolk-va.net/hosting/rest/services"
_SZ = f"{SF}/Zoning_Map/MapServer"
SUFFOLK_LAYERS = (
    Layer("sf_rpa", f"{_SZ}/6", "line_crossing", "Chesapeake Bay RPA boundary (100 ft)",
          "The city's Resource Protection Area boundary line, 100 ft from the protected feature: the parcel is crossed "
          "by it or lies within 100 ft of it (no RPA acres claimed).", near_ft=100, locality="suffolk"),
    Layer("sf_cbpa", f"{SF}/SharedMaps/Resource_Protection_Areas/MapServer/1", "polygon_share",
          "Chesapeake Bay Preservation Area (city layer)", "Chesapeake Bay Preservation Area: CBPA performance "
          "criteria apply.", locality="suffolk"),
    Layer("sf_zoning", f"{_SZ}/11", "polygon_share", "Zoning (city layer)", "Zoning district(s) of the parcel.",
          ("CODE",), "CODE", classification=True, locality="suffolk"),
    Layer("sf_airport_overlay", f"{_SZ}/1", "polygon_share", "Airport Overlay District",
          "Airport overlay: height and land-use limits.", ("OVERLAY_TYPE",), "OVERLAY_TYPE", locality="suffolk"),
    Layer("sf_historic_overlay", f"{_SZ}/4", "polygon_share", "Historic Conservation Overlay District",
          "Exterior work needs historic review.", ("OVERLAY_TYPE",), locality="suffolk"),
    Layer("sf_historic_districts", f"{_SZ}/5", "polygon_share", "National Register historic district (city layer)",
          "A National Register historic district as mapped by the city.", ("DISTRICT", "DIST_TYPE"), "DISTRICT",
          locality="suffolk"),
    Layer("sf_scod", f"{_SZ}/7", "polygon_share", "Special Corridor Overlay District",
          "Corridor design standards apply.", ("OVERLAY_TYPE",), locality="suffolk"),
    Layer("sf_growth_areas", f"{_SZ}/3", "polygon_share", "Growth area (comprehensive plan)",
          "Inside a growth area designated in the city's comprehensive plan.", ("G_Area",), "G_Area",
          locality="suffolk"),
    Layer("sf_sewer_service", f"{SF}/Sewer_Service_Areas/MapServer/1", "polygon_share", "Sewer service area",
          "The sewer service area and its receiving facility, as published. Capacity is not published.",
          ("AREANAME", "TYPE", "RECEIVER"), "AREANAME", classification=True, locality="suffolk"),
    Layer("sf_gravity_sewer", f"{SF}/Sewer_Gravity_Main/MapServer/1", "line_crossing", "City gravity sewer main",
          "Nearest published city gravity sewer main within 300 ft (size and material as published; capacity not "
          "published).", ("DIAMETER", "MATERIAL", "OWNERSHIP"), near_ft=300, fmt="{DIAMETER} in {MATERIAL}",
          locality="suffolk"),
    Layer("sf_water_main", f"{SF}/Water_Main/MapServer/1", "line_crossing", "City water main",
          "Nearest published city water main within 300 ft.", ("Diameter", "Material", "Ownership"), near_ft=300,
          fmt="{Diameter} in {Material}", locality="suffolk"),
    # federal and state layers, Suffolk's extent / survey area. NRHP has a Suffolk in VA, NY and MA.
    Layer("sf_usda_soils", "VA800", "polygon_share", "Soils (USDA soil survey)",
          LAYERS["usda_soils"].meaning, LAYERS["usda_soils"].attrs, "muname", classification=True, source="sda",
          locality="suffolk"),
    Layer("sf_nwi_wetlands", LAYERS["nwi_wetlands"].url, "polygon_share", LAYERS["nwi_wetlands"].label,
          LAYERS["nwi_wetlands"].meaning, LAYERS["nwi_wetlands"].attrs, "WETLAND_TYPE", bbox=True,
          where=LAYERS["nwi_wetlands"].where, locality="suffolk"),
    Layer("sf_nrhp_polygons", LAYERS["nrhp_polygons"].url, "polygon_share", LAYERS["nrhp_polygons"].label,
          LAYERS["nrhp_polygons"].meaning, LAYERS["nrhp_polygons"].attrs, "RESNAME",
          where="County = 'Suffolk' AND State = 'VIRGINIA'", locality="suffolk"),
    Layer("sf_nrhp_points", LAYERS["nrhp_points"].url, "point_near", LAYERS["nrhp_points"].label,
          LAYERS["nrhp_points"].meaning, LAYERS["nrhp_points"].attrs, near_ft=500,
          where="County = 'Suffolk' AND State = 'VIRGINIA'", fmt=LAYERS["nrhp_points"].fmt, locality="suffolk"),
    Layer("sf_vdot_adt", LAYERS["vdot_adt"].url, "line_crossing", LAYERS["vdot_adt"].label,
          LAYERS["vdot_adt"].meaning, LAYERS["vdot_adt"].attrs, near_ft=300,
          where="FROM_JURISDICTION = 'City of Suffolk' OR TO_JURISDICTION = 'City of Suffolk'",
          fmt=LAYERS["vdot_adt"].fmt, locality="suffolk"),
    Layer("sf_vdot_syip_lines", LAYERS["vdot_syip_lines"].url, "line_crossing", LAYERS["vdot_syip_lines"].label,
          LAYERS["vdot_syip_lines"].meaning, LAYERS["vdot_syip_lines"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Suffolk'", fmt=LAYERS["vdot_syip_lines"].fmt, locality="suffolk"),
    Layer("sf_vdot_syip_points", LAYERS["vdot_syip_points"].url, "point_near", LAYERS["vdot_syip_points"].label,
          LAYERS["vdot_syip_points"].meaning, LAYERS["vdot_syip_points"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Suffolk'", fmt=LAYERS["vdot_syip_points"].fmt, locality="suffolk"),
)
LAYERS.update({l.key: l for l in SUFFOLK_LAYERS})

# Hampton: federal and state layers only -- the city's own GIS server refuses this environment's connections
# (2026-10-30). Names confirmed against each service: NRHP has a Hampton in VA and SC; VDOT also lists
# Northampton / Southampton counties and "Hampton Roads District-wide", so only exact names are used.
HAMPTON_LAYERS = (
    Layer("ht_usda_soils", "VA715", "polygon_share", "Soils (USDA soil survey)",
          LAYERS["usda_soils"].meaning, LAYERS["usda_soils"].attrs, "muname", classification=True, source="sda",
          locality="hampton"),
    Layer("ht_nwi_wetlands", LAYERS["nwi_wetlands"].url, "polygon_share", LAYERS["nwi_wetlands"].label,
          LAYERS["nwi_wetlands"].meaning, LAYERS["nwi_wetlands"].attrs, "WETLAND_TYPE", bbox=True,
          where=LAYERS["nwi_wetlands"].where, locality="hampton"),
    Layer("ht_nrhp_polygons", LAYERS["nrhp_polygons"].url, "polygon_share", LAYERS["nrhp_polygons"].label,
          LAYERS["nrhp_polygons"].meaning, LAYERS["nrhp_polygons"].attrs, "RESNAME",
          where="County = 'Hampton' AND State = 'VIRGINIA'", locality="hampton"),
    Layer("ht_nrhp_points", LAYERS["nrhp_points"].url, "point_near", LAYERS["nrhp_points"].label,
          LAYERS["nrhp_points"].meaning, LAYERS["nrhp_points"].attrs, near_ft=500,
          where="County = 'Hampton' AND State = 'VIRGINIA'", fmt=LAYERS["nrhp_points"].fmt, locality="hampton"),
    Layer("ht_vdot_adt", LAYERS["vdot_adt"].url, "line_crossing", LAYERS["vdot_adt"].label,
          LAYERS["vdot_adt"].meaning, LAYERS["vdot_adt"].attrs, near_ft=300,
          where="FROM_JURISDICTION = 'City of Hampton' OR TO_JURISDICTION = 'City of Hampton'",
          fmt=LAYERS["vdot_adt"].fmt, locality="hampton"),
    Layer("ht_vdot_syip_lines", LAYERS["vdot_syip_lines"].url, "line_crossing", LAYERS["vdot_syip_lines"].label,
          LAYERS["vdot_syip_lines"].meaning, LAYERS["vdot_syip_lines"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Hampton'", fmt=LAYERS["vdot_syip_lines"].fmt, locality="hampton"),
    Layer("ht_vdot_syip_points", LAYERS["vdot_syip_points"].url, "point_near", LAYERS["vdot_syip_points"].label,
          LAYERS["vdot_syip_points"].meaning, LAYERS["vdot_syip_points"].attrs, near_ft=1000,
          where="JURISDICTION_DESC = 'Hampton'", fmt=LAYERS["vdot_syip_points"].fmt, locality="hampton"),
)
LAYERS.update({l.key: l for l in HAMPTON_LAYERS})
# the same national layer under another city's key: soil summary, NRHP boxes and wording behave the same
BASE_KEY = {**{l.key: l.key.removeprefix("ches_") for l in CHESAPEAKE_LAYERS},
            **{l.key: l.key.removeprefix("nf_") for l in NORFOLK_LAYERS},
            **{l.key: l.key.removeprefix("pt_") for l in PORTSMOUTH_LAYERS},
            **{l.key: l.key.removeprefix("sf_") for l in SUFFOLK_LAYERS},
            **{l.key: l.key.removeprefix("ht_") for l in HAMPTON_LAYERS}}

BOARD_LAYERS = {
    "cbpa": (f"{PLAN}/3", "Chesapeake Bay Preservation Area Board actions"),
    "wetlands_board": (f"{PLAN}/5", "Wetlands Board actions and waterfront permits"),
    "hrb": (f"{PLAN}/4", "Historical Review Board actions"),
}


def to_feet(geom):
    """Lon/lat geometry -> local feet (x east, y north)."""
    return shapely.transform(geom, lambda xy: xy * np.array([COS0 * FT_PER_DEG, FT_PER_DEG]))


def keep_attrs(layer: Layer, props: dict) -> dict:
    """Only the layer's allowlisted fields; epoch-millisecond DATE fields as YYYY-MM-DD."""
    from datetime import datetime, timezone
    out = {}
    for name in layer.attrs:
        v = next((v for k, v in (props or {}).items() if k.upper() == name.upper()), None)
        if isinstance(v, str):
            v = v.strip() or None
        if "DATE" in name.upper() and isinstance(v, (int, float)) and v > 1e11:
            v = datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out[name] = v
    return out


def _base(key: str) -> str:
    """The national layer a city-specific key stands for (ches_usda_soils -> usda_soils)."""
    return BASE_KEY.get(key, key)


def _attr(props: dict | None, name: str):
    return next((v for k, v in (props or {}).items() if k.upper() == name.upper()), None)


def _circumscribed(props: dict | None) -> bool:
    return str(_attr(props, "BND_TYPE") or "").lower().startswith("circumscribed")


def normalize(layer: Layer, features: list[dict], stats) -> list[dict]:
    """GeoJSON features -> {feature_id, geom (lon/lat), attrs}. Empty or wrong
    geometry is counted, never stored."""
    out = []
    for f in features:
        g = f.get("geometry")
        if not g:
            stats["no_geometry"] += 1
            continue
        geom = shape(g)
        if _base(layer.key) == "nrhp_polygons" and _circumscribed(f.get("properties")) and \
                str(_attr(f.get("properties"), "ResType") or "").lower() != "district":
            stats["circumscribed_non_district_skipped"] += 1  # a box around one building, not a boundary:
            continue                                            # its NPS point gives the location instead
        if layer.kind == "polygon_share":
            geom = polygonal(geom)
        elif layer.kind == "point_near":
            geom = geom if geom.geom_type in ("Point", "MultiPoint") else None
        elif geom.geom_type not in ("LineString", "MultiLineString"):
            geom = None
        if geom is None or geom.is_empty:
            stats["unusable_geometry"] += 1
            continue
        fid = f.get("id")
        if fid is None:
            fid = next((v for k, v in (f.get("properties") or {}).items() if k.upper() == "OBJECTID"), None)
        out.append({"feature_id": int(fid), "geom": geom, "attrs": keep_attrs(layer, f.get("properties"))})
    return out


def content_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["feature_id"]):
        h.update(json.dumps([r["feature_id"], r["attrs"]], sort_keys=True, default=str).encode())
        h.update(r["geom"].wkb)
    return h.hexdigest()


def geojson(geom) -> str:
    return json.dumps(mapping(geom), separators=(",", ":"))


class LayerIndex:
    """One active load of one layer, in feet, for per-parcel measurements."""

    def __init__(self, layer: Layer, rows: list[dict]):
        self.layer = layer
        self.rows = rows  # {feature_id, geom (lon/lat), attrs}
        self.ft = [to_feet(r["geom"]) for r in rows]
        self.tree = STRtree(self.ft) if rows else None

    def measure(self, parcel_geom) -> dict | None:
        """The parcel's measurement for this layer, or None when it does not touch
        it and no feature is within the layer's search distance."""
        if self.tree is None:
            return None
        p = to_feet(parcel_geom)
        area = p.area
        if self.layer.kind == "polygon_share":
            hits = [i for i in self.tree.query(p, predicate="intersects")]
            inter = union_all([self.ft[i] for i in hits]).intersection(p) if hits and area > 0 else None
            overlap = inter.area if inter is not None else 0.0
            if overlap <= 0:
                return self._nearest(p) if self.layer.near_ft else None
            detail = {}
            if self.layer.group_by:
                by: dict[str, list] = {}
                for i in hits:
                    part = self.ft[i].intersection(p).area
                    if part > 0:
                        k = self.rows[i]["attrs"].get(self.layer.group_by) or "unnamed"
                        by.setdefault(k, [0.0, self.rows[i]["attrs"]])[0] += part
                detail["by"] = [{"name": k, "acres": round(v / SQFT_PER_ACRE, 3), "share": round(v / area, 4),
                                 **({"attrs": a} if self.layer.source == "sda" or _base(self.layer.key) in ("nrhp_polygons", "pump_station_areas")
                                    else {})}
                                for k, (v, a) in sorted(by.items(), key=lambda kv: -kv[1][0])]
                if self.layer.source == "sda":
                    detail.update(soil_summary(detail["by"]))
            return {"overlap_sqft": round(overlap, 1), "share": round(min(1.0, overlap / area), 4),
                    "line_length_ft": None, "distance_ft": None, "detail": detail}
        reach = self.layer.near_ft or NEAR_FT
        near = list(self.tree.query(p, predicate="dwithin", distance=reach))
        if not near:
            return None
        dist = {i: p.distance(self.ft[i]) for i in near}
        closest = min(dist, key=lambda i: (dist[i], i))
        detail = {"nearest": self.rows[closest]["attrs"]} if self.layer.attrs else {}
        if self.layer.kind == "point_near":
            detail["within"] = len(near)
            return {"overlap_sqft": None, "share": None, "line_length_ft": None,
                    "distance_ft": round(dist[closest], 1), "detail": detail}
        inside = union_all([self.ft[i] for i in near]).intersection(p).length
        return {"overlap_sqft": None, "share": None, "line_length_ft": round(inside, 1),
                "distance_ft": 0.0 if inside > 0 else round(dist[closest], 1), "detail": detail}

    def _nearest(self, p) -> dict | None:
        """No overlap: the distance to the nearest polygon within the layer's near_ft."""
        near = list(self.tree.query(p, predicate="dwithin", distance=self.layer.near_ft))
        if not near:
            return None
        d = {i: p.distance(self.ft[i]) for i in near}
        i = min(d, key=lambda j: (d[j], j))
        return {"overlap_sqft": None, "share": 0.0, "line_length_ft": None, "distance_ft": round(d[i], 1),
                "detail": {"nearest": self.rows[i]["attrs"]}}


def soil_summary(by: list[dict]) -> dict:
    """Shallow water table and hydric shares of the parcel from its soil map units."""
    def total(pred):
        sel = [b for b in by if pred(b["attrs"])]
        return {"acres": round(sum(b["acres"] for b in sel), 3), "share": round(sum(b["share"] for b in sel), 4)}
    return {"shallow_water_table": total(lambda a: a.get("wtdepannmin") is not None
                                         and a["wtdepannmin"] < WATER_TABLE_CM),
            "hydric": total(lambda a: a.get("hydclprs") is not None and a["hydclprs"] >= HYDRIC_MIN_PCT)}


def load_layer_index(conn, layer: Layer) -> tuple[int, LayerIndex] | None:
    load = conn.execute("SELECT load_id FROM constraint_loads WHERE layer_key = ? AND is_active = 1",
                        (layer.key,)).fetchone()
    if load is None:
        return None
    rows = [{"feature_id": r["feature_id"], "geom": shape(json.loads(r["geom_geojson"])),
             "attrs": json.loads(r["attrs_json"])}
            for r in conn.execute("SELECT feature_id, geom_geojson, attrs_json FROM constraint_features "
                                  "WHERE load_id = ? ORDER BY feature_id", (load["load_id"],))]
    return load["load_id"], LayerIndex(layer, rows)


def _fmt(layer: Layer, attrs: dict | None) -> str:
    if not layer.fmt or not attrs:
        return ""
    vals = {}
    for k, v in attrs.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k.upper() in ("ADT", "TOTAL_ALLOCATIONS_CURRENT"):
            v = f"{v:,.0f}"
        vals[k] = "n/a" if v is None else v
    try:
        return layer.fmt.format(**vals)
    except (KeyError, ValueError):
        return ""


def describe(layer_key: str, row: dict) -> str:
    """One measured fact, in plain words."""
    layer = LAYERS[layer_key]
    detail = json.loads(row["detail_json"]) if isinstance(row.get("detail_json"), str) else (row.get("detail") or {})
    if _base(layer.key) == "pump_station_areas":
        parts = [f"pump station {b['name']}: {b['acres']:,.2f} ac ({b['share'] * 100:.0f}%)"
                 + (f", HRSD connection {b['attrs']['HRSDCONNCT']} (as published)"
                    if (b.get("attrs") or {}).get("HRSDCONNCT") else "") for b in detail.get("by", [])[:4]]
        return "Service area of " + "; ".join(parts) if parts else "mapped"
    if layer.classification:
        parts = [f"{b['name']} {b['acres']:,.2f} ac ({b['share'] * 100:.0f}%)" for b in detail.get("by", [])[:4]]
        text = "Mapped: " + "; ".join(parts) if parts else "mapped"
        if layer.source == "sda":
            wt, hy = detail.get("shallow_water_table", {}), detail.get("hydric", {})
            text += (f". Water table within 18 in: {wt.get('acres', 0):,.2f} ac ({wt.get('share', 0) * 100:.0f}%)"
                     f"; hydric map units (>= {HYDRIC_MIN_PCT}%): {hy.get('acres', 0):,.2f} ac "
                     f"({hy.get('share', 0) * 100:.0f}%)")
        return text
    what = _fmt(layer, detail.get("nearest"))
    if layer.kind == "point_near":
        n = detail.get("within", 1)
        return (f"{layer.label} {row['distance_ft']:,.0f} ft from the parcel" + (f": {what}" if what else "")
                + (f" ({n} within {layer.near_ft:,.0f} ft)" if n > 1 else ""))
    if layer.kind == "line_crossing":
        if row["line_length_ft"]:
            return (f"{layer.label} crosses the parcel ({row['line_length_ft']:,.0f} ft inside it)"
                    + (f": {what}" if what else ""))
        return f"{layer.label} lies {row['distance_ft']:,.0f} ft from the parcel" + (f": {what}" if what else "")
    if not row["overlap_sqft"]:
        return f"{layer.label} {row['distance_ft']:,.0f} ft from the parcel" + (f": {what}" if what else "")
    acres = row["overlap_sqft"] / SQFT_PER_ACRE
    text = f"{acres:,.2f} ac ({row['share'] * 100:.0f}%) of the parcel inside: {layer.label}"
    if _base(layer.key) == "nrhp_polygons":
        b = (detail.get("by") or [{}])[0]
        if _circumscribed(b.get("attrs")):
            return (f"{acres:,.2f} ac ({row['share'] * 100:.0f}%) of the parcel inside the approximate (circumscribed) "
                    f"mapping area of {b.get('name', '')}: the listed district boundary may be smaller")
        text += f": {b.get('name', '')}"
    return text


def public_constraints(conn, gpins: list[str]) -> dict:
    """The site-constraint block for a set of parcels (API and CSV): measured
    facts per layer, the boards' history on those parcels, which layers were
    checked and what is not covered. No raw attributes beyond each layer's
    allowlist; applicant names in board actions only for organizations."""
    from ingest import filings as F  # local import: filings pulls in lead_facts
    loaded = {r["layer_key"]: r["loaded_at"] for r in conn.execute(
        "SELECT layer_key, loaded_at FROM constraint_loads WHERE is_active = 1")}
    items = []
    if gpins:
        marks = ",".join("?" * len(gpins))
        for r in conn.execute(
                f"SELECT gpin, layer_key, overlap_sqft, share, line_length_ft, distance_ft, detail_json, "
                f"layer_loaded_at, source_url FROM parcel_constraints_effective WHERE gpin IN ({marks}) "
                f"ORDER BY layer_key, gpin", gpins):
            d = dict(r)
            if d["layer_key"] not in LAYERS:
                continue
            layer = LAYERS[d["layer_key"]]
            items.append({"gpin": d["gpin"], "layer": layer.key, "classification": layer.classification, "label": layer.label, "kind": layer.kind,
                          "meaning": layer.meaning, "fact": describe(layer.key, d),
                          "acres": round(d["overlap_sqft"] / SQFT_PER_ACRE, 3) if d["overlap_sqft"] else None,
                          "share": d["share"], "line_length_ft": d["line_length_ft"], "distance_ft": d["distance_ft"],
                          "detail": json.loads(d["detail_json"]), "as_of": d["layer_loaded_at"],
                          "source_url": d["source_url"]})
    items.sort(key=lambda x: (x["classification"], x["layer"], x["gpin"]))  # constraints first, soils last
    history = []
    if gpins:
        for r in conn.execute(
                f"SELECT DISTINCT a.layer_key, a.pln_id, a.date_sub, a.pln_type, a.appl_name, a.deeplink "
                f"FROM board_action_parcels bp JOIN board_actions_effective a ON a.load_id = bp.load_id "
                f"AND a.pln_id = bp.pln_id WHERE bp.gpin IN ({marks}) ORDER BY a.date_sub DESC", gpins):
            history.append({"board": r["layer_key"], "board_label": BOARD_LAYERS.get(r["layer_key"], ("", ""))[1],
                            "pln_id": r["pln_id"], "date_sub": r["date_sub"], "pln_type": r["pln_type"],
                            "applicant": F._public_name(r["appl_name"]), "deeplink": r["deeplink"]})
    boards = {r["layer_key"]: r["loaded_at"] for r in conn.execute(
        "SELECT layer_key, loaded_at FROM board_action_loads WHERE is_active = 1")}
    # checked / not covered are stated for the parcels' own city (no gpins: Virginia Beach, as before)
    cities = {r[0] for r in conn.execute(
        f"SELECT DISTINCT l.locality FROM parcels p JOIN parcel_loads l ON l.load_id = p.load_id AND l.is_active = 1 "
        f"WHERE p.gpin IN ({marks})", gpins)} if gpins else set()
    cities = cities or {"virginia_beach"}
    loaded = {k: v for k, v in loaded.items() if k in LAYERS and LAYERS[k].locality in cities}
    boards_note = None
    if "virginia_beach" not in cities:
        boards = {}  # the board histories are Virginia Beach's
        boards_note = "Board histories are loaded for Virginia Beach only."
    not_covered = []
    if cities & {"virginia_beach", "norfolk", "portsmouth", "hampton"}:  # none publishes (or serves us) its lines
        not_covered.append("sewer / water capacity and pipe locations (not published)")
    if cities & {"chesapeake", "suffolk"}:
        not_covered.append("sewer / water capacity (not published; mains and service areas are shown as published)")
    not_covered.append("Virginia Landmarks Register (state DHR service unreachable)")
    return {
        "constraints": items, "board_history": history,
        "layers_checked": [{"layer": k, "label": LAYERS[k].label, "as_of": v} for k, v in sorted(loaded.items())
                           if k in LAYERS],
        "boards_checked": [{"board": k, "label": BOARD_LAYERS[k][1], "as_of": v} for k, v in sorted(boards.items())
                           if k in BOARD_LAYERS],
        "not_covered": not_covered, "boards_note": boards_note,
        "notice": "Screening estimates from public GIS layers (city, USDA, USFWS, NPS, VDOT), not a determination: "
                  "confirm with a site survey, a wetland delineation and the city's RPA determination. Names of "
                  "private individuals are not shown.",
    }


CSV_COLUMNS = ("rpa", "southern_watersheds_buffer_share", "special_floodplain_share", "shrink_swell_share",
               "nwi_wetland_share", "shallow_water_table_share", "hydric_share", "nrhp", "nearest_adt",
               "syip_within_ft", "sewer_pump_station_project_ft", "utility_project_area", "rpa_buffer_share",
               "unserved_sewer_share", "gravity_sewer_ft", "fentress_apz_share",
               "historic_overlay", "dam_break", "prior_cbpa_actions", "prior_wetlands_board_actions",
               "prior_hrb_actions")


def csv_summary(conn, gpins: list[str]) -> dict:
    """Flat constraint columns for a lead's parcels (CSV): the largest share over
    the parcels, RPA as crosses / within N ft / blank, counts of prior board
    actions. Blank means not touching (or the layer not loaded)."""
    out = {c: "" for c in CSV_COLUMNS}
    if not gpins:
        return out
    marks = ",".join("?" * len(gpins))
    best: dict[str, dict] = {}
    near: dict[str, dict] = {}
    for r in conn.execute(f"SELECT layer_key, share, line_length_ft, distance_ft, detail_json "
                          f"FROM parcel_constraints_effective WHERE gpin IN ({marks})", gpins):
        k = _base(r["layer_key"])
        if k == "gravity_sewer" and r["distance_ft"] is not None:
            if out["gravity_sewer_ft"] == "" or r["distance_ft"] < out["gravity_sewer_ft"]:
                out["gravity_sewer_ft"] = round(r["distance_ft"])
            continue
        if k == "usda_soils":
            d = json.loads(r["detail_json"])
            for col, part in (("shallow_water_table_share", "shallow_water_table"), ("hydric_share", "hydric")):
                v = d.get(part, {}).get("share")
                if v is not None and (out[col] == "" or v > out[col]):
                    out[col] = round(v, 4)
            continue
        if k in ("vdot_adt", "vdot_syip_lines", "vdot_syip_points", "nrhp_points") and r["distance_ft"] is not None:
            if k not in near or r["distance_ft"] < near[k]["distance_ft"]:
                near[k] = {"distance_ft": r["distance_ft"], "detail": json.loads(r["detail_json"])}
            continue
        if k == "rpa":
            cur = best.get(k)
            if cur is None or (r["distance_ft"] or 0) < (cur["distance_ft"] or 0):
                best[k] = dict(r)
        elif (r["share"] or 0) > (best.get(k, {}).get("share") or 0):
            best[k] = dict(r)
    ps = conn.execute(f"SELECT MIN(distance_ft), MAX(overlap_sqft) FROM parcel_constraints_effective WHERE "
                      f"layer_key = 'sewer_pump_station_projects' AND gpin IN ({marks})", gpins).fetchone()
    if ps[0] is not None or ps[1]:
        out["sewer_pump_station_project_ft"] = 0 if ps[1] else round(ps[0])
    out["utility_project_area"] = " ".join(k for k in ("city_sewer_projects", "city_water_projects",
                                                       "water_sewer_projects") if (best.get(k) or {}).get("share"))
    if "rpa" in best:
        b = best["rpa"]
        out["rpa"] = "crosses" if b["line_length_ft"] else f"within {b['distance_ft']:.0f} ft"
    if "vdot_adt" in near:
        a = near["vdot_adt"]["detail"].get("nearest") or {}
        out["nearest_adt"] = f"{a.get('ADT')} ({a.get('ROUTE_COMMON_NAME')}, {near['vdot_adt']['distance_ft']:.0f} ft)"
    syip = [near[k]["distance_ft"] for k in ("vdot_syip_lines", "vdot_syip_points") if k in near]
    if syip:
        out["syip_within_ft"] = round(min(syip))
    nrhp = [json.loads(r[0]) for r in conn.execute(
        f"SELECT detail_json FROM parcel_constraints_effective WHERE layer_key IN ('nrhp_polygons', "
        f"'ches_nrhp_polygons') AND share > 0 "
        f"AND gpin IN ({marks})", gpins)]
    if any(not _circumscribed((d.get("by") or [{}])[0].get("attrs")) for d in nrhp):
        out["nrhp"] = "inside a listed property"
    elif nrhp:
        out["nrhp"] = "inside an approximate listed-district area"
    elif "nrhp_points" in near:
        out["nrhp"] = f"listed site {near['nrhp_points']['distance_ft']:.0f} ft"
    for col, key in (("southern_watersheds_buffer_share", "southern_watersheds_buffer"),
                     ("special_floodplain_share", "special_floodplain"), ("shrink_swell_share", "shrink_swell_soils"),
                     ("nwi_wetland_share", "nwi_wetlands"), ("rpa_buffer_share", "rpa_buffer"),
                     ("unserved_sewer_share", "unserved_sewer"), ("fentress_apz_share", "fentress_apz")):
        if key in best:
            out[col] = round(best[key]["share"], 4)
    for col, keys in (("historic_overlay", ("historic_overlay", "historic_kempsville")), ("dam_break", ("dam_break",))):
        hit = [best[k]["share"] for k in keys if k in best]
        if hit:
            out[col] = round(max(hit), 4)
    for col, board in (("prior_cbpa_actions", "cbpa"), ("prior_wetlands_board_actions", "wetlands_board"),
                       ("prior_hrb_actions", "hrb")):
        out[col] = conn.execute(
            f"SELECT COUNT(DISTINCT a.pln_id) FROM board_action_parcels bp JOIN board_actions_effective a "
            f"ON a.load_id = bp.load_id AND a.pln_id = bp.pln_id WHERE a.layer_key = ? AND bp.gpin IN ({marks})",
            (board, *gpins)).fetchone()[0]
    return out
