import minimalmodbus
import serial
import serial.rs485 
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- SCOUT IDENTITY ---
SCOUT_ID = "scout2"

# --- INFLUXDB CLOUD CONFIGURATION ---
INFLUX_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_TOKEN = "qsGKuJsL9po_6rsu8VpoLmspiyWfcvQRK2oCpu2Vht6je5_aYJMk16YKAci0cQB2Jn0-3hpkScs6KtBLJUZEVw=="
INFLUX_ORG = "Atutu"
INFLUX_BUCKET = "power-monitoring"

# Initialize InfluxDB Client
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

# --- MODBUS CONFIGURATION ---
MODBUS_PORT  = '/dev/ttyAMA0'
GPS_PORT     = '/dev/ttyAMA2' 
BAUD_MODBUS  = 9600
BAUD_GPS     = 9600
GPS_INTERVAL = 1800  # 30 minutes

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
    except: 
        return None

meters = [
    {"id": 4, "name": f"{SCOUT_ID}_AC_Meter_SDM120", "type": "sdm120", "obj": setup_modbus(4)},
    {"id": 2, "name": f"{SCOUT_ID}_DC_Meter_1",      "type": "dc",     "obj": setup_modbus(2)},
    {"id": 3, "name": f"{SCOUT_ID}_DC_Meter_2",      "type": "dc",     "obj": setup_modbus(3)}
]

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
    print(f"\n[GPS] [{SCOUT_ID}] Attempting location from {GPS_PORT}...")
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
                        print(f">>> GPS: LAT: {lat}, LON: {lon}")
                        return lat, lon
                    elif len(parts) > 2 and parts[2] == 'V':
                        print(">>> GPS: NO FIX")
                        return None, None
            print(">>> GPS: TIMEOUT")
            return None, None
    except Exception as e: 
        print(f">>> GPS: ERROR - {e} (continuing without GPS)")
        return None, None

def send_to_influxdb(meter_name, meter_type, voltage=None, current=None, power=None, latitude=None, longitude=None):
    try:
        point = Point("power_meter") \
            .tag("scout", SCOUT_ID) \
            .tag("meter", meter_name) \
            .tag("type", meter_type)
        
        if voltage is not None:
            point.field("voltage", voltage)
        if current is not None:
            point.field("current", current)
        if power is not None:
            point.field("power", power)
        if latitude is not None and longitude is not None:
            point.field("latitude", latitude)
            point.field("longitude", longitude)
        
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        fields = []
        if voltage is not None: fields.append(f"{voltage:.2f}V")
        if current is not None: fields.append(f"{current:.2f}A")
        if power is not None: fields.append(f"{power:.1f}W")
        print(f"✓ [{SCOUT_ID}] Data sent: {meter_name} = {' | '.join(fields)}")
    except Exception as e:
        print(f"✗ [{SCOUT_ID}] InfluxDB Error: {e} (continuing)")

# --- MAIN EXECUTION ---
last_gps_time = 0 
current_lat = None
current_lon = None

print(f"[{SCOUT_ID}] System Started. Polling SDM120 and DC Meters...")
print(f"[{SCOUT_ID}] Sending data to InfluxDB Cloud at: {INFLUX_URL}")
print(f"[{SCOUT_ID}] NOTE: System will continue running even if GPS or individual meters fail")

try:
    while True:
        current_time = time.time()
        
        # 1. GPS UPDATE (Every 30 Minutes)
        if current_time - last_gps_time >= GPS_INTERVAL:
            try:
                current_lat, current_lon = get_gps_location()
            except Exception as e:
                print(f">>> GPS: Unexpected error - {e} (continuing without GPS)")
                current_lat, current_lon = None, None
            last_gps_time = current_time
        
        # 2. METER POLLING (5 Seconds per meter)
        for m in meters:
            if m["obj"] is None: 
                print(f"\n--- Skipping {m['name']} (Slave {m['id']}) - Not initialized ---")
                continue
            
            print(f"\n--- {m['name']} (Slave {m['id']}) ---")
            meter_start = time.time()
            successful_reads = 0
            
            while time.time() - meter_start < 5:
                try:
                    if m["type"] == "sdm120":
                        v = m["obj"].read_float(0, functioncode=4)
                        a = m["obj"].read_float(6, functioncode=4)
                        p = m["obj"].read_float(12, functioncode=4)
                        print(f"AC -> {v:.1f}V | {a:.2f}A | {p:.1f}W")
                        send_to_influxdb(m['name'], m['type'], voltage=v, current=a, power=p,
                                        latitude=current_lat, longitude=current_lon)
                        successful_reads += 1
                    else:
                        raw_v = m["obj"].read_long(256, functioncode=3)
                        v = raw_v / 10000.0
                        print(f"DC -> {v:.2f}V")
                        send_to_influxdb(m['name'], m['type'], voltage=v,
                                        latitude=current_lat, longitude=current_lon)
                        successful_reads += 1
                        
                except Exception as e:
                    print(f"{m['name']} Error: {e} (continuing)")
                
                time.sleep(1)
            
            if successful_reads == 0:
                print(f"{m['name']}: No successful reads in this cycle (will retry next cycle)")

except KeyboardInterrupt:
    print(f"\n[{SCOUT_ID}] Shutting down gracefully...")
    influx_client.close()
    print(f"[{SCOUT_ID}] InfluxDB connection closed. Goodbye!")
except Exception as e:
    print(f"\n[{SCOUT_ID}] Unexpected error in main loop: {e}")
    try:
        influx_client.close()
    except:
        pass
    print(f"[{SCOUT_ID}] System stopped.")
