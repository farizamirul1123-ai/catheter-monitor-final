from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import psycopg2
from datetime import datetime, timedelta
import requests 
import os 
import io
import csv

app = Flask(__name__)
CORS(app) 

# ==================================
# 1. POSTGRES CONFIGURATION (Mendukung Local dan Cloud)
# ==================================

def get_db_connection():
    """Menyambung ke PostgreSQL menggunakan pembolehubah persekitaran (untuk Awan) atau konfigurasi tempatan."""
    database_url = os.environ.get('DATABASE_URL')
    
    if database_url:
        # print("🌐 Connecting to Cloud PostgreSQL...") # Optional: Boleh comment out untuk kurangkan log
        return psycopg2.connect(database_url)
    else:
        # print("🏠 Connecting to Local PostgreSQL...")
        return psycopg2.connect(
            dbname="catheter_db", 
            user="postgres", 
            password="Safiah_2706", 
            host="localhost", 
            port="5432"
        )

# ==================================
# 2. TELEGRAM AND CONFIGURATION FUNCTIONS
# ==================================
def get_config(conn, key):
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM global_config WHERE key = %s;", (key,))
        result = cur.fetchone()
        return result[0] if result else None
    finally:
        cur.close()

def update_config(conn, key, value):
    cur = conn.cursor()
    try:
        cur.execute("UPDATE global_config SET value = %s WHERE key = %s;", (value, key))
        conn.commit()
    finally:
        cur.close()

def send_telegram_message(token, chat_id, message):
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
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No data received"}), 400

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
        
        sql_insert = """
        INSERT INTO patient_data (
            timestamp_utc, urine_weight_kg, status_message, alert_level, 
            pyuria_status, pyuria_confidence, buzzer_status
        ) VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        """
        cur.execute(sql_insert, (weight, status, alert, pyuria_status, pyuria_conf, buzzer))
        conn.commit()
        
        # TELEGRAM LOGIC
        token = get_config(conn, 'TELEGRAM_BOT_TOKEN')
        chat_id = get_config(conn, 'TELEGRAM_CHAT_ID')

        # 1. PYURIA ALERT
        if pyuria_status == True and pyuria_conf >= 0.70:
            last_alert = get_config(conn, 'LAST_PYURIA_ALERT_SENT')
            if last_alert != 'PYURIA_ALERT_SENT':
                message = f"<b>🚨 PYURIA ALERT DETECTED!</b>\nAI Confidence: {(pyuria_conf * 100):.1f}%"
                send_telegram_message(token, chat_id, message)
                update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'PYURIA_ALERT_SENT')
        elif get_config(conn, 'LAST_PYURIA_ALERT_SENT') == 'PYURIA_ALERT_SENT' and pyuria_status == False:
            update_config(conn, 'LAST_PYURIA_ALERT_SENT', 'NONE')
        
        # 2. WEIGHT ALERT
        if weight >= 1.0:
            last_weight_alert = get_config(conn, 'LAST_WEIGHT_ALERT_SENT')
            
            if weight >= 1.5 and last_weight_alert != 'WEIGHT_1500_SENT':
                message = f"<b>🚨🚨 CRITICAL LIMIT (1.5 KG) REACHED!</b>\nCurrent Weight: {weight:.2f} kg\nChange catheter IMMEDIATELY!"
                send_telegram_message(token, chat_id, message)
                update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'WEIGHT_1500_SENT')
                
            elif weight >= 1.0 and weight < 1.5 and last_weight_alert not in ['WEIGHT_1000_SENT', 'WEIGHT_1500_SENT']:
                 message = f"<b>⚠️ WEIGHT WARNING (1.0 KG)</b>\nCurrent Weight: {weight:.2f} kg"
                 send_telegram_message(token, chat_id, message)
                 update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'WEIGHT_1000_SENT')
        
        elif weight < 0.5 and get_config(conn, 'LAST_WEIGHT_ALERT_SENT') != 'NONE':
             update_config(conn, 'LAST_WEIGHT_ALERT_SENT', 'NONE')

        return jsonify({"status": "success", "message": "Data logged"}), 200

    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ CRITICAL ERROR: {e}") 
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

# ==================================
# 4. DASHBOARD API ENDPOINTS
# ==================================

@app.route('/', methods=['GET'])
def index():
    return open('index.html', 'r').read()

@app.route('/api/v1/status', methods=['GET'])
def get_status_data():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Latest Metrics
        cur.execute("""
            SELECT urine_weight_kg, status_message, alert_level, buzzer_status, 
                   pyuria_status, pyuria_confidence
            FROM patient_data 
            ORDER BY log_id DESC LIMIT 1;
        """)
        latest_data = cur.fetchone()
        
        # History (50 records)
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM timestamp_utc) * 1000 AS timestamp_ms, 
                   urine_weight_kg, pyuria_confidence
            FROM patient_data 
            ORDER BY log_id DESC LIMIT 50;
        """)
        history = cur.fetchall()
        
        # Maintenance Count
        cur.execute("SELECT COUNT(*) FROM catheter_maintenance;")
        change_count = cur.fetchone()[0]
        
        # Change Logs
        cur.execute("""
            SELECT change_id, EXTRACT(EPOCH FROM timestamp_utc) * 1000
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

        weight_history_formatted = []
        for row in history:
            weight_history_formatted.append({
                "timestamp": int(row[0]),
                "weight": float(row[1]),
                "pyuria_conf": float(row[2])
            })

        data = {
            "current_metrics": latest_metrics,
            "weight_history": weight_history_formatted,
            "change_logs": [{
                "id": row[0],
                "timestamp": int(row[1]) 
            } for row in change_logs]
        }
        
        return jsonify(data), 200

    except Exception as e:
        print(f"❌ Error fetching status data: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/v1/weekly_stats', methods=['GET'])
def get_weekly_stats():
    return jsonify({
        "weight_accumulation": [{"day": "Mon", "total_weight_kg": 0.0}],
        "pyuria_detection": [{"day": "Mon", "pyuria_count": 0, "normal_count": 0}]
    }), 200

@app.route('/api/v1/control_buzzer', methods=['POST'])
def control_buzzer():
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
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/v1/log_change', methods=['POST'])
def log_catheter_change():
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
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

# ==================================
# 6. NEW MAINTENANCE FUNCTIONS (CLEAR & EXPORT)
# ==================================

@app.route('/api/v1/clear_maintenance_log', methods=['POST'])
def clear_maintenance_log():
    """
    DANGER: Clears ALL records from catheter_maintenance table.
    Resets ID counter to 1.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. DELETE ALL DATA
        cur.execute("DELETE FROM catheter_maintenance;")
        
        # 2. RESET ID SEQUENCE (Supaya mula dari 1 balik)
        # Nota: Sequence name biasanya 'catheter_maintenance_change_id_seq'.
        # Kalau error, mungkin nama sequence lain, tapi ini default PostgreSQL.
        cur.execute("ALTER SEQUENCE catheter_maintenance_change_id_seq RESTART WITH 1;")
        
        conn.commit()
        print("⚠️ MAINTENANCE LOG CLEARED BY USER")
        return jsonify({"status": "success", "message": "All maintenance logs cleared."}), 200
        
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Error clearing log: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/v1/export_maintenance_log', methods=['GET'])
def export_maintenance_log():
    """
    Exports catheter_maintenance table as a CSV file for download.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Select data
        cur.execute("SELECT change_id, timestamp_utc FROM catheter_maintenance ORDER BY change_id ASC;")
        rows = cur.fetchall()
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Add Header
        writer.writerow(['ID', 'Timestamp (UTC)', 'Date', 'Time'])
        
        # Add Data rows
        for row in rows:
            ts = row[1]
            # Format tarikh dan masa untuk mudah dibaca dalam Excel
            date_str = ts.strftime('%Y-%m-%d')
            time_str = ts.strftime('%H:%M:%S')
            writer.writerow([row[0], ts, date_str, time_str])
            
        # Return as downloadable file
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=maintenance_log.csv"}
        )
        
    except Exception as e:
        print(f"❌ Error exporting log: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn: conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)