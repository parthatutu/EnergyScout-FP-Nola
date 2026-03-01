import minimalmodbus
import serial
import serial.rs485 
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- SCOUT IDENTITY ---
SCOUT_ID = "scout3"

# --- INFLUXDB CLOUD CONFIGURATION ---
INFLUX_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN = "qsGKuJsL9po_6rsu8VpoLmspiyWfcvQRK2oCpu2Vht6je5_aYJMk16YKAci0cQB2Jn0-3hpkScs6KtBLJUZEVw=="
INFLUX_ORG = "Atutu"
INFLUX_BUCKET = "power-monitoring"

# --- LOCAL LOG FILE ---
LOG_FILE = "/home/scout3/Desktop/data.txt"

# Initialize InfluxDB Client
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

# --- MODBUS CONFIGURATION ---
MODBUS_PORT   = '/dev/ttyAMA0'
GPS_PORT      = '/dev/ttyAMA2'
BAUD_MODBUS   = 9600
BAUD_GPS      = 9600
GPS_INTERVAL  = 1800  # 30 minutes
POLL_INTERVAL = 60    # 60 seconds per cycle

# --- LOCAL LOGGER ---
def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(entry + "\n")
    except Exception as e:
        print(f"[LOG ERROR] Could not write to {LOG_FILE}: {e}")

# --- INSTRUMENT SETUP ---
def setup_modbus(slave_id):
    try:
        ins = minimalmodbus.Instrument(MODBUS_PORT, slave_id)
        ins.serial.baudrate = BAUD_MODBUS
        ins.serial.timeout = 0.5
        ins.mode = minimalmodbus.MODE_RTU
        ins.serial.rs485_mode = serial.rs485.RS485Settings(
            rts_level_for_tx=True, rts_level_for_rx=False,
            delay_before_tx=0.001, delay_before_rx=0.001
        )
        return ins
    except Exception as e:
        log(f"[SETUP ERROR] Slave {slave_id}: {e}")
        return None

meters = [
    {"id": 4, "name": f"{SCOUT_ID}_AC_Meter_SDM120", "type": "sdm120", "obj": setup_modbus(4)},
    {"id": 2, "name": f"{SCOUT_ID}_DC_Meter_1",      "type": "dc",     "obj": setup_modbus(2)},
    {"id": 3, "name": f"{SCOUT_ID}_DC_Meter_2",      "type": "dc",     "obj": setup_modbus(3)}
]

# --- GPS HELPERS ---
def parse_nmea_to_decimal(value, direction):
    if not value or not direction:
        return None
    float_val = float(value)
    degrees = int(float_val / 100)
    minutes = float_val - (degrees * 100)
    decimal = degrees + (minutes / 60)
    if direction in ['S', 'W']:
        decimal *= -1
    return round(decimal, 6)

def get_gps_location():
    log(f"[GPS] Attempting location from {GPS_PORT}...")
    try:
        with serial.Serial(GPS_PORT, BAUD_GPS, timeout=2) as ser:
            start_search = time.time()
            while time.time() - start_search < 5:
                line = ser.readline().decode('ascii', errors='replace').strip()
                if line.startswith("$GPRMC"):
                    parts = line.split(',')
                    if len(parts) > 6 and parts[2] == 'A':
                        lat = parse_nmea_to_decimal(parts[3], parts[4])
                        lon = parse_nmea_to_decimal(parts[5], parts[6])
                        log(f"[GPS] FIX: LAT={lat}, LON={lon}")
                        return lat, lon
                    elif len(parts) > 2 and parts[2] == 'V':
                        log("[GPS] NO FIX")
                        return None, None
            log("[GPS] TIMEOUT")
            return None, None
    except Exception as e:
        log(f"[GPS ERROR] {e}")
        return None, None

# --- INFLUXDB WRITERS ---
def send_gps_to_influxdb(latitude, longitude):
    try:
        point = Point("gps_location") \
            .tag("scout", SCOUT_ID) \
            .field("latitude",  latitude) \
            .field("longitude", longitude)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        log(f"[INFLUX] GPS sent: LAT={latitude}, LON={longitude}")
    except Exception as e:
        log(f"[INFLUX ERROR] GPS: {e}")

def send_to_influxdb(meter_name, meter_type, voltage=None, current=None, power=None,
                     power_factor=None, latitude=None, longitude=None):
    try:
        point = Point("power_meter") \
            .tag("scout", SCOUT_ID) \
            .tag("meter", meter_name) \
            .tag("type",  meter_type)

        if voltage      is not None: point.field("voltage",      voltage)
        if current      is not None: point.field("current",      current)
        if power        is not None: point.field("power",        power)
        if power_factor is not None: point.field("power_factor", power_factor)

        if latitude is not None and longitude is not None:
            point.field("latitude",  latitude)
            point.field("longitude", longitude)

        write_api.write(bucket=INFLUX_BUCKET, record=point)

        fields = []
        if voltage      is not None: fields.append(f"{voltage:.2f}V")
        if current      is not None: fields.append(f"{current:.4f}A")
        if power        is not None: fields.append(f"{power:.2f}W")
        if power_factor is not None: fields.append(f"PF={power_factor:.3f}")
        log(f"[INFLUX] {meter_name} = {' | '.join(fields)}")
    except Exception as e:
        log(f"[INFLUX ERROR] {meter_name}: {e}")

# --- MAIN EXECUTION ---
last_gps_time = 0
current_lat   = None
current_lon   = None

log(f"[{SCOUT_ID}] System Started. Polling all meters every {POLL_INTERVAL}s...")
log(f"[{SCOUT_ID}] Sending data to InfluxDB Cloud at: {INFLUX_URL}")
log(f"[{SCOUT_ID}] Logging to: {LOG_FILE}")

while True:
    try:
        current_time = time.time()
        cycle_start  = time.time()

        log(f"[{SCOUT_ID}] --- NEW CYCLE ---")

        # 1. GPS UPDATE (Every 30 Minutes)
        if current_time - last_gps_time >= GPS_INTERVAL:
            try:
                current_lat, current_lon = get_gps_location()
                if current_lat is not None and current_lon is not None:
                    send_gps_to_influxdb(current_lat, current_lon)
            except Exception as e:
                log(f"[GPS ERROR] Unexpected: {e}")
                current_lat, current_lon = None, None
            last_gps_time = current_time

        # 2. METER POLLING (one read per meter per cycle)
        for m in meters:
            if m["obj"] is None:
                log(f"[SKIP] {m['name']} (Slave {m['id']}) - Not initialized")
                continue

            log(f"[READING] {m['name']} (Slave {m['id']})")
            try:
                if m["type"] == "sdm120":
                    v  = m["obj"].read_float(0,  functioncode=4)
                    a  = m["obj"].read_float(6,  functioncode=4)
                    p  = m["obj"].read_float(12, functioncode=4)
                    pf = m["obj"].read_float(30, functioncode=4)
                    log(f"[AC] {m['name']} -> {v:.1f}V | {a:.4f}A | {p:.2f}W | PF={pf:.3f}")
                    send_to_influxdb(m['name'], m['type'],
                                     voltage=v, current=a, power=p, power_factor=pf,
                                     latitude=current_lat, longitude=current_lon)
                else:
                    raw_v = m["obj"].read_long(256, functioncode=3)
                    raw_a = m["obj"].read_long(258, functioncode=3)
                    raw_p = m["obj"].read_long(260, functioncode=3)
                    v = raw_v / 10000.0
                    a = raw_a / 10000.0
                    p = raw_p / 10000.0
                    log(f"[DC] {m['name']} -> {v:.2f}V | {a:.4f}A | {p:.2f}W")
                    send_to_influxdb(m['name'], m['type'],
                                     voltage=v, current=a, power=p,
                                     latitude=current_lat, longitude=current_lon)
            except Exception as e:
                log(f"[METER ERROR] {m['name']}: {e}")

        # 3. SLEEP for remainder of 60-second cycle
        elapsed    = time.time() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        log(f"[{SCOUT_ID}] Cycle complete. Sleeping {sleep_time:.1f}s...")
        time.sleep(sleep_time)

    except KeyboardInterrupt:
        log(f"[{SCOUT_ID}] Shutting down gracefully...")
        try:
            influx_client.close()
        except:
            pass
        log(f"[{SCOUT_ID}] InfluxDB connection closed. Goodbye!")
        break

    except Exception as e:
        log(f"[{SCOUT_ID}] Fatal error: {e} — restarting in 10s...")
        time.sleep(10)
