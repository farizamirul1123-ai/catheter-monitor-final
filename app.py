from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from datetime import datetime, timedelta
import requests 
import os # Import os untuk baca environment variables

app = Flask(__name__)
CORS(app) 

# ==================================
# 1. POSTGRES CONFIGURATION (Mendukung Local dan Cloud)
# ==================================

def get_db_connection():
    """Menyambung ke PostgreSQL menggunakan pembolehubah persekitaran (untuk Awan) atau konfigurasi tempatan."""
    
    # 1. CUBA GUNA CLOUD DB URL (dipanggil 'DATABASE_URL' di Render/Heroku)
    database_url = os.environ.get('DATABASE_URL')
    
    if database_url:
        print("🌐 Connecting to Cloud PostgreSQL...")
        # psycopg2 boleh menggunakan URL penuh untuk sambungan
        return psycopg2.connect(database_url)
    else:
        # 2. FAILBACK KE LOCAL DB (untuk testing di komputer anda)
        print("🏠 Connecting to Local PostgreSQL...")
        return psycopg2.connect(
            dbname="catheter_db", 
            user="postgres", 
            password="Safiah_2706", # <--- GANTI INI
            host="localhost", 
            port="5432"
        )

# ==================================
# 2. TELEGRAM AND CONFIGURATION FUNCTIONS
# ==================================
# (Fungsi-fungsi ini kekal seperti sedia ada)
def get_config(conn, key):
    """Retrieves configuration value from global_config table."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM global_config WHERE key = %s;", (key,))
        result = cur.fetchone()
        return result[0] if result else None
    finally:
        cur.close()

def update_config(conn, key, value):
    """Updates configuration value."""
    cur = conn.cursor()
    try:
        cur.execute("UPDATE global_config SET value = %s WHERE key = %s;", (value, key))
        conn.commit()
    finally:
        cur.close()

def send_telegram_message(token, chat_id, message):
    """Sends message via Telegram Bot API."""
    if not token or not chat_id:
        print("🔔 Telegram config missing. Skipping notification.")
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        print("🔔 Telegram notification sent.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ Error sending Telegram: {e}")
        return False

# ==================================
# 3. DEVICE API ENDPOINT (ESP32)
# ==================================

@app.route('/api/v1/log_data', methods=['POST'])
def log_data():
    """Receives data from ESP32 (HTTP POST) and logs it to patient_data."""
    
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No data received"}), 400

    # Data Extraction and cleaning for safety
    try:
        weight = float(data.get('weight_kg', 0.0))
        status = data.get('status_message', 'Data Received')
        alert = data.get('alert_level', 'NONE')
        buzzer = data.get('buzzer_status', 'OFF')
        pyuria_status = data.get('pyuria_detected', False) 
        pyuria_conf = float(data.get('pyuria_confidence', 0.0)) 
        
        if isinstance(buzzer, str) and ',' in buzzer:
             buzzer = buzzer.split(',')[-1].strip()

    except ValueError:
        return jsonify({"status": "error", "message": "Invalid number format in input data"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # INSERT patient_data
        sql_insert = """
        INSERT INTO patient_data (
            timestamp_utc, urine_weight_kg, status_message, alert_level, 
            pyuria_status, pyuria_confidence, buzzer_status
        ) VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        """
        cur.execute(sql_insert, (weight, status, alert, pyuria_status, pyuria_conf, buzzer))
        conn.commit()
        print(f"✅ New data recorded to PG: {weight} kg, Pyuria: {pyuria_status}")
        
        # =========================================================
        # TELEGRAM PUSH LOGIC (SERVER-SIDE)
        # =========================================================
        token = get_config(conn, 'TELEGRAM_BOT_TOKEN')
        chat_id = get_config(conn, 'TELEGRAM_CHAT_ID')

        # 1. PYURIA ALERT LOGIC
        if pyuria_status == True and pyuria_conf >= 0.70:
            last_alert = get_config(conn, 'LAST_PYURIA_ALERT_SENT')
            if last_alert != 'PYURIA_ALERT_SENT':
                message = f"<b>🚨 PYURIA ALERT DETECTED!</b>\n" \
                          f"Immediate action required to prevent infection.\n" \
                          f"AI Confidence: {(pyuria_conf * 100):.1f}%"
                send_telegram_message(token, chat_id, message)
                update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'PYURIA_ALERT_SENT')
        elif get_config(conn, 'LAST_PYURIA_ALERT_SENT') == 'PYURIA_ALERT_SENT' and pyuria_status == False:
            update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'NONE')
        
        # 2. WEIGHT ALERT LOGIC (1.0 KG / 1.5 KG)
        if weight >= 1.0:
            last_weight_alert = get_config(conn, 'LAST_WEIGHT_ALERT_SENT')
            
            if weight >= 1.5 and last_weight_alert != 'WEIGHT_1500_SENT':
                message = f"<b>🚨🚨 CRITICAL LIMIT (1.5 KG) REACHED!</b>\n" \
                          f"Current Weight: {weight:.2f} kg\n" \
                          f"Change catheter IMMEDIATELY!"
                send_telegram_message(token, chat_id, message)
                update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'WEIGHT_1500_SENT')
                
            elif weight >= 1.0 and weight < 1.5 and last_weight_alert not in ['WEIGHT_1000_SENT', 'WEIGHT_1500_SENT']:
                 message = f"<b>⚠️ WEIGHT WARNING (1.0 KG)</b>\n" \
                           f"Current Weight: {weight:.2f} kg\n" \
                           f"Catheter change is due soon."
                 send_telegram_message(token, chat_id, message)
                 update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'WEIGHT_1000_SENT')
        
        elif weight < 0.5 and get_config(conn, 'LAST_WEIGHT_ALERT_SENT') != 'NONE':
             update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'NONE')

        return jsonify({"status": "success", "message": "Data logged, alerts processed"}), 200

    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ CRITICAL ERROR in log_data (HTTP 500): {e}") 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

# ==================================
# 4. DASHBOARD API ENDPOINTS
# ==================================

@app.route('/', methods=['GET'])
def index():
    """Menghidangkan fail index.html dari server Flask."""
    return open('index.html', 'r').read()


@app.route('/api/v1/status', methods=['GET'])
def get_status_data():
    """Retrieves latest metrics and history for the main dashboard."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get Latest Metrics
        cur.execute("""
            SELECT urine_weight_kg, status_message, alert_level, buzzer_status, 
                   pyuria_status, pyuria_confidence
            FROM patient_data 
            ORDER BY log_id DESC LIMIT 1;
        """)
        latest_data = cur.fetchone()
        
        # Get History (50 records)
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM timestamp_utc) * 1000 AS timestamp_ms, 
                   urine_weight_kg, pyuria_confidence
            FROM patient_data 
            ORDER BY log_id DESC LIMIT 50;
        """)
        history = cur.fetchall()
        
        # Get Maintenance Count
        cur.execute("SELECT COUNT(*) FROM catheter_maintenance;")
        change_count = cur.fetchone()[0]
        
        # Get Change Logs
        cur.execute("""
            SELECT change_id, timestamp_utc 
            FROM catheter_maintenance
            ORDER BY change_id DESC LIMIT 10;
        """)
        change_logs = cur.fetchall()
        
        if latest_data:
            buzzer_status_raw = latest_data[3]
            buzzer_status_clean = buzzer_status_raw.split(',')[-1].strip() if buzzer_status_raw else "OFF"
        else:
            buzzer_status_clean = "OFF"

        latest_metrics = {
            "weight_kg": float(latest_data[0]) if latest_data else 0.0,
            "status_message": latest_data[1] if latest_data else "No Data",
            "alert_level": latest_data[2] if latest_data else "NONE",
            "buzzer_status": buzzer_status_clean,
            "pyuria_status": latest_data[4] if latest_data else False,
            "pyuria_confidence": float(latest_data[5]) if latest_data else 0.0,
            "change_count": change_count
        }

        # Format history data
        weight_history_formatted = []
        for row in history:
            timestamp_ms = int(row[0])
            weight_history_formatted.append({
                "timestamp": timestamp_ms,
                "weight": float(row[1]),
                "pyuria_conf": float(row[2])
            })

        data = {
            "current_metrics": latest_metrics,
            "weight_history": weight_history_formatted,
            "change_logs": [{
                "id": row[0],
                "timestamp": row[1].isoformat()
            } for row in change_logs]
        }
        
        return jsonify(data), 200

    except Exception as e:
        print(f"❌ Error fetching status data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

# -----------------------------------------------
# WEEKLY STATISTICS ENDPOINT (Dummy data untuk kestabilan)
# -----------------------------------------------

@app.route('/api/v1/weekly_stats', methods=['GET'])
def get_weekly_stats():
    """Retrieves dummy weekly statistics for dashboard stability (kerana graph mingguan dibuang)."""
    return jsonify({
        "weight_accumulation": [{"day": "Mon", "total_weight_kg": 0.0}],
        "pyuria_detection": [{"day": "Mon", "pyuria_count": 0, "normal_count": 0}]
    }), 200


# ==================================
# 5. DASHBOARD CONTROL ENDPOINTS
# ==================================

@app.route('/api/v1/control_buzzer', methods=['POST'])
def control_buzzer():
    """Receives Buzzer commands (ON/OFF/RESET) from the dashboard."""
    data = request.json
    command = data.get('command')
    
    if command not in ['ON', 'OFF', 'RESET']:
        return jsonify({"status": "error", "message": "Invalid command"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        if command == 'RESET':
            cur.execute("""
                INSERT INTO patient_data (
                    urine_weight_kg, status_message, alert_level, buzzer_status, 
                    pyuria_status, pyuria_confidence
                ) VALUES (0.0, 'System Reset by User', 'NONE', 'OFF', FALSE, 0.0);
            """)
            update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'NONE')
            update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'NONE')
        
        cur.execute("UPDATE patient_data SET buzzer_status = %s WHERE log_id = (SELECT log_id FROM patient_data ORDER BY log_id DESC LIMIT 1);", (command,))
        
        conn.commit()
        return jsonify({"status": "success", "message": f"Buzzer command '{command}' logged."}), 200

    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Error in control_buzzer: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/v1/log_change', methods=['POST'])
def log_catheter_change():
    """Logs catheter change event and resets weight."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("INSERT INTO catheter_maintenance (timestamp_utc) VALUES (NOW());")
        
        cur.execute("""
            INSERT INTO patient_data (
                urine_weight_kg, status_message, alert_level, buzzer_status, 
                pyuria_status, pyuria_confidence
            ) VALUES (0.0, 'Catheter Changed', 'NONE', 'OFF', FALSE, 0.0);
        """)

        update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'NONE')
        update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'NONE')
        
        conn.commit()
        return jsonify({"status": "success", "message": "Catheter change logged and weight reset."}), 200
        
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Error in log_catheter_change: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

# ==================================
# MAIN PROGRAM
# ==================================
if __name__ == '__main__':
    # Flask akan menghidangkan index.html secara automatik di root (/)
    # Di cloud, gunicorn akan mengendalikan port.
    app.run(host='0.0.0.0', port=5000, debug=True)