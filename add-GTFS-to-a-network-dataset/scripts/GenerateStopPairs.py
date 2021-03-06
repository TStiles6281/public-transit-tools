################################################################################
## Toolbox: Add GTFS to a Network Dataset
## Tool name: 1) Generate Transit Lines and Stops
## Created by: Melinda Morang, Esri, mmorang@esri.com
## Last updated: 21 October 2017
################################################################################
''' This tool generates feature classes of transit stops and lines from the
information in the GTFS dataset.  The stop locations are taken directly from the
lat/lon coordinates in the GTFS data.  A straight line is generated connecting
each pair of adjacent stops in the network (ie, stops directly connected by at
least one transit trip in the GTFS data with no other stops in between). When
multiple trips or routes travel directly between the same two stops, only one
line is generated unless the routes have different mode types.  This tool also
generates a SQL database version of the GTFS data which is used by the network
dataset for schedule lookups.'''
################################################################################
'''Copyright 2017 Esri
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at
       http://www.apache.org/licenses/LICENSE-2.0
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.'''
################################################################################

import sqlite3, os, operator, itertools, csv, re
import arcpy
import sqlize_csv, hms

class CustomError(Exception):
    pass

# ----- Collect user inputs -----

# GTFS directories
inGTFSdir = arcpy.GetParameterAsText(0)
# Feature dataset where the network will be built
outFD = arcpy.GetParameterAsText(1)

# Derived inputs
outGDB = os.path.dirname(outFD)
SQLDbaseName = "GTFS.sql"
SQLDbase = os.path.join(outGDB, SQLDbaseName)
outStopPairsFCName = "StopPairs"
outStopPairsFC = os.path.join(outGDB, outStopPairsFCName)
outLinesFC = os.path.join(outFD, "TransitLines")
outStopsFCName = "Stops"
outStopsFC = os.path.join(outFD, outStopsFCName)

# Get the original overwrite output setting so we can reset it at the end.
OverwriteOutput = arcpy.env.overwriteOutput
# It's okay to overwrite stuff in this tool
arcpy.env.overwriteOutput = True

# GTFS stop lat/lon are written in WGS1984 coordinates
WGSCoords = "GEOGCS['GCS_WGS_1984',DATUM['D_WGS_1984', \
SPHEROID['WGS_1984',6378137.0,298.257223563]], \
PRIMEM['Greenwich',0.0],UNIT['Degree',0.0174532925199433]]; \
-400 -400 1000000000;-100000 10000;-100000 10000; \
8.98315284119522E-09;0.001;0.001;IsHighPrecision"
# Output files must be written in the coordinate system of the output FD.
outFD_SR = arcpy.Describe(outFD).spatialReference

# GTFS route_type information
#0 - Tram, Streetcar, Light rail. Any light rail or street level system within a metropolitan area.
#1 - Subway, Metro. Any underground rail system within a metropolitan area.
#2 - Rail. Used for intercity or long-distance travel.
#3 - Bus. Used for short- and long-distance bus routes.
#4 - Ferry. Used for short- and long-distance boat service.
#5 - Cable car. Used for street-level cable cars where the cable runs beneath the car.
#6 - Gondola, Suspended cable car. Typically used for aerial cable cars where the car is suspended from the cable.
#7 - Funicular. Any rail system designed for steep inclines.
route_type_dict = {0: "Tram, Streetcar, Light rail",
                    1: "Subway, Metro",
                    2: "Rail",
                    3: "Bus",
                    4: "Ferry",
                    5: "Cable car",
                    6: "Gondola, Suspended cable car",
                    7: "Funicular"}

try:

# ----- SQLize the GTFS data -----

    arcpy.AddMessage("SQLizing the GTFS data...")
    arcpy.AddMessage("(This will take a few minutes for large datasets.)")

    # Fix up list of GTFS datasets (it comes in as a ;-separated list)
    inGTFSdirList = inGTFSdir.split(";")
    # Remove single quotes ArcGIS puts in if there are spaces in the filename.
    for d in inGTFSdirList:
        if d[0] == "'" and d[-1] == "'":
            loc = inGTFSdirList.index(d)
            inGTFSdirList[loc] = d[1:-1]

    # The main SQLizing work is done in the sqlize_csv module
    # Connect to or create the SQL file.
    sqlize_csv.connect(SQLDbase)
    # Create tables.
    for tblname in sqlize_csv.sql_schema:
        sqlize_csv.create_table(tblname)
    # SQLize all the GTFS files, for each separate GTFS dataset.
    for gtfs_dir in inGTFSdirList:
        # Run sqlize for each GTFS dataset. Check for returned errors
        GTFSErrors = sqlize_csv.handle_agency(gtfs_dir)
        if GTFSErrors:
            for error in GTFSErrors:
                arcpy.AddError(error)
            raise CustomError

    # Create indices to make queries faster.
    sqlize_csv.create_indices()

    # Check for non-overlapping date ranges to prevent double-counting.
    overlapwarning = sqlize_csv.check_nonoverlapping_dateranges()
    if overlapwarning:
        arcpy.AddWarning(overlapwarning)


# ----- Connect to SQL locally for further queries and entries -----

    # Connect to the SQL database
    conn = sqlize_csv.db #sqlite3.connect(SQLDbase)
    c = conn.cursor()


# ----- Make dictionary of route types -----

    # Find all routes and associated info.
    RouteDict = {}
    routesfetch = '''
        SELECT route_id, route_type
        FROM routes
        ;'''
    c.execute(routesfetch)
    for route in c:
        RouteDict[route[0]] = route[1]


# ----- Make dictionary of {trip_id: route_type} -----

    # First, make sure there are no duplicate trip_id values, as this will mess things up later.
    tripDuplicateFetch = "SELECT trip_id, count(*) from trips group by trip_id having count(*) > 1"
    c.execute(tripDuplicateFetch)
    tripdups = c.fetchall()
    tripdupslist = [tripdup for tripdup in tripdups]
    if tripdupslist:
        arcpy.AddError("Your GTFS trips table is invalid.  It contains multiple trips with the same trip_id.")
        for tripdup in tripdupslist:
            arcpy.AddError("There are %s instances of the trip_id value '%s'." % (str(tripdup[1]), unicode(tripdup[0])))
        raise CustomError

    # Now make the dictionary
    trip_routetype_dict = {}
    tripsfetch = '''
        SELECT trip_id, route_id
        FROM trips
        ;'''
    c.execute(tripsfetch)
    for trip in c:
        try:
            trip_routetype_dict[trip[0]] = RouteDict[trip[1]]
        except KeyError:
            arcpy.AddWarning("Trip_id %s in trips.txt has a route_id value, %s, which does not appear in your routes.txt file.  \
This trip can still be used for analysis, but it might be an indication of a problem with your GTFS dataset." % (trip[0], trip[1]))
            trip_routetype_dict[trip[0]] = 100 # 100 is an arbitrary number that doesn't match anything in the GTFS spec


# ----- Make dictionary of frequency information (if there is any) -----

    frequencies_dict = {}
    freqfetch = '''
        SELECT trip_id, start_time, end_time, headway_secs
        FROM frequencies
        ;'''
    c.execute(freqfetch)
    for freq in c:
        trip_id = freq[0]
        if freq[3] == 0:
            arcpy.AddWarning("Trip_id %s in your frequencies.txt file has a headway of 0 seconds. \
This is invalid, so trips with this id will not be included in your network." % trip_id)
            continue
        trip_data = [freq[1], freq[2], freq[3]]
        # {trip_id: [start_time, end_time, headway_secs]}
        frequencies_dict.setdefault(trip_id, []).append(trip_data)


# ----- Generate transit stops feature class (for the final ND) -----

    arcpy.AddMessage("Generating transit stops feature class.")

    # Find parent stations that are actually used
    selectparentstationsstmt = "SELECT parent_station FROM stops WHERE location_type='0' AND parent_station <> ''"
    c.execute(selectparentstationsstmt)
    used_parent_stations = list(set([station[0] for station in c]))

    # Get the combined stops table.
    selectstoptablestmt = "SELECT stop_id, stop_lat, stop_lon, stop_code, \
                        stop_name, stop_desc, zone_id, stop_url, location_type, \
                        parent_station, wheelchair_boarding FROM stops;"
    c.execute(selectstoptablestmt)

    # Initialize a dictionary of stop lat/lon (filled below)
    # {stop_id: <stop geometry object>} in the output coordinate system
    stoplatlon_dict = {}

    # Create a points feature class for the point pairs.
    text_field_length = 500
    arcpy.CreateFeatureclass_management(outFD, outStopsFCName, "POINT", "", "", "", outFD_SR)
    arcpy.management.AddField(outStopsFC, "stop_id", "TEXT")
    arcpy.management.AddField(outStopsFC, "stop_code", "TEXT")
    arcpy.management.AddField(outStopsFC, "stop_name", "TEXT")
    arcpy.management.AddField(outStopsFC, "stop_desc", "TEXT", field_length=text_field_length)
    arcpy.management.AddField(outStopsFC, "zone_id", "TEXT")
    arcpy.management.AddField(outStopsFC, "stop_url", "TEXT")
    arcpy.management.AddField(outStopsFC, "location_type", "TEXT")
    arcpy.management.AddField(outStopsFC, "parent_station", "TEXT")
    arcpy.management.AddField(outStopsFC, "wheelchair_boarding", "TEXT")

    # Add the stops table to a feature class.
    with arcpy.da.InsertCursor(outStopsFC, ["SHAPE@", "stop_id",
                                                 "stop_code", "stop_name", "stop_desc",
                                                 "zone_id", "stop_url", "location_type",
                                                 "parent_station", "wheelchair_boarding"]) as cur3:
        for stop in c:
            stop_id = stop[0]
            stop_lat = stop[1]
            stop_lon = stop[2]
            stop_code = stop[3]
            stop_name = stop[4]
            stop_desc = stop[5][:text_field_length]
            zone_id = stop[6]
            stop_url = stop[7]
            location_type = stop[8]
            parent_station = stop[9]
            wheelchair_boarding = unicode(stop[10])
            if location_type == 1 and stop_id not in used_parent_stations:
                # Skip this stop because it's an unused parent station
                # since these will just make useless standalone junctions.
                continue
            if location_type == 2 and parent_station not in used_parent_stations:
                # Remove station entrances that don't have a valid parent_station
                # since these serve no purpose
                continue
            pt = arcpy.Point()
            pt.X = float(stop_lon)
            pt.Y = float(stop_lat)
            # GTFS stop lat/lon is written in WGS1984
            ptGeometry = arcpy.PointGeometry(pt, WGSCoords)
            # But the stops fc must be in the user's FD coordinate system
            ptGeometry_projected = ptGeometry.projectAs(outFD_SR)
            stoplatlon_dict[stop_id] = ptGeometry_projected
            cur3.insertRow((ptGeometry_projected, stop_id, stop_code, stop_name,
                            stop_desc, zone_id, stop_url, location_type,
                            parent_station, wheelchair_boarding))


# ----- Obtain schedule info from the stop_times.txt file and convert it to a line-based model -----

    arcpy.AddMessage("Obtaining and processing transit schedule and line information...")
    arcpy.AddMessage("(This will take a few minutes for large datasets.)")

    def Make_Frequency_Rows(trip_id, stop_times):
        '''If the trip uses the frequencies.txt file, extrapolate the stop_times
        throughout the day using the relative time between the stops given in
        stop_times and the headways listed in frequencies. Construct rows of 
        (SourceOIDkey, start_time, end_time, trip_id) to insert into schedule table'''

        if len(stop_times) < 2: # No complete stop-stop segments for this trip
            return []

        global linefeature_dict
        route_type = trip_routetype_dict[trip_id]

        stop_times_current_trip = []
        first_trip_initial_start_time = stop_times[0][2] # First start time of trip is departure_time of first stop
        previous_stop = stop_times[0][0] # Initialize as the first stop
        start_time = stop_times[0][2] # Initialize as the departure_time of first stop
        # Loop over stop_times entries for this trip and convert to a line-based model
        for st in stop_times[1:]:
            stop_id = st[0]
            arrival_time = st[1]
            departure_time = st[2]
            start_stop = previous_stop
            end_stop = stop_id
            start_time_along_trip = start_time - first_trip_initial_start_time # Start time of line segment is departure time of first stop
            end_time_along_trip = arrival_time - first_trip_initial_start_time # End time of line segment is arrival time at second stop
            SourceOIDkey = "%s , %s , %s" % (start_stop, end_stop, route_type)
            linefeature_dict[SourceOIDkey] = True
            # Loop over all time windows in frequencies.txt for this trip
            for window in frequencies_dict[trip_id]: # {trip_id: [start_time, end_time, headway_secs]}
                start_timeofday = window[0]
                end_timeofday = window[1]
                headway = window[2]
                # Loop over the range of times based on headway
                for i in range(int(round(start_timeofday, 0)), int(round(end_timeofday, 0)), headway):
                    start_time_extrapolated = i + start_time_along_trip # current trip initial start time + time along trip
                    end_time_extrapolated = i + end_time_along_trip # current trip initial start time + time along trip
                    stop_times_current_trip.append((SourceOIDkey, start_time_extrapolated, end_time_extrapolated, trip_id))
            previous_stop = stop_id # Increment previous_stop
            start_time = departure_time # Reset start_time to current stop's departure_time

        return stop_times_current_trip

    def Make_StopsTimes_Rows(trip_id, stop_times):
        '''Using values from stop_times for a particular trip, construct rows of 
        (SourceOIDkey, start_time, end_time, trip_id) to insert into schedule table'''
        
        if len(stop_times) < 2: # No complete stop-stop segments for this trip
            return []

        global linefeature_dict
        route_type = trip_routetype_dict[trip_id]

        stop_times_current_trip = []
        current_trip_initial_start_time = stop_times[0][2] # First start time of trip is departure_time of first stop
        previous_stop = stop_times[0][0] # Initialize as the first stop
        start_time = stop_times[0][2] # Initialize as the departure_time of first stop
        # Loop over stop_times entries for this trip and convert to a line-based model
        for st in stop_times[1:]:
            stop_id = st[0]
            arrival_time = st[1]
            departure_time = st[2]
            start_stop = previous_stop
            end_stop = stop_id
            end_time = arrival_time # End time of line segment is arrival time at second stop
            SourceOIDkey = "%s , %s , %s" % (start_stop, end_stop, route_type)
            linefeature_dict[SourceOIDkey] = True
            stop_times_current_trip.append((SourceOIDkey, start_time, end_time, trip_id))
            previous_stop = stop_id # Increment previous_stop
            start_time = departure_time # Reset start_time to current stop's departure_time

        return stop_times_current_trip

    def Insert_Schedules(rows):
        '''Insert into schedules table'''
        c2 = conn.cursor()
        columns = ["SourceOIDKey", "start_time", "end_time", "trip_id"]
        values_placeholders = ["?"] * len(columns)
        c2.executemany("INSERT INTO schedules (%s) VALUES (%s);" %
                        (",".join(columns), ",".join(values_placeholders)), rows)

    def Make_Rows_For_Trip(trip_id):
        '''Find pairs of directly-connected stops for this trip and prepare to insert in schedule table'''

        stoptimefetch = '''
        SELECT stop_id, arrival_time, departure_time
        FROM stop_times
        WHERE trip_id = '%s'
        ORDER BY stop_sequence
        ;''' % trip_id
        c.execute(stoptimefetch)
        stop_time_data = c.fetchall()
        if trip_id in frequencies_dict:
            stop_times_current_trip = Make_Frequency_Rows(trip_id, stop_time_data)
        else:
            stop_times_current_trip = Make_StopsTimes_Rows(trip_id, stop_time_data)
        return stop_times_current_trip


    global linefeature_dict
    linefeature_dict = {}
    # Insert the trip schedules into the table
    rows = itertools.chain.from_iterable(itertools.imap(Make_Rows_For_Trip, trip_routetype_dict.keys()))
    Insert_Schedules(rows)
    conn.commit()

    # Delete stop_times table because it's huge and we're done with it.
    c2 = conn.cursor()
    c2.execute("DROP TABLE stop_times;")
    conn.commit()


# ----- Write pairs to a points feature class (this is intermediate and will NOT go into the final ND) -----

    # Create a points feature class for the point pairs.
    arcpy.management.CreateFeatureclass(outGDB, outStopPairsFCName, "POINT", "", "", "", outFD_SR)
    arcpy.management.AddField(outStopPairsFC, "stop_id", "TEXT")
    arcpy.management.AddField(outStopPairsFC, "pair_id", "TEXT")
    arcpy.management.AddField(outStopPairsFC, "sequence", "SHORT")

    # Add pairs of stops to the feature class in preparation for generating line features
    badStops = []
    badkeys = []
    with arcpy.da.InsertCursor(outStopPairsFC, ["SHAPE@", "stop_id", "pair_id", "sequence"]) as cur:
        # linefeature_dict = {"start_stop , end_stop , route_type": True}
        for SourceOIDkey in linefeature_dict:
            stopPair = SourceOIDkey.split(" , ")
            # {stop_id: [stop_lat, stop_lon]}
            try:
                stop1 = stopPair[0]
                stop1_geom = stoplatlon_dict[stop1]
            except KeyError:
                badStops.append(stop1)
                badkeys.append(SourceOIDkey)
                continue
            try:
                stop2 = stopPair[1]
                stop2_geom = stoplatlon_dict[stop2]
            except KeyError:
                badStops.append(stop2)
                badkeys.append(SourceOIDkey)
                continue
            cur.insertRow((stop1_geom, stop1, SourceOIDkey, 1))
            cur.insertRow((stop2_geom, stop2, SourceOIDkey, 2))

    if badStops:
        badStops = list(set(badStops))
        arcpy.AddWarning("Your stop_times.txt lists times for the following \
stops which are not included in your stops.txt file. Schedule information for \
these stops will be ignored. " + unicode(badStops))

    # Remove these entries from the linefeatures dictionary so it doesn't cause false records later
    if badkeys:
        badkeys = list(set(badkeys))
        for key in badkeys:
            del linefeature_dict[key]


# ----- Generate lines between all stops (for the final ND) -----

    arcpy.management.PointsToLine(outStopPairsFC, outLinesFC, "pair_id", "sequence")
    arcpy.management.AddField(outLinesFC, "route_type", "SHORT")
    arcpy.management.AddField(outLinesFC, "route_type_text", "TEXT")

    # We don't need the points for anything anymore, so delete them.
    arcpy.Delete_management(outStopPairsFC)

    # Clean up lines with 0 length.  They will just produce build errors and
    # are not valuable for the network dataset in any other way.
    expression = """"Shape_Length" = 0"""
    with arcpy.da.UpdateCursor(outLinesFC, ["pair_id"], expression) as cur2:
        for row in cur2:
            del linefeature_dict[row[0]]
            cur2.deleteRow()

    # Insert the route type into the output lines
    with arcpy.da.UpdateCursor(outLinesFC, ["pair_id", "route_type", "route_type_text", "OID@"]) as cur4:
        # StopPairs: {pairID: [firstStop_id, secondStop_id, route_type]}
        for row in cur4:
            pair_id_list = row[0].split(" , ")
            try:
                route_type = int(pair_id_list[2])
            except ValueError:
                # The route_type has an invalid non-integer value.  If that's the case, just leave it as a string for now.
                route_type = pair_id_list[2]
            # While we're at it, add the line's ObjectID value to the linefeature_dict dictionary
            linefeature_dict[row[0]] = long(row[3])
            try:
                route_type_text = route_type_dict[route_type]
            except KeyError: # The user's data isn't a standard type from the GTFS spec
                route_type_text = "Other / Type not specified (%s)" % unicode(route_type)
            if not isinstance(route_type, int):
                row[1] = None
            else:
                row[1] = route_type
            row[2] = route_type_text
            cur4.updateRow(row)


# ----- Add transit line feature information to the SQL database -----

    def retrieve_linefeatures_info(in_key):
        '''Creates the correct rows for insertion into the linefeatures table.'''
        SourceOID = linefeature_dict[in_key]
        pair_id_list = in_key.split(" , ")
        from_stop = pair_id_list[0]
        to_stop = pair_id_list[1]
        try:
            route_type = int(pair_id_list[2])
        except ValueError:
            # The route_type field has an invalid non-integer value, so just set it to a dummy value
            route_type = "NULL"
        out_row = (SourceOID, from_stop, to_stop, route_type)
        return out_row

    # Convert the dictionary into rows appropriately formatted for insertion into the SQL table
    rows = itertools.imap(retrieve_linefeatures_info, linefeature_dict.keys())

    # Add the rows to the SQL table
    columns = ["SourceOID", "from_stop", "to_stop", "route_type"]
    values_placeholders = ["?"] * len(columns)
    c.executemany("INSERT INTO linefeatures (%s) VALUES (%s);" %
                        (",".join(columns),
                        ",".join(values_placeholders))
                        , rows)
    conn.commit()

    # Index the new table for fast lookups later (particularly in GetEIDs)
    c.execute("CREATE INDEX linefeatures_index_SourceOID ON linefeatures (SourceOID);")
    conn.commit()


# ----- Add the TransitLines feature class OID values to the schedules table for future reference -----

    conn.create_function("getSourceOID", 1, lambda v: linefeature_dict[v] if v in linefeature_dict else -1)
    c.execute("UPDATE schedules SET SourceOID = getSourceOID(SourceOIDKey)")
    conn.commit()


# ----- Finish up. -----

    # Clean up
    conn.close()
    # We don't need the points for anything anymore, so delete them.
    # Delete the pair_id column from TransitLines since it's no longer needed
    arcpy.management.DeleteField(outLinesFC, "pair_id")

    arcpy.AddMessage("Finished!")
    arcpy.AddMessage("Your SQL table of GTFS data is:")
    arcpy.AddMessage("- " + SQLDbase)
    arcpy.AddMessage("Your transit stops feature class is:")
    arcpy.AddMessage("- " + outStopsFC)
    arcpy.AddMessage("Your transit lines feature class is:")
    arcpy.AddMessage("- " + outLinesFC)

except CustomError:
    arcpy.AddError("Failed to generate transit lines and stops.")
    pass

except:
    arcpy.AddError("Failed to generate transit lines and stops.")
    raise

finally:
    # Reset the overwrite output to the user's original setting..
    arcpy.env.overwriteOutput = OverwriteOutput