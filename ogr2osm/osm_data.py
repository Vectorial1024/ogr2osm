# -*- coding: utf-8 -*-

'''
Copyright (c) 2012-2021 Roel Derickx, Paul Norman <penorman@mac.com>,
Sebastiaan Couwenberg <sebastic@xs4all.nl>, The University of Vermont
<andrew.guertin@uvm.edu>, github contributors

Released under the MIT license, as given in the file LICENSE, which must
accompany any distribution of this code.
'''

import logging
from osgeo import ogr
from osgeo import osr

from .osm_geometries import OsmBoundary, OsmPoint, OsmWay, OsmRelation

class OsmData:
    def __init__(self, translation, rounding_digits=7, max_points_in_way=1800, add_bounds=False):
        # options
        self.translation = translation
        self.rounding_digits = rounding_digits
        self.max_points_in_way = max_points_in_way
        self.add_bounds = add_bounds

        self.__bounds = OsmBoundary()
        self.__nodes = []
        self.__unique_node_index = {}
        self.__ways = []
        self.__relations = []


    def __get_layer_fields(self, layer):
        layer_fields = []
        layer_def = layer.GetLayerDefn()
        for i in range(layer_def.GetFieldCount()):
            field_def = layer_def.GetFieldDefn(i)
            layer_fields.append((i, field_def.GetNameRef(), field_def.GetType()))
        return layer_fields


    # This function builds up a dictionary with the source data attributes
    # and passes them to the filter_tags function, returning the result.
    def __get_feature_tags(self, ogrfeature, layer_fields, source_encoding):
        tags = {}
        for (index, field_name, field_type) in layer_fields:
            field_value = ''
            if field_type == ogr.OFTString:
                field_value = ogrfeature.GetFieldAsBinary(index).decode(source_encoding)
            else:
                field_value = ogrfeature.GetFieldAsString(index)

            tags[field_name] = field_value.strip()

        return self.translation.filter_tags(tags)


    def __calc_bounds(self, ogrgeometry):
        (minx, maxx, miny, maxy) = ogrgeometry.GetEnvelope()
        self.__bounds.add_envelope(minx, maxx, miny, maxy)


    def __round_number(self, n):
        return int(round(n * 10**self.rounding_digits))


    def __add_node(self, x, y, tags, is_way_member):
        rx = self.__round_number(x)
        ry = self.__round_number(y)

        unique_node_id = None
        if is_way_member:
            unique_node_id = (rx, ry)
        else:
            # TODO deprecated, to be removed
            unique_node_id = self.translation.get_unique_node_identifier(rx, ry, tags)

        if unique_node_id in self.__unique_node_index:
            for index in self.__unique_node_index[unique_node_id]:
                duplicate_node = self.__nodes[index]
                merged_tags = self.translation.merge_tags('node', duplicate_node.tags, tags)
                if merged_tags is not None:
                    duplicate_node.tags = merged_tags
                    return duplicate_node

            node = OsmPoint(x, y, tags)
            self.__unique_node_index[unique_node_id].append(len(self.__nodes))
            self.__nodes.append(node)
            return node
        else:
            node = OsmPoint(x, y, tags)
            self.__unique_node_index[unique_node_id] = [ len(self.__nodes) ]
            self.__nodes.append(node)
            return node


    def __add_way(self, tags):
        way = OsmWay(tags)
        self.__ways.append(way)
        return way


    def __add_relation(self, tags):
        relation = OsmRelation(tags)
        self.__relations.append(relation)
        return relation


    def __parse_point(self, ogrgeometry, tags):
        return self.__add_node(ogrgeometry.GetX(), ogrgeometry.GetY(), tags, False)


    def __get_ordered_nodes(self, nodes):
        is_closed = len(nodes) > 2 and nodes[0].id == nodes[-1].id
        if is_closed:
            lowest_index = 0
            lowest_node_id = nodes[0].id
            for i in range(1, len(nodes)):
                if nodes[i].id < lowest_node_id:
                    lowest_node_id = nodes[i].id
                    lowest_index = i

            return nodes[lowest_index:-1] + nodes[:lowest_index+1]
        else:
            return nodes


    def __verify_duplicate_ways(self, potential_duplicate_ways, nodes):
        duplicate_ways = []
        ordered_nodes = self.__get_ordered_nodes(nodes)
        for dupway in potential_duplicate_ways:
            if len(dupway.nodes) == len(nodes):
                dupnodes = self.__get_ordered_nodes(dupway.nodes)
                if dupnodes == ordered_nodes:
                    duplicate_ways.append((dupway, 'way'))
                elif dupnodes == list(reversed(ordered_nodes)):
                    duplicate_ways.append((dupway, 'reverse_way'))
        return duplicate_ways


    def __parse_linestring(self, ogrgeometry, tags):
        previous_node_id = None
        nodes = []
        potential_duplicate_ways = []
        for i in range(ogrgeometry.GetPointCount()):
            (x, y, z_unused) = ogrgeometry.GetPoint(i)
            node = self.__add_node(x, y, {}, True)
            if previous_node_id is None or previous_node_id != node.id:
                if previous_node_id is None:
                    # first node: add all parent ways as potential duplicates
                    potential_duplicate_ways = [ p for p in node.get_parents() if type(p) == OsmWay ]
                elif not any(node.get_parents()) and any(potential_duplicate_ways):
                    # next nodes: if node doesn't belong to another way then this way is unique
                    potential_duplicate_ways.clear()
                nodes.append(node)
                previous_node_id = node.id

        duplicate_ways = self.__verify_duplicate_ways(potential_duplicate_ways, nodes)

        for duplicate_way in duplicate_ways:
            merged_tags = self.translation.merge_tags(duplicate_way[1], duplicate_way[0].tags, tags)
            if merged_tags is not None:
                duplicate_way[0].tags = merged_tags
                return duplicate_way[0]

        way = self.__add_way(tags)
        way.nodes = nodes
        for node in nodes:
            node.addparent(way)
        return way


    def __verify_duplicate_relations(self, potential_duplicate_relations, members):
        duplicate_relations = []
        for duprelation in potential_duplicate_relations:
            if duprelation.members == members:
                duplicate_relations.append(duprelation)
        return duplicate_relations


    def __parse_polygon(self, ogrgeometry, tags):
        # Special case polygons with only one ring. This does not (or at least
        # should not) change behavior when simplify relations is turned on.
        if ogrgeometry.GetGeometryCount() == 0:
            logging.warning("Polygon with no rings?")
            return None
        elif ogrgeometry.GetGeometryCount() == 1 and \
             ogrgeometry.GetGeometryRef(0).GetPointCount() <= self.max_points_in_way:
            # only 1 linestring which is not too long: no relation required
            result = self.__parse_linestring(ogrgeometry.GetGeometryRef(0), tags)
            return result
        else:
            members = []
            potential_duplicate_relations = []

            # exterior ring
            exterior_geom_type = ogrgeometry.GetGeometryRef(0).GetGeometryType()
            if exterior_geom_type in [ ogr.wkbLineString, ogr.wkbLinearRing, ogr.wkbLineString25D ]:
                exterior = self.__parse_linestring(ogrgeometry.GetGeometryRef(0), {})
                members.append((exterior, "outer"))
                # first member: add all parent relations as potential duplicates
                potential_duplicate_relations = \
                    [ p for p in exterior.get_parents() \
                        if type(p) == OsmRelation and p.get_member_role(exterior) == "outer" ]
            else:
                logging.warning("Polygon with no exterior ring?")
                return None

            # interior rings
            for i in range(1, ogrgeometry.GetGeometryCount()):
                interior = self.__parse_linestring(ogrgeometry.GetGeometryRef(i), {})
                members.append((interior, "inner"))
                if not any(interior.get_parents()) and any(potential_duplicate_relations):
                    # next members: if interior doesn't belong to another relation then this
                    #               relation is unique
                    potential_duplicate_relations.clear()

            duplicate_relations = \
                self.__verify_duplicate_relations(potential_duplicate_relations, members)

            for duplicate_relation in duplicate_relations:
                merged_tags = self.translation.merge_tags('relation', duplicate_relation.tags, tags)
                if merged_tags is not None:
                    duplicate_relation.tags = merged_tags
                    return duplicate_relation

            relation = self.__add_relation(tags)
            for m in members:
                m[0].addparent(relation)
            relation.members = members
            return relation


    def __parse_collection(self, ogrgeometry, tags):
        # OGR MultiPolygon maps easily to osm multipolygon, so special case it
        # TODO: Does anything else need special casing?
        geometry_type = ogrgeometry.GetGeometryType()
        if geometry_type in [ ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D ]:
            if ogrgeometry.GetGeometryCount() > 1:
                relation = self.__add_relation(tags)
                for polygon in range(ogrgeometry.GetGeometryCount()):
                    ext_geom = ogrgeometry.GetGeometryRef(polygon).GetGeometryRef(0)
                    exterior = self.__parse_linestring(ext_geom, {})
                    exterior.addparent(relation)
                    relation.members.append((exterior, "outer"))
                    for i in range(1, ogrgeometry.GetGeometryRef(polygon).GetGeometryCount()):
                        int_geom = ogrgeometry.GetGeometryRef(polygon).GetGeometryRef(i)
                        interior = self.__parse_linestring(int_geom, {})
                        interior.addparent(relation)
                        relation.members.append((interior, "inner"))
                return [ relation ]
            else:
                return [ self.__parse_polygon(ogrgeometry.GetGeometryRef(0), tags) ]
        elif geometry_type in [ ogr.wkbMultiLineString, ogr.wkbMultiLineString25D ]:
            geometries = []
            for linestring in range(ogrgeometry.GetGeometryCount()):
                geometries.append(self.__parse_linestring(ogrgeometry.GetGeometryRef(linestring), tags))
            return geometries
        else:
            relation = self.__add_relation(tags)
            for i in range(ogrgeometry.GetGeometryCount()):
                member = self.__parse_geometry(ogrgeometry.GetGeometryRef(i), {})
                member.addparent(relation)
                relation.members.append((member, "member"))
            return [ relation ]


    def __parse_geometry(self, ogrgeometry, tags):
        osmgeometries = []

        geometry_type = ogrgeometry.GetGeometryType()

        if geometry_type in [ ogr.wkbPoint, ogr.wkbPoint25D ]:
            osmgeometries.append(self.__parse_point(ogrgeometry, tags))
        elif geometry_type in [ ogr.wkbLineString, ogr.wkbLinearRing, ogr.wkbLineString25D ]:
            # ogr.wkbLinearRing25D does not exist
            osmgeometries.append(self.__parse_linestring(ogrgeometry, tags))
        elif geometry_type in [ ogr.wkbPolygon, ogr.wkbPolygon25D ]:
            osmgeometries.append(self.__parse_polygon(ogrgeometry, tags))
        elif geometry_type in [ ogr.wkbMultiPoint, ogr.wkbMultiLineString, ogr.wkbMultiPolygon, \
                                ogr.wkbGeometryCollection, ogr.wkbMultiPoint25D, \
                                ogr.wkbMultiLineString25D, ogr.wkbMultiPolygon25D, \
                                ogr.wkbGeometryCollection25D ]:
            osmgeometries.extend(self.__parse_collection(ogrgeometry, tags))
        else:
            logging.warning("Unhandled geometry, type %s", str(geometry_type))

        return osmgeometries


    def add_feature(self, ogrfeature, layer_fields, source_encoding, reproject = lambda geometry: None):
        ogrfilteredfeature = self.translation.filter_feature(ogrfeature, layer_fields, reproject)
        if ogrfilteredfeature is None:
            return

        ogrgeometry = ogrfilteredfeature.GetGeometryRef()
        if ogrgeometry is None:
            return

        feature_tags = self.__get_feature_tags(ogrfilteredfeature, layer_fields, source_encoding)
        if feature_tags is None:
            return

        reproject(ogrgeometry)

        if self.add_bounds:
            self.__calc_bounds(ogrgeometry)

        osmgeometries = self.__parse_geometry(ogrgeometry, feature_tags)

        # TODO performance: run in __parse_geometry to avoid second loop
        for osmgeometry in [ geom for geom in osmgeometries if geom ]:
            self.translation.process_feature_post(osmgeometry, ogrfilteredfeature, ogrgeometry)


    def __split_way(self, way):
        new_nodes = [ way.nodes[i:i + self.max_points_in_way] \
                               for i in range(0, len(way.nodes), self.max_points_in_way - 1) ]
        new_ways = [ way ] + [ OsmWay(way.tags) for i in range(len(new_nodes) - 1) ]

        for new_way, nodes in zip(new_ways, new_nodes):
            new_way.nodes = nodes
            if new_way.id != way.id:
                self.__ways.append(new_way)
                for node in nodes:
                    node.removeparent(way)
                    node.addparent(new_way)

        return new_ways


    def __split_way_in_relation(self, rel, way_parts):
        way_role = rel.get_member_role(way_parts[0])
        for way in way_parts[1:]:
            way.addparent(rel)
            rel.members.append((way, way_role))


    def split_long_ways(self):
        if self.max_points_in_way < 2:
            # pointless :-)
            return

        logging.debug("Splitting long ways")

        for way in self.__ways:
            if len(way.nodes) > self.max_points_in_way:
                way_parts = self.__split_way(way)
                for rel in way.get_parents():
                    self.__split_way_in_relation(rel, way_parts)


    def process(self, datasource):
        for i in range(datasource.get_layer_count()):
            (layer, reproject) = datasource.get_layer(i)

            if layer:
                layer_fields = self.__get_layer_fields(layer)
                for j in range(layer.GetFeatureCount()):
                    ogrfeature = layer.GetNextFeature()
                    self.add_feature(ogrfeature, layer_fields, datasource.source_encoding, reproject)

        self.split_long_ways()


    class DataWriterContextManager:
        def __init__(self, datawriter):
            self.datawriter = datawriter

        def __enter__(self):
            self.datawriter.open()
            return self.datawriter

        def __exit__(self, exception_type, value, traceback):
            self.datawriter.close()


    def output(self, datawriter):
        self.translation.process_output(self.__nodes, self.__ways, self.__relations)

        with self.DataWriterContextManager(datawriter) as dw:
            dw.write_header(self.__bounds)
            dw.write_nodes(self.__nodes)
            dw.write_ways(self.__ways)
            dw.write_relations(self.__relations)
            dw.write_footer()
