#!/usr/bin/env python3
"""
Generates the Node-RED flow JSON for Challenge 3.
Run: python3 generate_flow.py
Output: nodered.txt
"""

import json
import os

BASE_PATH = "/challenge3"  # Mount point inside the Node-RED container (see docker-compose.yml)
CSV_PATH = os.path.join(BASE_PATH, "challenge3.csv")
IDLOG_PATH = os.path.join(BASE_PATH, "id_log.csv")
FILTERED_PATH = os.path.join(BASE_PATH, "filtered_elems.csv")
OUTGOING_PATH = os.path.join(BASE_PATH, "outgoing_cost.csv")
THINGSPEAK_API_KEY = "YOUR_WRITE_API_KEY"  # Replace with actual key

# ─── JavaScript function bodies ───────────────────────────────────────────────

FN_PARSE_CSV = r"""
// CSV parser: handles double-quoted fields that may contain commas
function parseCSVLine(line) {
    var result = [];
    var cur = '';
    var inQ = false;
    for (var i = 0; i < line.length; i++) {
        var c = line[i];
        if (c === '"') {
            if (inQ && line[i+1] === '"') { cur += '"'; i++; }
            else inQ = !inQ;
        } else if (c === ',' && !inQ) {
            result.push(cur); cur = '';
        } else cur += c;
    }
    result.push(cur);
    return result;
}

var lines = msg.payload.split('\n');
var headers = parseCSVLine(lines[0]);
var data = {};
for (var i = 1; i < lines.length; i++) {
    var line = lines[i].trim();
    if (!line) continue;
    var fields = parseCSVLine(line);
    var pktNum = parseInt(fields[0], 10);
    if (isNaN(pktNum)) continue;
    var row = {};
    for (var j = 0; j < headers.length; j++) {
        row[headers[j].trim()] = (fields[j] !== undefined ? fields[j] : '').trim();
    }
    data[pktNum] = row;
}
global.set('csvData', data);
global.set('msgCount', 0);
global.set('done', false);
global.set('logCounter', 0);
global.set('filteredCounter', 0);
global.set('linkCosts', {});
node.log('CSV loaded: ' + Object.keys(data).length + ' rows');
return null;
""".strip()

FN_GEN_ID = r"""
var id = Math.floor(Math.random() * 30001);
var ts = Math.floor(Date.now() / 1000);
msg.payload = JSON.stringify({"id": id, "timestamp": ts});
msg.genId = id;
msg.genTs = ts;
return msg;
""".strip()

FN_LOG_FORMAT = r"""
var counter = (global.get('logCounter') || 0) + 1;
global.set('logCounter', counter);
var data = (typeof msg.payload === 'string') ? JSON.parse(msg.payload) : msg.payload;
var line = (counter === 1 ? 'No.,ID,TIMESTAMP\n' : '') +
           counter + ',' + data.id + ',' + data.timestamp + '\n';
msg.payload = line;
msg.filename = '__IDLOG__';
return msg;
""".strip().replace("__IDLOG__", IDLOG_PATH)

FN_COUNTER_ROUTE = r"""
// Gate: stop after 200 messages; also compute N and route by packet type
if (global.get('done')) return null;

var count = (global.get('msgCount') || 0) + 1;
global.set('msgCount', count);

var data = (typeof msg.payload === 'string') ? JSON.parse(msg.payload) : msg.payload;
var id = data.id;
var N = id % 5218;
var csvData = global.get('csvData');
if (!csvData) { node.warn('CSV not loaded'); return null; }

var row = csvData[N];
var isDone = (count >= 200);
if (isDone) global.set('done', true);

var finalMsg = isDone ? {payload: 'finalize'} : null;

if (!row) {
    // No matching row – ignore but count toward 200
    return [null, null, null, finalMsg];
}

var cmdStr = row['Command String'] || '';
var packetType = row['Packet Type'] || '';
var hasZCL = cmdStr.indexOf("'Layer ZBEE_ZCL'") !== -1 ||
             cmdStr.indexOf('"Layer ZBEE_ZCL"') !== -1;
var isLink = packetType.indexOf('Link Status (0x08)') !== -1;

msg.subId = id;
msg.N = N;
msg.csvRow = row;
msg.cmdStr = cmdStr;
msg.packetType = packetType;

// Outputs: 0=ZCL, 1=LinkStatus, 2=Other/Ignore, 3=Finalize trigger
if (hasZCL)   return [msg,  null, null, finalMsg];
if (isLink)   return [null, msg,  null, finalMsg];
              return [null, null, msg,  finalMsg];
""".strip()

FN_ZCL_BUILD = r"""
var row = msg.csvRow;
var ts = Math.floor(Date.now() / 1000);
var devName = row['Device Name ZigBee Source'] || 'unknown';

var payload = {
    "timestamp": ts,
    "id": msg.subId,
    "wpan.src":  row['Source Address'],
    "wpan.dst":  row['Destination Address'],
    "zbee.src":  row['Source Address ZigBee'],
    "zbee.dst":  row['Destination Address ZigBee'],
    "topic":     "ZigBee/" + devName,
    "payload":   row['Command String']
};

// Output 1: MQTT publish message
// Output 2: same msg passed along for RMS filtering
var mqttMsg = {topic: devName, payload: JSON.stringify(payload)};
msg.publishTs = ts;
return [mqttMsg, msg];
""".strip()

FN_FILTER_RMS = r"""
var cmdStr = msg.cmdStr || msg.csvRow['Command String'] || '';
var ts = msg.publishTs || Math.floor(Date.now() / 1000);

var hasRMS = cmdStr.indexOf('RMS Current') !== -1 ||
             cmdStr.indexOf('RMS Voltage') !== -1 ||
             cmdStr.indexOf('Active Power') !== -1;
if (!hasRMS) return null;

// Convert Python dict string to JSON-parseable
function py2json(s) {
    return s.replace(/'/g, '"')
            .replace(/\bTrue\b/g, 'true')
            .replace(/\bFalse\b/g, 'false')
            .replace(/\bNone\b/g, 'null');
}

var cmd;
try { cmd = JSON.parse(py2json(cmdStr)); }
catch(e) { node.warn('Parse error: ' + e.message); return null; }

var attrs = cmd['Attribute'];
if (!attrs) return null;
if (!Array.isArray(attrs)) attrs = [attrs];
// Skip Read Attributes REQUESTS (no Status = no actual values to save)
if (!cmd['Status']) return null;
var statuses = cmd['Status'];
if (!Array.isArray(statuses)) statuses = [statuses];
var dataTypes = cmd['Data Type'] || [];
if (!Array.isArray(dataTypes)) dataTypes = [dataTypes];
var seqNum = cmd['Sequence Number'] || '';

function getValueKey(dt) {
    if (!dt) return null;
    var l = dt.toLowerCase();
    if (l.indexOf('unsigned') !== -1 && l.indexOf('8-bit') !== -1)  return 'Uint8';
    if (l.indexOf('signed') !== -1   && l.indexOf('8-bit') !== -1)  return 'Int8';
    if (l.indexOf('unsigned') !== -1 && l.indexOf('16-bit') !== -1) return 'Uint16';
    if (l.indexOf('signed') !== -1   && l.indexOf('16-bit') !== -1) return 'Int16';
    if (l.indexOf('unsigned') !== -1 && l.indexOf('32-bit') !== -1) return 'Uint32';
    if (l.indexOf('signed') !== -1   && l.indexOf('32-bit') !== -1) return 'Int32';
    return null;
}

var targets = ['Active Power', 'RMS Current', 'RMS Voltage'];
var typeCounters = {};
var results = [];

for (var i = 0; i < attrs.length; i++) {
    var attr     = attrs[i];
    var dt       = Array.isArray(dataTypes) ? dataTypes[i] : dataTypes;
    var status   = Array.isArray(statuses)  ? statuses[i]  : statuses;
    var vKey     = getValueKey(dt);
    var idx      = vKey ? (typeCounters[vKey] || 0) : 0;
    if (vKey) typeCounters[vKey] = idx + 1;

    var attrBase = attr.split(' (')[0];
    var isTarget = targets.some(function(t) { return attrBase.indexOf(t) !== -1; });
    if (!isTarget) continue;

    var valueRaw = null;
    if (vKey && cmd[vKey] !== undefined) {
        var vArr = cmd[vKey];
        valueRaw = Array.isArray(vArr) ? vArr[idx] : vArr;
    }
    var numValue = (valueRaw !== null && valueRaw !== undefined)
                    ? String(valueRaw).split(' ')[0] : '0';

    results.push({
        ts:       ts,
        seqNum:   seqNum,
        attr:     attrBase,
        status:   (status || '').split(' (')[0],
        dataType: (dt || '').split(' (')[0],
        value:    numValue
    });
}

if (results.length === 0) return null;

msg.rmsResults = results;
return msg;
""".strip()

FN_FORMAT_FILTERED = r"""
var results = msg.rmsResults;
var counter = global.get('filteredCounter') || 0;
var lines = [];
if (counter === 0) lines.push('No.,Timestamp,Sequence Number, Attribute,Status,Data Type,Data Value');

for (var i = 0; i < results.length; i++) {
    counter++;
    var r = results[i];
    lines.push(counter + ',' + r.ts + ',' + r.seqNum + ',' +
               r.attr + ',' + r.status + ',' + r.dataType + ',' + r.value);
}
global.set('filteredCounter', counter);
msg.payload = lines.join('\n') + '\n';
msg.filename = '__FILTERED__';
return msg;
""".strip().replace("__FILTERED__", FILTERED_PATH)

FN_CHART_PREP = r"""
var results = msg.rmsResults;
var ts = msg.rmsResults[0].ts * 1000;
var vMsg = null, iMsg = null;

for (var i = 0; i < results.length; i++) {
    var r = results[i];
    var v = parseFloat(r.value);
    if (r.attr.indexOf('RMS Voltage') !== -1)
        vMsg = {payload: v, topic: 'RMS Voltage'};
    if (r.attr.indexOf('RMS Current') !== -1)
        iMsg = {payload: v, topic: 'RMS Current'};
}
return [vMsg, iMsg];
""".strip()

FN_LINK_STATUS = r"""
var row = msg.csvRow;
var zbeeSource = row['Source Address ZigBee'];
var cmdStr = row['Command String'] || '';

function py2json(s) {
    return s.replace(/'/g, '"')
            .replace(/\bTrue\b/g, 'true')
            .replace(/\bFalse\b/g, 'false')
            .replace(/\bNone\b/g, 'null');
}

var cmd;
try { cmd = JSON.parse(py2json(cmdStr)); }
catch(e) { node.warn('LinkStatus parse error: ' + e.message); return null; }

var links = cmd['Links'];
if (!links || !Array.isArray(links)) return null;

var linkCosts = global.get('linkCosts') || {};
for (var i = 0; i < links.length; i++) {
    var lnk = links[i];
    var key = zbeeSource + '|' + lnk['Address'];
    linkCosts[key] = {
        source:      zbeeSource,
        destination: lnk['Address'],
        cost:        parseInt(lnk['Outgoing Cost'], 10)
    };
}
global.set('linkCosts', linkCosts);
return null;
""".strip()

FN_FINALIZE = r"""
// Called once at 200 messages; writes outgoing_cost.csv and queues ThingSpeak sends
var linkCosts = global.get('linkCosts') || {};
var entries = Object.values(linkCosts);

// Sort by source hex, then destination hex
entries.sort(function(a, b) {
    var sa = parseInt(a.source, 16), sb = parseInt(b.source, 16);
    if (sa !== sb) return sa - sb;
    return parseInt(a.destination, 16) - parseInt(b.destination, 16);
});

// Write CSV
var csvLines = ['No.,Source,Destination,Cost'];
for (var i = 0; i < entries.length; i++)
    csvLines.push((i+1)+','+entries[i].source+','+entries[i].destination+','+entries[i].cost);

var csvContent = csvLines.join('\n') + '\n';
var csvMsg = { payload: csvContent, filename: '__OUTGOING__' };

// Find smallest source address
var sourceSet = {};
entries.forEach(function(e) { sourceSet[e.source] = true; });
var sources = Object.keys(sourceSet).sort(function(a,b) {
    return parseInt(a,16) - parseInt(b,16);
});
var smallest = sources[0];

// Destinations for smallest source, sorted ascending hex
var targets = entries.filter(function(e){ return e.source === smallest; });
targets.sort(function(a,b){ return parseInt(a.destination,16) - parseInt(b.destination,16); });

node.log('Smallest source: ' + smallest + ', destinations: ' + targets.length);

// Build array of ThingSpeak HTTP messages
// (url and method are set on the http-request node; do NOT put them on msg in NR4)
var tsMsgs = targets.map(function(e) {
    return {
        payload: JSON.stringify({ api_key: '__API_KEY__', field1: e.cost }),
        headers: { 'Content-Type': 'application/json' }
    };
});

// Output 0: CSV write  Output 1: array of ThingSpeak messages (rate-limited downstream)
return [csvMsg, tsMsgs.length > 0 ? tsMsgs : null];
""".strip().replace("__OUTGOING__", OUTGOING_PATH).replace("__API_KEY__", THINGSPEAK_API_KEY)


# ─── Build Node-RED JSON ───────────────────────────────────────────────────────

def make_node(id_, type_, name, z, x, y, wires, **extra):
    node = {"id": id_, "type": type_, "name": name, "z": z, "x": x, "y": y, "wires": wires}
    node.update(extra)
    return node

Z = "challenge3_flow"

nodes = []

# ── Tab ──────────────────────────────────────────────────────────────────────
nodes.append({"id": Z, "type": "tab", "label": "Challenge 3", "disabled": False, "info": ""})

# ── MQTT Broker Config ────────────────────────────────────────────────────────
nodes.append({
    "id": "mqtt_broker_cfg",
    "type": "mqtt-broker",
    "name": "Local Mosquitto 1884",
    "broker": "mosquitto",   # Docker service name on the iot-net network
    "port": "1884",
    "clientid": "nodered_c3",
    "autoConnect": True,
    "usetls": False,
    "protocolVersion": "4",
    "keepalive": "60",
    "cleansession": True,
    "autoUnsubscribe": True,
    "birthTopic": "", "birthQos": "0", "birthPayload": "", "birthMsg": {},
    "closeTopic": "", "closeQos": "0", "closePayload": "", "closeMsg": {},
    "willTopic": "",  "willQos": "0", "willPayload": "",  "willMsg": {},
    "userProps": "", "sessionExpiry": ""
})

# ════════════════════════════════════════════════════════════════════════════════
# INIT BRANCH  (y=60)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("init_inject", "inject", "Load CSV on startup", Z, 140, 60,
    [["init_file_read"]],
    props=[{"p": "payload", "v": "", "vt": "date"}],
    repeat="", once=True, onceDelay=0.5, topic="", payload="", payloadType="date"
))

nodes.append(make_node("init_file_read", "file in", "Read challenge3.csv", Z, 360, 60,
    [["init_parse_csv"]],
    filename=CSV_PATH, format="utf8", chunk=False, sendError=False, encoding="none"
))

nodes.append(make_node("init_parse_csv", "function", "Parse & Store CSV", Z, 590, 60,
    [[]],
    func=FN_PARSE_CSV, outputs=1
))

# ════════════════════════════════════════════════════════════════════════════════
# PUBLISHER BRANCH  (y=160)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("pub_inject", "inject", "Every 1 second", Z, 140, 160,
    [["pub_gen_id"]],
    props=[{"p": "payload", "v": "", "vt": "date"}],
    repeat="1", once=False, onceDelay=0.1, topic="", payload="", payloadType="date"
))

nodes.append(make_node("pub_gen_id", "function", "Generate ID & Timestamp", Z, 360, 160,
    [["pub_mqtt_out", "pub_log_fmt"]],
    func=FN_GEN_ID, outputs=2
))

nodes.append(make_node("pub_mqtt_out", "mqtt out", "Publish to id_generator", Z, 600, 140,
    [[]],
    topic="challenge3/id_generator", qos="0", retain="false",
    respTopic="", contentType="", userProps="", correl="",
    expiry="", broker="mqtt_broker_cfg"
))

nodes.append(make_node("pub_log_fmt", "function", "Format id_log.csv", Z, 600, 180,
    [["pub_log_file"]],
    func=FN_LOG_FORMAT, outputs=1
))

nodes.append(make_node("pub_log_file", "file", "Write id_log.csv", Z, 810, 180,
    [[]],
    filename=IDLOG_PATH, filenameType="str", appendNewline=False,
    createDir=True, overwriteFile="false", encoding="none"
))

# ════════════════════════════════════════════════════════════════════════════════
# SUBSCRIBER BRANCH  (y=300)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("sub_mqtt_in", "mqtt in", "Subscribe id_generator", Z, 140, 300,
    [["sub_counter_route"]],
    topic="challenge3/id_generator", qos="0", datatype="auto-detect",
    broker="mqtt_broker_cfg", nl=False, rap=True, rh=0, inputs=0
))

# 4-output function: ZCL | LinkStatus | Other | Finalize
nodes.append(make_node("sub_counter_route", "function", "Counter + Route (200 msgs)", Z, 360, 300,
    [["zcl_rate_limit"], ["link_fn"], ["dbg_ignore"], ["fin_fn"]],
    func=FN_COUNTER_ROUTE, outputs=4
))

nodes.append(make_node("dbg_ignore", "debug", "Ignored messages", Z, 600, 360,
    [[]],
    active=False, tosidebar=True, console=False, tostatus=False, complete="false"
))

# ════════════════════════════════════════════════════════════════════════════════
# ZCL PATH  (y=440)
# ════════════════════════════════════════════════════════════════════════════════
# Rate limiter: 10 messages per minute
nodes.append(make_node("zcl_rate_limit", "delay", "Rate Limit 10/min", Z, 140, 440,
    [["zcl_build_fn"]],
    pauseType="rate", timeout="5", timeoutUnits="seconds",
    rate="10", nbRateUnits="1", rateUnits="minute",
    randomFirst="1", randomLast="5", randomUnits="seconds",
    drop=False, allowrate=False, outputs=1
))

nodes.append(make_node("zcl_build_fn", "function", "Build ZCL MQTT Payload", Z, 360, 440,
    [["zcl_mqtt_out"], ["rms_filter_fn"]],
    func=FN_ZCL_BUILD, outputs=2
))

nodes.append(make_node("zcl_mqtt_out", "mqtt out", "Publish to ZigBee Topic", Z, 600, 400,
    [[]],
    topic="", qos="0", retain="false",
    respTopic="", contentType="", userProps="", correl="",
    expiry="", broker="mqtt_broker_cfg"
))

# ════════════════════════════════════════════════════════════════════════════════
# RMS FILTER PATH  (y=540)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("rms_filter_fn", "function", "Filter RMS / Active Power", Z, 600, 480,
    [["rms_fmt_fn", "chart_prep_fn"]],
    func=FN_FILTER_RMS, outputs=1
))

nodes.append(make_node("rms_fmt_fn", "function", "Format filtered_elems.csv", Z, 840, 460,
    [["rms_file"]],
    func=FN_FORMAT_FILTERED, outputs=1
))

nodes.append(make_node("rms_file", "file", "Write filtered_elems.csv", Z, 1060, 460,
    [[]],
    filename=FILTERED_PATH, filenameType="str", appendNewline=False,
    createDir=True, overwriteFile="false", encoding="none"
))

# Chart preparation: 2 outputs (voltage, current)
nodes.append(make_node("chart_prep_fn", "function", "Prepare Chart Data", Z, 840, 520,
    [["node_chart_voltage"], ["node_chart_current"]],
    func=FN_CHART_PREP, outputs=2
))

nodes.append({
    "id": "node_chart_voltage", "type": "ui_chart", "name": "RMS Voltage",
    "z": Z, "x": 1060, "y": 500,
    "group": "node_grp_zigbee", "order": 1, "width": 6, "height": 4,
    "label": "RMS Voltage (V)", "chartType": "line", "legend": "false",
    "xformat": "HH:mm:ss", "interpolate": "linear", "nodata": "",
    "dots": False, "ymin": "", "ymax": "", "removeOlder": "1",
    "removeOlderPoints": "", "removeOlderUnit": "3600",
    "cutout": 0, "useOneColor": False, "colors": ["#1f77b4"],
    "useOldColors": False, "wires": [[]]
})

nodes.append({
    "id": "node_chart_current", "type": "ui_chart", "name": "RMS Current",
    "z": Z, "x": 1060, "y": 560,
    "group": "node_grp_zigbee", "order": 2, "width": 6, "height": 4,
    "label": "RMS Current (A)", "chartType": "line", "legend": "false",
    "xformat": "HH:mm:ss", "interpolate": "linear", "nodata": "",
    "dots": False, "ymin": "", "ymax": "", "removeOlder": "1",
    "removeOlderPoints": "", "removeOlderUnit": "3600",
    "cutout": 0, "useOneColor": False, "colors": ["#ff7f0e"],
    "useOldColors": False, "wires": [[]]
})

nodes.append({
    "id": "node_grp_zigbee", "type": "ui_group", "name": "ZigBee Measurements",
    "tab": "node_tab_ch3", "order": 1, "disp": True, "width": 12, "collapse": False
})

nodes.append({
    "id": "node_tab_ch3", "type": "ui_tab", "name": "Challenge 3",
    "icon": "dashboard", "disabled": False, "hidden": False
})

# ════════════════════════════════════════════════════════════════════════════════
# LINK STATUS PATH  (y=640)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("link_fn", "function", "Process Link Status", Z, 600, 640,
    [[]],
    func=FN_LINK_STATUS, outputs=1
))

# ════════════════════════════════════════════════════════════════════════════════
# FINALIZE PATH  (y=760)
# ════════════════════════════════════════════════════════════════════════════════
nodes.append(make_node("fin_fn", "function", "Finalize: Write CSV + ThingSpeak", Z, 600, 760,
    [["fin_csv_file"], ["ts_rate_delay"]],
    func=FN_FINALIZE, outputs=2
))

nodes.append(make_node("fin_csv_file", "file", "Write outgoing_cost.csv", Z, 840, 720,
    [[]],
    filename=OUTGOING_PATH, filenameType="str", appendNewline=False,
    createDir=True, overwriteFile="true", encoding="none"
))

# Rate limit ThingSpeak: 1 per 20 seconds
nodes.append(make_node("ts_rate_delay", "delay", "Rate Limit 1/20s", Z, 840, 800,
    [["ts_http"]],
    pauseType="rate", timeout="5", timeoutUnits="seconds",
    rate="1", nbRateUnits="20", rateUnits="seconds",
    randomFirst="1", randomLast="5", randomUnits="seconds",
    drop=False, allowrate=False, outputs=1
))

nodes.append(make_node("ts_http", "http request", "POST to ThingSpeak", Z, 1060, 800,
    [["ts_debug"]],
    method="POST", ret="txt", paytoqs="ignore",
    url="https://api.thingspeak.com/update.json",
    tls="", persist=False, proxy="", insecureHTTPParser=False,
    authType="", senderr=False, headers=[]
))

nodes.append(make_node("ts_debug", "debug", "ThingSpeak Response", Z, 1300, 800,
    [[]],
    active=True, tosidebar=True, console=False, tostatus=False, complete="payload"
))

# ─── Write output ──────────────────────────────────────────────────────────────
output_path = os.path.join(
    "/Users/javad/Desktop/MSc AI Polimi/Courses/2025-2026 - Sem 2/Internet of Things/Materials/Challenges/Challenge 3",
    "nodered.txt"
)

with open(output_path, "w") as f:
    json.dump(nodes, f, indent=2)

print(f"Written {len(nodes)} nodes to {output_path}")
print("Done! Import nodered.txt into Node-RED.")
print()
print("Before running, update BASE_PATH at the top of this script to match")
print("the directory where challenge3.csv lives on your Node-RED host.")
