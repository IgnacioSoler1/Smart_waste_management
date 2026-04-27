-- Truck profile — SmartWaste MVD
--
-- Basado en car.lua de la imagen osrm/osrm-backend:latest (api_version = 4).
-- Solo se modifican los valores del setup(); process_way/process_node/process_turn
-- son idénticos a car.lua porque los WayHandlers leen todo desde el profile.
--
-- Características del camión de referencia (recolector lateral, Montevideo):
--   Peso:    ~20 t cargado  → vehicle_weight = 20000 kg
--   Alto:    ~4.0 m         → vehicle_height = 4.0 m
--   Ancho:   ~2.5 m         → vehicle_width  = 2.5 m
--   Largo:   ~9.5 m         → vehicle_length = 9.5 m
--
-- Cambios respecto a car.lua:
--   1. vehicle_* → dimensiones y peso reales del camión
--   2. access_tags_hierarchy → agrega 'hgv' y 'goods' con mayor prioridad
--   3. access_tag_blacklist → quita 'delivery' (la recolección es un servicio de entrega)
--   4. restricted_access_tag_list → quita 'delivery' (ídem)
--   5. restrictions → agrega 'hgv', 'goods'
--   6. speeds → reducidas para camión (~25 km/h urbano vs ~50 km/h coche)
--   7. weight_name = 'duration' → optimización pura por tiempo sin bonus de accesibilidad

api_version = 4

Set = require('lib/set')
Sequence = require('lib/sequence')
Handlers = require("lib/way_handlers")
Relations = require("lib/relations")
find_access_tag = require("lib/access").find_access_tag
limit = require("lib/maxspeed").limit
Utils = require("lib/utils")
Measure = require("lib/measure")

function setup()
  return {
    properties = {
      max_speed_for_map_matching      = 90/3.6, -- 90 km/h -> m/s (camiones no van a 180)
      weight_name                     = 'duration', -- optimización pura por tiempo
      process_call_tagless_node       = false,
      u_turn_penalty                  = 20,
      continue_straight_at_waypoint   = true,
      use_turn_restrictions           = true,
      left_hand_driving               = false,
      traffic_light_penalty           = 2,
    },

    default_mode              = mode.driving,
    default_speed             = 10,
    oneway_handling           = true,
    side_road_multiplier      = 0.8,
    turn_penalty              = 7.5,
    speed_reduction           = 0.8,
    turn_bias                 = 1.075,
    cardinal_directions       = false,

    -- [TRUCK] Dimensiones reales del camión recolector lateral
    vehicle_height = 4.0,   -- metros (4.0 m con mecanismo de elevación)
    vehicle_width  = 2.5,   -- metros
    vehicle_length = 9.5,   -- metros
    vehicle_weight = 20000, -- kilogramos (20 toneladas cargado)

    suffix_list = {
      'N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW', 'North', 'South', 'West', 'East', 'Nor', 'Sou', 'We', 'Ea'
    },

    barrier_whitelist = Set {
      'cattle_grid',
      'border_control',
      'toll_booth',
      'sally_port',
      'gate',
      'lift_gate',
      'no',
      'entrance',
      'height_restrictor',
      'arch'
    },

    access_tag_whitelist = Set {
      'yes',
      'motorcar',
      'motor_vehicle',
      'vehicle',
      'permissive',
      'designated',
      'delivery',  -- [TRUCK] la recolección de residuos es un servicio de recogida
      'hov'
    },

    -- [TRUCK] 'delivery' eliminado: los camiones de recolección DEBEN poder
    -- acceder a vías marcadas hgv=delivery o access=delivery.
    access_tag_blacklist = Set {
      'no',
      'agricultural',
      'forestry',
      'emergency',
      'psv',
      'customers',
      'private',
      'destination'
    },

    service_access_tag_blacklist = Set {
      'private'
    },

    -- [TRUCK] 'delivery' eliminado de restricted_access_tag_list por el mismo motivo
    restricted_access_tag_list = Set {
      'private',
      'destination',
      'customers',
    },

    -- [TRUCK] 'hgv' y 'goods' con mayor prioridad que 'motorcar'
    -- WayHandlers.access buscará estos tags en orden y tomará el primero que encuentre.
    access_tags_hierarchy = Sequence {
      'hgv',
      'goods',
      'motorcar',
      'motor_vehicle',
      'vehicle',
      'access'
    },

    service_tag_forbidden = Set {
      'emergency_access'
    },

    -- [TRUCK] restricciones de giro también verifican tags hgv y goods
    restrictions = Sequence {
      'hgv',
      'goods',
      'motorcar',
      'motor_vehicle',
      'vehicle'
    },

    classes = Sequence {
      'toll', 'motorway', 'ferry', 'restricted', 'tunnel'
    },

    excludable = Sequence {
      Set {'toll'},
      Set {'motorway'},
      Set {'ferry'}
    },

    avoid = Set {
      'area',
      'reversible',
      'impassable',
      'hov_lanes',
      'steps',
      'construction',
      'proposed'
    },

    -- [TRUCK] Velocidades reducidas respecto a car.lua
    -- La velocidad comercial real es menor por paradas en contenedores;
    -- estos valores determinan la selección de ruta, no el ETA final.
    speeds = Sequence {
      highway = {
        motorway        = 70,  -- autopistas (acceso a Felipe Cardoso / Ruta 102)
        motorway_link   = 45,
        trunk           = 60,
        trunk_link      = 35,
        primary         = 40,  -- avenidas principales
        primary_link    = 25,
        secondary       = 35,  -- avenidas secundarias
        secondary_link  = 20,
        tertiary        = 30,  -- calles colectoras
        tertiary_link   = 15,
        unclassified    = 25,
        residential     = 20,  -- calles residenciales: aquí están los contenedores
        living_street   = 10,  -- zonas de convivencia
        service         = 15
      }
    },

    service_penalties = {
      alley             = 0.5,
      parking           = 0.5,
      parking_aisle     = 0.5,
      driveway          = 0.5,
      ["drive-through"] = 0.5,
      ["drive-thru"]    = 0.5
    },

    restricted_highway_whitelist = Set {
      'motorway',
      'motorway_link',
      'trunk',
      'trunk_link',
      'primary',
      'primary_link',
      'secondary',
      'secondary_link',
      'tertiary',
      'tertiary_link',
      'residential',
      'living_street',
      'unclassified',
      'service'
    },

    construction_whitelist = Set {
      'no',
      'widening',
      'minor',
    },

    route_speeds = {
      ferry = 5,
      shuttle_train = 10
    },

    bridge_speeds = {
      movable = 5
    },

    surface_speeds = {
      asphalt = nil,
      concrete = nil,
      ["concrete:plates"] = nil,
      ["concrete:lanes"] = nil,
      paved = nil,

      cement = 60,
      compacted = 60,
      fine_gravel = 60,

      paving_stones = 40,
      metal = 40,
      bricks = 40,

      grass = 20,
      wood = 20,
      sett = 20,
      grass_paver = 20,
      gravel = 20,
      unpaved = 20,
      ground = 20,
      dirt = 20,
      pebblestone = 20,
      tartan = 20,

      cobblestone = 15,
      clay = 15,

      earth = 10,
      stone = 10,
      rocky = 10,
      sand = 10,

      mud = 5
    },

    tracktype_speeds = {
      grade1 = 40,
      grade2 = 25,
      grade3 = 15,
      grade4 = 10,
      grade5 = 10
    },

    smoothness_speeds = {
      intermediate  = 60,
      bad           = 30,
      very_bad      = 15,
      horrible      = 10,
      very_horrible = 5,
      impassable    = 0
    },

    maxspeed_table_default = {
      urban    = 50,
      rural    = 90,
      trunk    = 110,
      motorway = 130
    },

    maxspeed_table = {
      ["at:rural"] = 100,
      ["at:trunk"] = 100,
      ["be:motorway"] = 120,
      ["be-bru:rural"] = 70,
      ["be-bru:urban"] = 30,
      ["be-vlg:rural"] = 70,
      ["by:urban"] = 60,
      ["by:motorway"] = 110,
      ["ch:rural"] = 80,
      ["ch:trunk"] = 100,
      ["ch:motorway"] = 120,
      ["cz:trunk"] = 0,
      ["cz:motorway"] = 0,
      ["de:living_street"] = 7,
      ["de:rural"] = 100,
      ["de:motorway"] = 0,
      ["dk:rural"] = 80,
      ["fr:rural"] = 80,
      ["gb:nsl_single"] = (60*1609)/1000,
      ["gb:nsl_dual"] = (70*1609)/1000,
      ["gb:motorway"] = (70*1609)/1000,
      ["nl:rural"] = 80,
      ["nl:trunk"] = 100,
      ['no:rural'] = 80,
      ['no:motorway'] = 110,
      ['pl:rural'] = 100,
      ['pl:trunk'] = 120,
      ['pl:motorway'] = 140,
      ["ro:trunk"] = 100,
      ["ru:living_street"] = 20,
      ["ru:urban"] = 60,
      ["ru:motorway"] = 110,
      ["uk:nsl_single"] = (60*1609)/1000,
      ["uk:nsl_dual"] = (70*1609)/1000,
      ["uk:motorway"] = (70*1609)/1000,
      ['za:urban'] = 60,
      ['za:rural'] = 100,
      ["none"] = 140
    },

    relation_types = Sequence {
      "route"
    },

    highway_turn_classification = {
    },

    access_turn_classification = {
    }
  }
end

-- process_node, process_way y process_turn son idénticos a car.lua.
-- Toda la lógica de restricciones físicas (handle_height, handle_width,
-- handle_weight) la ejecutan los WayHandlers usando los valores de profile.

function process_node(profile, node, result, relations)
  local access = find_access_tag(node, profile.access_tags_hierarchy)
  if access then
    if profile.access_tag_blacklist[access] and not profile.restricted_access_tag_list[access] then
      result.barrier = true
    end
  else
    local barrier = node:get_value_by_key("barrier")
    if barrier then
      local restricted_by_height = false
      if barrier == 'height_restrictor' then
        local maxheight = Measure.get_max_height(node:get_value_by_key("maxheight"), node)
        restricted_by_height = maxheight and maxheight < profile.vehicle_height
      end

      local bollard = node:get_value_by_key("bollard")
      local rising_bollard = bollard and "rising" == bollard

      local kerb = node:get_value_by_key("kerb")
      local highway = node:get_value_by_key("highway")
      local flat_kerb = kerb and ("lowered" == kerb or "flush" == kerb)
      local highway_crossing_kerb = barrier == "kerb" and highway and highway == "crossing"

      if not profile.barrier_whitelist[barrier]
              and not rising_bollard
              and not flat_kerb
              and not highway_crossing_kerb
              or restricted_by_height then
        result.barrier = true
      end
    end
  end

  local tag = node:get_value_by_key("highway")
  if "traffic_signals" == tag then
    result.traffic_lights = true
  end
end

function process_way(profile, way, result, relations)
  local data = {
    highway = way:get_value_by_key('highway'),
    bridge  = way:get_value_by_key('bridge'),
    route   = way:get_value_by_key('route')
  }

  if (not data.highway or data.highway == '') and
     (not data.route   or data.route   == '')
  then
    return
  end

  handlers = Sequence {
    WayHandlers.default_mode,
    WayHandlers.blocked_ways,
    WayHandlers.avoid_ways,
    WayHandlers.handle_height,
    WayHandlers.handle_width,
    WayHandlers.handle_length,
    WayHandlers.handle_weight,
    WayHandlers.access,
    WayHandlers.oneway,
    WayHandlers.destinations,
    WayHandlers.ferries,
    WayHandlers.movables,
    WayHandlers.service,
    WayHandlers.hov,
    WayHandlers.speed,
    WayHandlers.maxspeed,
    WayHandlers.surface,
    WayHandlers.penalties,
    WayHandlers.classes,
    WayHandlers.turn_lanes,
    WayHandlers.classification,
    WayHandlers.roundabouts,
    WayHandlers.startpoint,
    WayHandlers.driving_side,
    WayHandlers.names,
    WayHandlers.weights,
    WayHandlers.way_classification_for_turn
  }

  WayHandlers.run(profile, way, result, data, handlers, relations)

  if profile.cardinal_directions then
    Relations.process_way_refs(way, relations, result)
  end
end

function process_turn(profile, turn)
  local turn_penalty = profile.turn_penalty
  local turn_bias = turn.is_left_hand_driving and 1. / profile.turn_bias or profile.turn_bias

  if turn.has_traffic_light then
    turn.duration = profile.properties.traffic_light_penalty
  end

  if turn.number_of_roads > 2 or turn.source_mode ~= turn.target_mode or turn.is_u_turn then
    if turn.angle >= 0 then
      turn.duration = turn.duration + turn_penalty / (1 + math.exp( -((13 / turn_bias) *  turn.angle/180 - 6.5*turn_bias)))
    else
      turn.duration = turn.duration + turn_penalty / (1 + math.exp( -((13 * turn_bias) * -turn.angle/180 - 6.5/turn_bias)))
    end

    if turn.is_u_turn then
      turn.duration = turn.duration + profile.properties.u_turn_penalty
    end
  end

  if profile.properties.weight_name == 'distance' then
    turn.weight = 0
  else
    turn.weight = turn.duration
  end

  if profile.properties.weight_name == 'routability' then
    if not turn.source_restricted and turn.target_restricted then
      turn.weight = constants.max_turn_weight
    end
  end
end

return {
  setup        = setup,
  process_way  = process_way,
  process_node = process_node,
  process_turn = process_turn
}
