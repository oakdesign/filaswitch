import logging
import re
from extruder import Extruder
from switch_tower import PEEK
from gcode import GCode
from layer import FirstLayer, ACT_INFILL, ACT_PASS, ACT_SWITCH, Layer

import utils
from gcode_file import SLICER_KISSLICER, GCodeFile

gcode = GCode()
log = logging.getLogger("KISSlicer")


class KISSlicerGCodeFile(GCodeFile):

    slicer_type = SLICER_KISSLICER

    # ; BEGIN_LAYER_OBJECT z=0.294 z_thickness=0.294
    LAYER_START_RE = re.compile(b"BEGIN_LAYER_OBJECT z=(\d+\.*\d*) z_thickness=(\d+\.*\d*)")
    VERSION_RE = re.compile(b" version (\d+)\.(\d+).*")

    def __init__(self, logger, hw_config, tower_position, purge_lines):
        super().__init__(logger, hw_config, tower_position, purge_lines)

        self.extruder_use_wipe = []
        self.extruder_wipe = []
        self.relative_e = False
        self.retract_while_wiping = False
        self.version = None

        self.default_speed = None
        self.machine_type = None
        self.stroke_x = None
        self.stroke_y = None
        self.origin_offset_x = None
        self.origin_offset_y = None

    def process(self, gcode_file):
        self.open_file(gcode_file)
        self.parse_header()
        self.parse_print_settings()
        self.filter_layers()
        self.parse_perimeter_rates()
        if len(self.tools) > 1:
            self.find_tower_position()
            self.add_switch_raft()
            self.add_tool_change_gcode()
        else:
            self.log.info("No tool changes detected, skipping tool change g-code additions")
        return self.save_new_file()

    def parse_header(self):
        """
         Parse KISS header and stuff for print settings
        :return: none
        """

        z_offset = 0
        current_tool = None

        ext_re = re.compile(b"Material Settings for Extruder (\d+)")

        for layer in self.layers:
            for cmd, comment in layer.lines:

                if cmd and gcode.is_tool_change(cmd) is not None:
                    current_extruder = gcode.last_match
                if cmd:
                    continue
                if b" version" in comment:
                    # parse version
                    try:
                        m = self.VERSION_RE.match(comment)
                        self.version = (int(m.groups()[0]), int(m.groups()[1]))
                    except Exception as e:
                        print(e)
                elif b"bed_size_x_mm =" in comment:
                    #; bed_size_x_mm = 145
                    self.stroke_x = float(comment.split(b' = ')[1])
                elif b"bed_size_y_mm =" in comment:
                    #; bed_size_y_mm = 145
                    self.stroke_y = float(comment.split(b' = ')[1])
                elif b"bed_offset_x_mm =" in comment:
                    #; bed_offset_x_mm = 72.5
                    self.origin_offset_x = float(comment.split(b' = ')[1])
                elif b"bed_offset_y_mm =" in comment:
                    #; bed_offset_y_mm = 72.5
                    self.origin_offset_y = float(comment.split(b' = ')[1])
                elif b"bed_offset_z_mm =" in comment:
                    #; bed_offset_z_mm = 0
                    z_offset = float(comment.split(b' = ')[1])
                elif b"round_bed =" in comment:
                    # ; round_bed = 0
                    self.machine_type = int(comment.split(b' = ')[1])
                elif b"travel_speed_mm_per_s =" in comment:
                    # ; travel_speed_mm_per_s = 100
                    speed = float(comment.split(b' = ')[1]) * 60
                    self.travel_xy_speed = speed
                    self.travel_z_speed = speed

                elif b"num_extruders = " in comment:
                    # ; num_extruders = 4
                    for t in range(int(comment.split(b' = ')[1])):
                        if not t in self.extruders:
                            self.extruders[t] = Extruder(t)

                elif b"first_layer_speed_mm_per_s =" in comment:
                    # ; first_layer_speed_mm_per_s = 25
                    self.first_layer_speed = float(comment.split(b' = ')[1]) * 60

                elif b"Perimeter Speed =" in comment:
                    # ; Perimeter Speed = 32.50
                    self.outer_perimeter_speed = float(comment.split(b' = ')[1]) * 60

                elif b"*** Material Settings for Extruder" in comment:
                    # ; *** Material Settings for Extruder 2 ***
                    m = ext_re.match(comment)
                    current_tool = int(m.groups()[0]) - 1

                elif current_tool and b"destring_length =" in comment:
                    # ; destring_length = 3
                    self.extruders[current_tool].retract = float(comment.split(b' = ')[1])

                elif current_tool and b"destring_speed_mm_per_s =" in comment:
                    # ; destring_speed_mm_per_s = 80
                    self.extruders[current_tool].retract_speed = float(comment.split(b' = ')[1]) * 60

                elif current_tool and b"Z_lift_mm =" in comment:
                    # ; Z_lift_mm = 0
                    self.extruders[current_tool].z_hop = float(comment.split(b' = ')[1])

                elif current_tool and b"wipe_mm =" in comment:
                    # ; wipe_mm = 5
                    self.extruders[current_tool].wipe = float(comment.split(b' = ')[1])

                elif current_tool and b"flowrate_tweak =" in comment:
                    # ; flowrate_tweak = 1
                    self.extruders[current_tool].feed_rate_multiplier = float(comment.split(b' = ')[1])

                elif current_tool and b"g_code_matl =" in comment:
                    # ; g_code_matl = NULL
                    self.extruders[current_tool].filament_type = comment.split(b' = ')[1]

                elif b"firmware_type =" in comment:
                    # ; firmware_type = 1
                    if comment.split(b' = ')[1] != b"1":
                        raise ValueError("Relative E distances not enabled! Filaswitch won't work without relative E distances")

        if not self.version:
            self.log.warning("Could not detect KISSlicer version. Use at your own risk!")
        else:
            self.log.info("Slic3r version %d.%d" % self.version)

        for t in self.extruders:
            self.extruders[t].z_offset = z_offset

    def parse_print_settings(self):
        """ KISS specific settings """

        super().parse_print_settings()

        for cmd, comment, line_index in self.layers[0].read_lines():
            # find first tool change and remove it if it's T0. No need to
            # do tool change as e already have T0 active
            if line_index > self.layers[0].start_gcode_end and cmd and gcode.is_tool_change(cmd) == 0:
                self.layers[0].delete_line(line_index)
                break

    def parse_layers(self, lines):
        """
        Go through the g-code and find layer start points.
        Store each layer to list.
        :return:
        """

        current_layer = FirstLayer(1, 0.2, 0.2)
        layer_num = 0

        for line in lines:
            cmd, comment = gcode.read_gcode_line(line)
            if comment:
                ret = self.check_layer_change(comment, None)
                if ret:
                    layer_z, layer_height = ret

                    if current_layer.num == 1 and layer_num == 0:
                        current_layer.z = layer_z
                        current_layer.height = layer_height
                        layer_num += 1
                    else:
                        self.layers.append(current_layer)
                        layer_num += 1
                        current_layer = Layer(layer_num, layer_z, layer_height)
            current_layer.add_line(cmd, comment)

        # last layer
        self.layers.append(current_layer)

    def check_layer_change(self, line, current_layer):
        """
        Check if line is layer change
        :param line: g-code line
        :param current_layer: current layer data
        :return: None or tuple of layer z and layer thickness
        """
        m = self.LAYER_START_RE.match(line)
        if m:
            return int(m.groups()[0]), float(m.groups()[1])
        return current_layer

    def filter_layers(self):
        """
        Filter layers so that only layers relevant purge tower processing
        are returned. Also layers are tagged for action (tool witch, infill, pass)
        Layers that are left out:
        - empty (no command lines)
        - non-tool
        :return: none
        """

        # maxes = [last_switch_heights[k] for k in last_switch_heights]
        # maxes.sort(reverse=True)
        # last_switch_height = maxes[1]
        # print(last_switch_heights)
        # print(last_switch_height)

        layer_data = {}

        # step 1: filter out empty layers and add populate dictionary
        for layer in self.layers:
            if layer.z not in layer_data:
                layer_data[layer.z] = {'layers': []}

            layer_data[layer.z]['layers'].append(layer)

        # z-list sorted
        zs = sorted(layer_data.keys())

        # get needed slot counts per layer by going through reversed z-position list.
        # if there are only few changes for slot, tuck them in the previous slot
        slots = 1
        zs.reverse()
        count = 0
        for z in zs:
            lrs = 0
            for l in layer_data[z]['layers']:
                # each layer counts whether there's tool changes or not
                lrs += l.has_tool_changes()
            if lrs > slots:
                count += 1
            if count >= 3:
                slots = lrs
                count = 0
            layer_data[z]['slots'] = slots

        self.max_slots = slots
        #print(self.max_slots)

        # tag layers for actions: tool change, infill, etc
        zs.reverse()
        for z in zs:

            if layer_data[z]['slots'] == 0:
                continue

            slots_filled = 0
            # first check tool change layers
            for l in layer_data[z]['layers']:
                l.tower_slots = layer_data[z]['slots']
                if l.has_tool_changes():
                    l.action = ACT_SWITCH
                    slots_filled += 1

            # then check other layers
            for l in layer_data[z]['layers']:
                if not l.has_tool_changes():
                    if slots_filled < layer_data[z]['slots']:
                        l.action = ACT_INFILL
                        slots_filled += 1
                    else:
                        l.action = ACT_PASS

        # step 3: pack groups to list
        layers = []
        for z in zs:
            for l in layer_data[z]['layers']:
                layers.append(l)
        self.filtered_layers = sorted(layers, key=lambda x: x.num)

    def parse_perimeter_rates(self):
        """
        Parses perimeter print speed and feed rate for each layer
        :return: none
        """
        last_speed = None
        last_feed_rate = None
        for layer in self.layers:
            layer.outer_perimeter_speed = self.outer_perimeter_speed
            layer.outer_perimeter_feedrate = 0.05


if __name__ == "__main__":
    import logger
    logger = logger.Logger(".")
    s = PrusaSlic3rCodeFile(logger, PEEK, 4, "Automatic")
    print(s.check_layer_change(b" layer 1, Z = 1", None))