from flask import Flask, request, jsonify, g
from flask_cors import CORS
import paho.mqtt.client as mqtt
import mysql.connector
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import json
import datetime
import requests
import jwt
from functools import wraps

# ==========================================
# INISIALISASI FLASK & CORS
# ==========================================
app = Flask(__name__)
CORS(app) # Mengizinkan Klien HTML dari asal mana pun untuk mengakses API
app.config['SECRET_KEY'] = "kunci_rahasia_ras_iot_super_aman"

# ==========================================
# KONFIGURASI DATABASE & MQTT
# ==========================================
MYSQL_HOST = "localhost"
MYSQL_USER = "ras_backend"      
MYSQL_PASS = "ras123"          
MYSQL_DB   = "db_ras"

INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "CIoSGGTn2bRlr4lnoWkPpp7_1Knt9BlJVzIk82B7QTyL0P9Yec3bY_x3bSH3yaJfme_xxmXMvRGVwNy6je_DPw=="
INFLUX_ORG    = "Organisasi_RAS"
INFLUX_BUCKET = "sensor_data"

MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883
MQTT_TOPIC  = "ras/+/sensor/data" 

TELEGRAM_BOT_TOKEN = "8829971865:AAGY4BjOaJ1I4NWuK_ApBcoSumXWkxu2QTc"

influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

status_relay_terakhir = {}
status_alarm_terakhir = {} 

# ==========================================
# MIDDLEWARE KEAMANAN (JWT AUTHENTICATION)
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        # Mengambil token dari header Authorization: Bearer <token>
        if 'Authorization' in request.headers:
            parts = request.headers['Authorization'].split()
            if len(parts) == 2:
                token = parts[1]
        
        if not token:
            return jsonify({"status": "error", "pesan": "Token otorisasi hilang! Akses ditolak."}), 401
        
        try:
            # Membuka gembok token
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            g.user_id = data['user_id'] # Menyimpan ID User ke variabel global Flask selama request berlangsung
        except jwt.ExpiredSignatureError:
            return jsonify({"status": "error", "pesan": "Sesi telah berakhir (Token Expired). Silakan login ulang."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"status": "error", "pesan": "Token tidak valid!"}), 401
            
        return f(*args, **kwargs)
    return decorated

# ==========================================
# FUNGSI PEMBANTU
# ==========================================
def kirim_notifikasi_telegram(pesan, chat_id):
    if not chat_id: return 
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": pesan, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram Error] {e}")

def get_mysql_connection():
    return mysql.connector.connect(host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB)

def cek_kepemilikan_alat(device_id):
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT k.id_kolam, k.nama_kolam, u.telegram_chat_id 
            FROM devices d
            JOIN kolam k ON d.id_kolam = k.id_kolam
            JOIN users u ON k.id_user = u.id_user
            WHERE d.id_device = %s
        """, (device_id,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result 
    except Exception as e:
        print(f"[MySQL Error] {e}")
        return None

def cek_dan_eksekusi_threshold(incoming_device_id, device_info, payload, mqtt_client):
    try:
        id_kolam = device_info['id_kolam']
        nama_kolam = device_info['nama_kolam']
        user_chat_id = device_info['telegram_chat_id']

        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id_aturan, sensor, jenis_batas, nilai_batas, target_device_id, target_relay, perintah 
            FROM aturan_threshold 
            WHERE id_kolam = %s AND trigger_device_id = %s AND is_active = TRUE
        """, (id_kolam, incoming_device_id))
        aturan_list = cursor.fetchall()
        cursor.close()
        conn.close()

        # Pemetaan nama sensor agar lebih enak dibaca di Telegram
        sensor_map = {
            'suhu': 'Suhu Air', 
            'ph': 'pH Air', 
            'tds_ppm': 'TDS', 
            'do_mgL': 'Oksigen (DO)', 
            'air_penuh': 'Status Air'
        }

        for aturan in aturan_list:
            sensor = aturan['sensor']
            if sensor not in payload: continue
                
            nilai_sekarang = float(payload[sensor])
            batas = float(aturan['nilai_batas'])
            jenis = aturan['jenis_batas']
            
            target_device = aturan['target_device_id']
            relay = aturan['target_relay'] # contoh nilai: 'input1', 'input2'
            id_aturan = aturan['id_aturan']
            perintah_aksi = aturan['perintah'] 
            
            terpicu = (jenis == 'atas' and nilai_sekarang > batas) or (jenis == 'bawah' and nilai_sekarang < batas)
            rule_key = f"aturan_{id_aturan}"
            status_lama_terpicu = status_alarm_terakhir.get(rule_key, False)

            # --- LOGIKA PENARIKAN NAMA KUSTOM RELAY DARI DATABASE ---
            nama_relay_kustom = ""
            if relay:
                nama_relay_kustom = relay.upper() # Fallback bawaan (INPUT1)
                if target_device:
                    try:
                        conn2 = get_mysql_connection()
                        cursor2 = conn2.cursor(dictionary=True)
                        cursor2.execute("SELECT capabilities FROM devices WHERE id_device = %s", (target_device,))
                        dev_data = cursor2.fetchone()
                        cursor2.close()
                        conn2.close()
                        
                        if dev_data and dev_data['capabilities']:
                            caps = json.loads(dev_data['capabilities']) if isinstance(dev_data['capabilities'], str) else dev_data['capabilities']
                            if 'relay_names' in caps:
                                # Konversi 'input1' menjadi format kunci JSON 'r1_en'
                                r_key = f"r{relay[-1]}_en" 
                                if r_key in caps['relay_names'] and caps['relay_names'][r_key]:
                                    nama_relay_kustom = caps['relay_names'][r_key].upper()
                    except Exception as e:
                        print(f"[Error Ambil Nama Relay] {e}")

            nama_sensor_rapi = sensor_map.get(sensor, sensor.upper())

            if terpicu and not status_lama_terpicu:
                status_alarm_terakhir[rule_key] = True 
                
                if target_device is None or relay is None:
                    pesan = f"<b>[ALARM PANTAU]</b>\n\n<b>Kolam:</b> {nama_kolam}\n<b>Info:</b> {nama_sensor_rapi} mencapai {nilai_sekarang} (Batas {jenis}: {batas}).\n<b>Status:</b> Butuh tindakan manual."
                    kirim_notifikasi_telegram(pesan, user_chat_id)
                else:
                    state_baru = perintah_aksi
                    mqtt_client.publish(f"ras/{target_device}/kontrol/{relay}", state_baru)
                    status_relay_terakhir[f"{target_device}_{relay}"] = state_baru
                    
                    pesan = f"<b>[OTOMASI AKTIF]</b>\n\n<b>Kolam:</b> {nama_kolam}\n<b>Pemicu:</b> {nama_sensor_rapi} pada {incoming_device_id} terbaca {nilai_sekarang}.\n<b>Aksi:</b> Status {nama_relay_kustom} di alat {target_device} diubah menjadi {state_baru}."
                    kirim_notifikasi_telegram(pesan, user_chat_id)

            elif not terpicu and status_lama_terpicu:
                status_alarm_terakhir[rule_key] = False 
                
                if target_device is not None and relay is not None:
                    state_normal = "OFF" if perintah_aksi == "ON" else "ON"
                    mqtt_client.publish(f"ras/{target_device}/kontrol/{relay}", state_normal)
                    status_relay_terakhir[f"{target_device}_{relay}"] = state_normal
                    
                    pesan = f"<b>[KONDISI NORMAL]</b>\n\n<b>Kolam:</b> {nama_kolam}\n<b>Info:</b> {nama_sensor_rapi} aman di angka {nilai_sekarang}.\n<b>Aksi:</b> Status {nama_relay_kustom} di alat {target_device} dikembalikan menjadi {state_normal}."
                    kirim_notifikasi_telegram(pesan, user_chat_id)

    except Exception as e:
        print(f"[Error Otomasi] {e}")

# ==========================================
# LOGIKA PENERIMA DATA (MQTT SUBSCRIBER)
# ==========================================
def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Terhubung (Kode: {rc})")
    client.subscribe(MQTT_TOPIC)

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        device_id = msg.topic.split('/')[1] 
        
        device_info = cek_kepemilikan_alat(device_id)
        
        if device_info: 
            try:
                sensors = [k for k in payload.keys() if k in ['suhu', 'ph', 'tds_ppm', 'do_mgL', 'air_penuh']]
                relays = [k for k, v in payload.items() if str(k).startswith('r') and str(k).endswith('_en') and v is True]
                
                if sensors or relays:
                    relay_names = {}
                    if 'r1_nama' in payload: relay_names['r1_en'] = payload['r1_nama']
                    if 'r2_nama' in payload: relay_names['r2_en'] = payload['r2_nama']
                    if 'r3_nama' in payload: relay_names['r3_en'] = payload['r3_nama']
                    if 'r4_nama' in payload: relay_names['r4_en'] = payload['r4_nama']

                    caps = {"sensors": sensors, "relays": relays, "relay_names": relay_names}
                    conn = get_mysql_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE devices SET capabilities = %s WHERE id_device = %s", (json.dumps(caps), device_id))
                    conn.commit()
                    cursor.close()
                    conn.close()
            except Exception as e:
                print(f"[Error Capabilities] {e}")   

            point = Point("sensor_air") \
                .tag("device_id", device_id) \
                .tag("id_kolam", str(device_info['id_kolam']))
            
            if 'suhu' in payload: point.field("suhu", float(payload['suhu']))
            if 'ph' in payload: point.field("ph", float(payload['ph']))
            if 'tds_ppm' in payload: point.field("tds_ppm", float(payload['tds_ppm']))
            if 'do_mgL' in payload: point.field("do_mgL", float(payload['do_mgL']))
            if 'air_penuh' in payload: point.field("air_penuh", bool(payload['air_penuh']))
            if 'r1_en' in payload: point.field("r1_en", bool(payload['r1_en']))
            if 'r2_en' in payload: point.field("r2_en", bool(payload['r2_en']))
            if 'r3_en' in payload: point.field("r3_en", bool(payload['r3_en']))
            if 'r4_en' in payload: point.field("r4_en", bool(payload['r4_en']))
            
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
            cek_dan_eksekusi_threshold(device_id, device_info, payload, client)
    except Exception:
        pass

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start() 

# ==========================================
# REST API ENDPOINTS
# ==========================================

@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.json
    if not data:
        return jsonify({"status": "error", "pesan": "Payload JSON kosong"}), 400
        
    email = data.get('email')
    password = data.get('password')
    
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and user['password_hash'] == password:
            # 1. Buat Token JWT yang berlaku selama 24 jam
            token = jwt.encode({
                'user_id': user['id_user'],
                'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)
            }, app.config['SECRET_KEY'], algorithm="HS256")
            
            # 2. Kembalikan data profil dan token
            return jsonify({
                "status": "sukses", 
                "token": token, 
                "user": {
                    "id": user['id_user'],
                    "nama": user['nama_lengkap'],
                    "telegram_chat_id": user['telegram_chat_id'] or ""
                }
            })
        else:
            return jsonify({"status": "error", "pesan": "Email atau Password Salah!"}), 401
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500
    
@app.route('/api/register', methods=['POST'])
def register_api():
    data = request.json
    if not data:
        return jsonify({"status": "error", "pesan": "Payload JSON kosong"}), 400
        
    nama = data.get('nama_lengkap')
    email = data.get('email')
    password = data.get('password')
    
    if not nama or not email or not password:
        return jsonify({"status": "error", "pesan": "Semua kolom wajib diisi!"}), 400
        
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        
        # 1. Cek apakah email sudah pernah didaftarkan
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({"status": "error", "pesan": "Email sudah terdaftar! Gunakan email lain."}), 400
            
        # 2. Masukkan pengguna baru ke database
        cursor.execute("""
            INSERT INTO users (nama_lengkap, email, password_hash) 
            VALUES (%s, %s, %s)
        """, (nama, email, password))
        
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "sukses", "pesan": "Registrasi berhasil! Silakan login."})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/user/telegram', methods=['POST'])
@token_required
def update_telegram_id():
    chat_id = request.json.get('telegram_chat_id', '').strip()
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        # Menggunakan g.user_id yang disuntikkan oleh token_required
        cursor.execute("UPDATE users SET telegram_chat_id = %s WHERE id_user = %s", (chat_id if chat_id else None, g.user_id))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "sukses", "pesan": "Chat ID Diperbarui!"})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/kontrol', methods=['POST'])
@token_required
def kontrol_relay():
    data = request.json
    device_id = data.get('device_id') 
    relay_no = data.get('relay') 
    perintah = data.get('perintah') 
    
    if not device_id or not relay_no or not perintah:
         return jsonify({"error": "Data tidak lengkap"}), 400
         
    mqtt_client.publish(f"ras/{device_id}/kontrol/{relay_no}", perintah)
    status_relay_terakhir[f"{device_id}_{relay_no}"] = perintah
    return jsonify({"status": "sukses", "pesan": f"Perintah {perintah} dikirim ke {device_id} ({relay_no})"})

@app.route('/api/data-sensor/<int:id_kolam>', methods=['GET'])
@token_required
def get_data_sensor(id_kolam):
    try:
        time_range = request.args.get('range', '1h')
        start = request.args.get('start')
        stop = request.args.get('stop')
        
        if time_range == 'custom' and start and stop:
            query = f'from(bucket: "{INFLUX_BUCKET}") |> range(start: time(v: "{start}"), stop: time(v: "{stop}")) |> filter(fn: (r) => r["id_kolam"] == "{id_kolam}")'
        else:
            valid_ranges = ['15m', '1h', '6h', '12h', '24h', '7d', '30d']
            if time_range not in valid_ranges: time_range = '1h'
            query = f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -{time_range}) |> filter(fn: (r) => r["id_kolam"] == "{id_kolam}")'
        
        result = influx_client.query_api().query(org=INFLUX_ORG, query=query)
        data_points = []
        for table in result:
            for record in table.records:
                data_points.append({
                    "device_id": record.values.get("device_id"), 
                    "waktu": record.get_time().isoformat(),
                    "field": record.get_field(),
                    "nilai": record.get_value()
                })
        return jsonify(data_points)
    except Exception as e:
        print(f"[Error Influx] {e}")
        return jsonify([]), 500

@app.route('/api/aturan/<int:id_kolam>', methods=['GET'])
@token_required
def get_aturan(id_kolam):
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM aturan_threshold WHERE id_kolam = %s", (id_kolam,))
        aturan = cursor.fetchall()
        
        cursor.execute("SELECT id_device, capabilities FROM devices WHERE id_kolam = %s", (id_kolam,))
        devices = cursor.fetchall()
        
        cursor.close()
        conn.close()
        return jsonify({"aturan": aturan, "devices": devices})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/aturan', methods=['POST'])
@token_required
def tambah_aturan():
    data = request.json
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO aturan_threshold 
            (id_kolam, trigger_device_id, sensor, jenis_batas, nilai_batas, target_device_id, target_relay, perintah) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data['id_kolam'], data['trigger_device_id'], data['sensor'], data['jenis_batas'], 
            float(data['nilai_batas']), data.get('target_device_id'), data.get('target_relay'),
            data.get('perintah', 'ON')
        ))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "sukses", "pesan": "Aturan lintas-alat berhasil disimpan!"})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/aturan/<int:id_aturan>', methods=['DELETE'])
@token_required
def hapus_aturan(id_aturan):
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM aturan_threshold WHERE id_aturan = %s", (id_aturan,))
        conn.commit()
        cursor.close()
        conn.close()
        
        keys_to_delete = [k for k in status_alarm_terakhir if k.endswith(f"_{id_aturan}")]
        for k in keys_to_delete: del status_alarm_terakhir[k]
        return jsonify({"status": "sukses", "pesan": "Aturan dihapus"})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/kolam-list', methods=['GET'])
@token_required
def get_kolam_list():
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id_kolam, nama_kolam FROM kolam WHERE id_user = %s", (g.user_id,))
        data = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(data)
    except:
        return jsonify([])

@app.route('/api/user-devices', methods=['GET'])
@token_required
def get_user_devices():
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT d.id_device, d.id_kolam, k.nama_kolam
            FROM devices d
            JOIN kolam k ON d.id_kolam = k.id_kolam
            WHERE k.id_user = %s
        """, (g.user_id,))
        data = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(data)
    except:
        return jsonify([])

@app.route('/api/tambah-alat', methods=['POST'])
@token_required
def tambah_alat():
    data = request.json
    device_id = data.get('device_id')
    id_kolam_req = data.get('id_kolam') 
    nama_kolam_baru = data.get('nama_kolam_baru')
    user_id = g.user_id
    
    if not device_id:
         return jsonify({"status": "error", "pesan": "ID Alat tidak boleh kosong"}), 400
         
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        
        if id_kolam_req == 'new':
            if not nama_kolam_baru:
                return jsonify({"status": "error", "pesan": "Nama kolam baru wajib diisi"}), 400
            cursor.execute("INSERT INTO kolam (id_user, nama_kolam) VALUES (%s, %s)", (user_id, nama_kolam_baru))
            id_kolam = cursor.lastrowid
        else:
            id_kolam = id_kolam_req
            
        cursor.execute("""
            INSERT INTO devices (id_device, id_kolam) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE id_kolam = %s
        """, (device_id, id_kolam, id_kolam))
        
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "sukses", "pesan": f"Alat {device_id} berhasil dihubungkan ke kolam!"})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/pindah-alat', methods=['POST'])
@token_required
def pindah_alat():
    data = request.json
    device_id = data.get('device_id')
    id_kolam_baru = data.get('id_kolam_baru')

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE devices SET id_kolam = %s 
            WHERE id_device = %s 
            AND id_kolam IN (SELECT id_kolam FROM kolam WHERE id_user = %s)
        """, (id_kolam_baru, device_id, g.user_id))
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        conn.close()
        
        if affected > 0:
            return jsonify({"status": "sukses", "pesan": "Alat berhasil dipindahkan ke kolam baru!"})
        else:
            return jsonify({"status": "error", "pesan": "Gagal memindah alat. Hak akses ditolak."})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

@app.route('/api/hapus-alat/<device_id>', methods=['DELETE'])
@token_required
def hapus_alat(device_id):
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM devices 
            WHERE id_device = %s 
            AND id_kolam IN (SELECT id_kolam FROM kolam WHERE id_user = %s)
        """, (device_id, g.user_id))
        conn.commit()
        affected = cursor.rowcount
        cursor.close()
        conn.close()
        
        if affected > 0:
            return jsonify({"status": "sukses", "pesan": "Alat berhasil dihapus permanen dari sistem!"})
        else:
            return jsonify({"status": "error", "pesan": "Gagal menghapus. Alat tidak ditemukan."})
    except Exception as e:
        return jsonify({"status": "error", "pesan": str(e)}), 500

if __name__ == '__main__':
    print("====================================")
    print(" SERVER RAS PURE REST API MENYALA ")
    print("====================================")
    app.run(host='0.0.0.0', port=5000, debug=False)