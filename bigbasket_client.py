#!/usr/bin/env python3
"""
BigBasket Client - with silent mode for clean output
Proxy DISABLED for better performance
"""
import tls_client, json, random, time, uuid, base64, urllib.parse
from typing import Dict, Optional, List

# ============================================================
# PROXY - DISABLED (set to None to disable)
# ============================================================
PROXY = None  # Set to None to disable proxy

class BigBasketClient:
    def __init__(self, silent=False):
        self.silent = silent
        self.session = tls_client.Session(
            client_identifier="chrome_120",
            random_tls_extension_order=True
        )
        
        if PROXY:
            self.session.proxies = {'http': PROXY, 'https': PROXY}
        else:
            self.session.proxies = {}
        
        self.session.headers.update({
            'accept': 'application/json, text/plain, */*',
            'accept-encoding': 'gzip, deflate, br',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'sec-ch-ua': '"Google Chrome";v="120", "Not:A-Brand";v="8", "Chromium";v="120"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'BB Android/v8.35.0/os 15',
            'x-channel': 'BB-Android',
            'x-entry-context': 'bb-b2c',
            'x-tcp-platform': 'native',
            'x-tcp-device-version': 'android_8.35.0_25113510',
            'common-client-static-version': '104',
            'x-device-model': 'Samsung SM-A536E',
            'x-is-debug': 'false',
            'x-pharma': 'true',
            'x-integrated-fc-door-visible': 'true',
            'x-bucket-id': '50',
        })
        self.lat = None; self.lng = None; self.pincode = None; self.area = None
        self.city_id = "1"; self.actual_city_id = "18"
        self.device_id = None; self.bb_token = None; self.ref_id = None
        self.visitor_id = None; self.m_id = None; self.address_id = None
        self.sa_ids = None; self.member_id = None
        self.cart_items = []; self.product_list = []
        self.csurftoken = None; self.current_address_id = None
        self.wallet_details = None; self.last_otp_error = None
        self.po_id = None; self.cart_summary = {}
        self.is_checkout_allowed = False
        self.bb_txn_id = None
        self.first_name = None; self.last_name = None
        self.contact_number = None; self.house_no = None
        self.apartment_name = None; self.landmark = None
        self.addresses = []
        self.is_partial_address = False
        self.address_set_skipped = False

    def _print(self, *args, **kwargs):
        if not self.silent:
            print(*args, **kwargs)

    def _request_with_retry(self, method, url, **kwargs):
        for attempt in range(3):
            if attempt > 0:
                self._print(f"   ⏳ Retry {attempt}/2 after 429...")
                time.sleep(2)
            request_func = getattr(self.session, method.lower())
            response = request_func(url, **kwargs)
            if response.status_code != 429:
                return response
            self._print(f"   ⚠️ Got 429, retrying...")
        return response

    def _verify_otp_with_retry(self, url, payload):
        for attempt in range(5):
            if attempt > 0:
                self._print(f"   ⏳ Retry {attempt}/4 after 429 (5 sec delay)...")
                time.sleep(5)
            response = self.session.post(url, json=payload)
            if response.status_code != 429:
                return response
            self._print(f"   ⚠️ Got 429, retrying...")
        return response

    def set_location(self, lat, lng, pincode, area):
        self.lat = str(lat); self.lng = str(lng); self.pincode = str(pincode); self.area = area

    def generate_device_id(self):
        return ''.join(random.choices('0123456789abcdef', k=16))

    def generate_tracker(self):
        return str(uuid.uuid4())

    def _encode_coords(self, lat=None, lng=None):
        if lat is None: lat = self.lat
        if lng is None: lng = self.lng
        coords = f"{lat}|{lng}"
        return base64.b64encode(coords.encode()).decode()

    def _set_cookie_safe(self, name, value):
        try:
            if name in self.session.cookies:
                del self.session.cookies[name]
        except:
            pass
        if value is not None:
            self.session.cookies.set(name, value)

    def _update_cookies(self, response):
        if hasattr(response, 'cookies'):
            for key, value in response.cookies.items():
                self._set_cookie_safe(key, value)
                if key == 'csurftoken':
                    self.csurftoken = value

    def refresh_session_cookies(self):
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
            sa_ids = self.session.cookies.get('_bb_sa_ids')
            if sa_ids: self.sa_ids = sa_ids
            return True
        except Exception as e:
            self._print(f"   ⚠️ Refresh error: {e}")
            return False

    def register_device(self):
        self.device_id = self.generate_device_id()
        existing_vid = self.session.cookies.get('_bb_vid')
        if existing_vid:
            self.visitor_id = existing_vid
            self._print(f"✅ Using existing visitor_id: {self.visitor_id[:12]}...")
            return True
        
        payload = {
            "imei": "02:00:00:00:00:00",
            "device_id": self.device_id,
            "city_id": "1",
            "properties": json.dumps({
                "platform": "java", "os_name": "android", "os_version": "15",
                "app_version": "8.35.0", "device_make": "Samsung", "device_model": "SM-A536E",
                "screen_resolution": "900X1600", "screen_dpi": 240
            })
        }
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/mapi/v4.2.0/register/device/', data=payload)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                if 'response' in data and 'visitor_id' in data['response']:
                    self.visitor_id = data['response']['visitor_id']
                elif '_bb_vid' in response.cookies:
                    self.visitor_id = response.cookies['_bb_vid']
                else:
                    self.visitor_id = str(int(time.time() * 1000)) + str(uuid.uuid4().hex[:8])
                self._print(f"✅ Device registered: {self.device_id[:8]}...")
                return True
            else:
                self.visitor_id = str(int(time.time() * 1000)) + str(uuid.uuid4().hex[:8])
                self._print(f"⚠️ Registration returned {response.status_code}, using fallback visitor_id")
                return True
        except Exception as e:
            self.visitor_id = str(int(time.time() * 1000)) + str(uuid.uuid4().hex[:8])
            self._print(f"⚠️ Register error: {e}, using fallback visitor_id")
            return True

    def load_ui_data(self):
        endpoints = [
            ("header", 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true&send_pseudo_door=true&send_order_restriction_enabled_door=true&app_launch=true&enable-pharma-door=true'),
            ("app data", 'https://www.bigbasket.com/ui-svc/v1/app-data?os_name=android&app_version=8.35.0'),
            ("door data", 'https://www.bigbasket.com/ui-svc/v1/door-data?lob_required=true'),
            ("health check", 'https://www.bigbasket.com/service/healthcheck.html')
        ]
        for name, url in endpoints:
            try:
                response = self._request_with_retry('GET', url)
                self._update_cookies(response)
                if response.status_code != 200:
                    self._print(f"   ⚠️ {name} failed: {response.status_code}")
            except:
                pass
            time.sleep(0.3)
        return True

    def update_device_info(self):
        self.session.headers.update({'content-type': 'application/x-www-form-urlencoded'})
        payload = {"ad_id": "3e0d3f64-3657-40af-9095-c0f4fc692d8e"}
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/mapi/v4.2.0/update/device/info/', data=payload)
            self._update_cookies(response)
            return True
        except:
            return True

    def request_otp(self, mobile):
        self.session.headers.update({'content-type': 'application/json'})
        payload = {"identifier": mobile, "referrer": "unified_login"}
        
        old_proxy = self.session.proxies
        self.session.proxies = {}
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/member-tdl/v3/member/otp/', json=payload)
            self.session.proxies = old_proxy
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                if 'refId' in data:
                    self.ref_id = data['refId']
                    self.last_otp_error = None
                    self._print(f"   ✅ OTP sent! RefID: {self.ref_id[:8]}...")
                    return True
                else:
                    self.last_otp_error = "Missing refId"
                    return False
            else:
                error_msg = ""
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict):
                        errors = error_data.get("errors", [])
                        if errors:
                            error_msg = errors[0].get("msg") or errors[0].get("display_msg") or ""
                except:
                    pass
                self.last_otp_error = error_msg
                self._print(f"   ❌ OTP failed: {response.status_code} - {error_msg}")
                return False
        except Exception as e:
            self.session.proxies = old_proxy
            self.last_otp_error = str(e)
            self._print(f"   ❌ OTP error: {e}")
            return False

    def verify_otp(self, mobile, otp):
        payload = {"mobile_no": mobile, "mobile_no_otp": otp, "refId": self.ref_id}
        old_proxy = self.session.proxies
        self.session.proxies = {}
        try:
            response = self._verify_otp_with_retry('https://www.bigbasket.com/member-tdl/v3/member/unified-login/', payload)
            self.session.proxies = old_proxy
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                if 'bb_token' in data:
                    self.bb_token = data['bb_token']
                    self._set_cookie_safe('BBAUTHTOKEN', self.bb_token)
                    self._print(f"   ✅ Login successful!")
                if 'visitor_id' in data:
                    self.visitor_id = data['visitor_id']
                if 'm_id' in data:
                    self.m_id = data['m_id']
                self.csurftoken = self.session.cookies.get('csurftoken')
                return True
            else:
                self._print(f"   ❌ Login failed: {response.status_code}")
                return False
        except Exception as e:
            self.session.proxies = old_proxy
            self._print(f"   ❌ Verify error: {e}")
            return False

    def get_wallet_details(self):
        if not self.bb_token:
            self._print("   ❌ No bb_token")
            return None
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except:
            pass
        
        self.session.headers.update({
            'content-type': 'application/json',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true'
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        
        self._set_cookie_safe('_bb_source', 'app')
        self._set_cookie_safe('_bb_vid', self.visitor_id)
        self._set_cookie_safe('_bb_mid', self.m_id)
        if self.sa_ids:
            self._set_cookie_safe('_bb_sa_ids', self.sa_ids)
        
        old_proxy = self.session.proxies
        self.session.proxies = {}
        try:
            response = self._request_with_retry('GET', 'https://www.bigbasket.com/wallet/v1/details')
            self.session.proxies = old_proxy
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                self.wallet_details = data
                balance = data.get('current_wallet_balance', 0)
                self._print(f"   💰 Wallet: ₹{balance}")
                return data
            return None
        except Exception as e:
            self.session.proxies = old_proxy
            self._print(f"   ❌ Wallet error: {e}")
            return None

    def get_free_cash(self):
        if not self.bb_token:
            return None
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except:
            pass
        
        headers = {
            'content-type': 'application/json',
            'x-retry': '0',
            'x-tcp-device-version': 'android_8.38.0_25115710',
            'common-client-static-version': '105',
            'x-bucket-id': '36',
            'x-channel': 'BB-Android',
            'x-tracker': self.generate_tracker(),
            'x-entry-context': 'bb-b2c',
            'x-entry-context-id': '100'
        }
        if self.csurftoken:
            headers['x-csurftoken'] = self.csurftoken
        
        payload = {
            "freecash_v2_enabled": True,
            "sa_city_ids": [int(self.actual_city_id)],
            "sa_ids": [int(self.sa_ids) if self.sa_ids else 28425],
            "context": "homepage",
            "page_type": None,
            "channel": "BB-Android"
        }
        
        old_proxy = self.session.proxies
        self.session.proxies = {}
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/ui-svc/v1/free-cash/', json=payload, headers=headers)
            self.session.proxies = old_proxy
            if response.status_code == 200:
                data = response.json()
                freecash = data.get('total_freecash_amount', 0)
                self._print(f"   🎯 FreeCash: ₹{freecash}")
                return data
            return None
        except:
            self.session.proxies = old_proxy
            return None

    # ============================================
    # ADDRESS MANAGEMENT METHODS
    # ============================================
    def get_address_list(self) -> Optional[List[Dict]]:
        self._print(f"\n📋 Getting address list")
        if self.bb_token:
            self._set_cookie_safe('BBAUTHTOKEN', self.bb_token)
        else:
            return None
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except Exception as e:
            self._print(f"   ⚠️ Refresh error: {e}")
        
        self.session.headers.update({
            'x-retry': '0', 'content-type': 'application/json', 'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true', 'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        
        self._set_cookie_safe('_bb_source', 'app')
        self._set_cookie_safe('_bb_vid', self.visitor_id)
        self._set_cookie_safe('_bb_mid', self.m_id)
        if self.csurftoken:
            self._set_cookie_safe('csurftoken', self.csurftoken)
        
        url = 'https://www.bigbasket.com/ui-svc/v1/address/list?address_type=1&skip_partial=1&is3plAddressesNeeded=true'
        try:
            response = self._request_with_retry('GET', url)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    addresses = data
                elif isinstance(data, dict):
                    addresses = data.get('addresses', data.get('response', []))
                else:
                    addresses = []
                if not isinstance(addresses, list):
                    addresses = []
                self.addresses = addresses
                return addresses
            return None
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return None

    def set_current_delivery_address(self, address_id: int) -> bool:
        self._print(f"\n📍 Setting address ID {address_id} as current delivery address")
        if self.bb_token:
            self._set_cookie_safe('BBAUTHTOKEN', self.bb_token)
        else:
            return False
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except Exception as e:
            self._print(f"   ⚠️ Refresh error: {e}")
        
        self.session.headers.update({
            'x-retry': '0', 'content-type': 'application/json', 'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true', 'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        
        self._set_cookie_safe('_bb_source', 'app')
        self._set_cookie_safe('_bb_vid', self.visitor_id)
        self._set_cookie_safe('_bb_mid', self.m_id)
        if self.csurftoken:
            self._set_cookie_safe('csurftoken', self.csurftoken)
        
        payload = {"address_id": int(address_id), "return_hub_cookies": False}
        try:
            response = self._request_with_retry('PUT', 'https://www.bigbasket.com/member-svc/v2/member/current-delivery-address/', json=payload)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                self.current_address_id = data.get('address_id')
                self.member_id = data.get('member_id')
                self.refresh_session_cookies()
                return True
            return False
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    def set_address_with_user_details(self) -> bool:
        self._print("\n" + "="*60)
        self._print(f"📍 STEP: SET ADDRESS")
        self._print("="*60)
        if not self.bb_token:
            self._print(f"   ❌ No bb_token! Cannot proceed.")
            return False

        self._print("   🔄 Refreshing csurftoken...")
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except Exception as e:
            self._print(f"      ⚠️ Refresh error: {e}")

        self.session.headers.update({
            'x-retry': '0',
            'content-type': 'application/json',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken

        self._set_cookie_safe('_bb_source', 'app')
        self._set_cookie_safe('_bb_vid', self.visitor_id)
        self._set_cookie_safe('_bb_mid', self.m_id)
        self._set_cookie_safe('_bb_cid', '1')
        if self.lat and self.lng:
            self._set_cookie_safe('_bb_lat_long', self._encode_coords())
        if self.csurftoken:
            self._set_cookie_safe('csurftoken', self.csurftoken)

        self._print("   📋 Checking serviceability...")
        if self.lat and self.lng:
            service_url = (f'https://www.bigbasket.com/ui-svc/v1/serviceable/'
                          f'?lat={self.lat}&lng={self.lng}'
                          f'&journey_referer=partial_address&send_all_serviceability=true')
            try:
                resp = self._request_with_retry('GET', service_url)
                self._update_cookies(resp)
                if resp.status_code == 200:
                    self._print(f"      ✅ Serviceability checked")
            except Exception as e:
                self._print(f"      ⚠️ Serviceability error: {e}")
        time.sleep(0.5)

        self._print("   📋 Setting delivery address...")
        lat_val = float(self.lat) if self.lat else 0
        lng_val = float(self.lng) if self.lng else 0
        pincode_val = int(self.pincode) if self.pincode else 0
        
        payload = {
            "nick": "Home",
            "first_name": self.first_name or "User",
            "last_name": self.last_name or "",
            "contact_number": self.contact_number or "9876543210",
            "house_no": self.house_no or "1",
            "apartment_name": self.apartment_name or "Home",
            "landmark": self.landmark or "",
            "pincode": pincode_val,
            "is_default": 0,
            "area": self.area or "",
            "is_partial": False,
            "id": 0,
            "lng": lng_val,
            "lat": lat_val,
            "set_current_address": True,
            "is_address_correction_required": False
        }

        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/member-svc/v2/address/', json=payload)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                self.address_id = data.get('id')
                self.is_partial_address = False
                self._print(f"      ✅ Address set! ID: {self.address_id}")
                if '_bb_cid' in response.cookies:
                    self.actual_city_id = response.cookies['_bb_cid']
                if '_bb_sa_ids' in response.cookies:
                    self.sa_ids = response.cookies['_bb_sa_ids']
                self.refresh_session_cookies()
                return True
            else:
                self._print(f"      ❌ Failed with status {response.status_code}")
                return False
        except Exception as e:
            self._print(f"      ❌ Error: {e}")
            return False

    def skip_address_and_continue(self) -> bool:
        self._print("\n⏭️ SKIPPING ADDRESS SETUP")
        self.address_set_skipped = True
        self._set_cookie_safe('_bb_cid', self.actual_city_id)
        self._set_cookie_safe('_bb_sa_ids', '28425')
        return True

    def refresh_ui_after_address(self) -> bool:
        self._print("\n📋 Refreshing UI...")
        self._set_cookie_safe('_bb_cid', self.actual_city_id)
        if self.sa_ids:
            self._set_cookie_safe('_bb_sa_ids', self.sa_ids)
        try:
            response = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true&send_pseudo_door=true&send_order_restriction_enabled_door=true&enable-pharma-door=true')
            self._update_cookies(response)
            return True
        except:
            return True

    def load_homepage(self) -> bool:
        self._print("\n🏠 Loading homepage...")
        try:
            home_url = f'https://www.bigbasket.com/ui-svc/v1/page/dynamic?city_id={self.actual_city_id}&client_static_version=2.0.0&shopping_list&_bb_sa_ids={self.sa_ids if self.sa_ids else "28425"}&device_type=app-pwa&slug=hp&type=hp'
            response = self._request_with_retry('GET', home_url)
            self._update_cookies(response)
            return True
        except:
            return True

    # ============================================
    # SEARCH PRODUCTS
    # ============================================
    def search_product(self, search_term: str) -> bool:
        self._print(f"\n🔍 Searching for: {search_term}")
        self.refresh_session_cookies()
        self.session.headers.update({
            'x-tracker': self.generate_tracker(),
            'x-entry-context': 'bb-b2c',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true'
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        if self.sa_ids:
            self._set_cookie_safe('_bb_sa_ids', self.sa_ids)
        
        products_url = (f'https://www.bigbasket.com/listing-svc/v4/products'
                       f'?page=1&_mode=affinity_prod_ab&bucket_id=84'
                       f'&is_oos_widget_flow=true&slug={search_term}&type=ps&new_offer_flow=true')
        try:
            response = self._request_with_retry('GET', products_url)
            if response.status_code == 200:
                data = response.json()
                if 'tabs' in data and len(data['tabs']) > 0:
                    products = data['tabs'][0].get('product_info', {}).get('products', [])
                    self._print(f"   ✅ Found {len(products)} products!")
                    self.product_list = products
                    return True
                else:
                    self._print(f"   ⚠️ No products found")
                    return False
            else:
                self._print(f"   ❌ Failed: {response.status_code}")
                return False
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    def _extract_product_data(self, product: Dict) -> Optional[Dict]:
        try:
            fc_id = None
            if 'visibility' in product and 'fc_id' in product['visibility']:
                fc_id = product['visibility']['fc_id']
            elif 'fc_id' in product:
                fc_id = product['fc_id']
            sa_id = None
            if 'visibility' in product and 'sa_id' in product['visibility']:
                sa_id = product['visibility']['sa_id']
            mrp = None; sp = None; discount_text = None
            discount_percent = None; subscription_price = None
            if 'pricing' in product and 'discount' in product['pricing']:
                discount = product['pricing']['discount']
                mrp = discount.get('mrp')
                if 'prim_price' in discount:
                    sp = discount['prim_price'].get('sp')
                discount_text = discount.get('d_text')
                subscription_price = discount.get('subscription_price')
                if 'camp_detail' in discount and 'd_v' in discount['camp_detail']:
                    discount_percent = round(discount['camp_detail']['d_v'], 1)
            image = None
            if 'images' in product and len(product['images']) > 0:
                image = product['images'][0].get('m') or product['images'][0].get('s')
            avg_rating = None; rating_count = None
            if 'rating_info' in product:
                avg_rating = product['rating_info'].get('avg_rating')
                rating_count = product['rating_info'].get('rating_count')
            avail_status = None; delivery_eta = None
            if 'availability' in product:
                avail_status = product['availability'].get('avail_status')
                delivery_eta = product['availability'].get('short_eta')
            brand = None
            if 'brand' in product and 'name' in product['brand']:
                brand = product['brand']['name']
            category = None
            if 'category' in product and 'mlc_name' in product['category']:
                category = product['category']['mlc_name']
            return {
                'id': product.get('id'),
                'desc': product.get('desc'),
                'w': product.get('w'),
                'pack_desc': product.get('pack_desc'),
                'magnitude': product.get('magnitude'),
                'unit': product.get('unit'),
                'brand': brand,
                'category': category,
                'mrp': mrp,
                'sp': sp,
                'discount_text': discount_text,
                'discount_percent': discount_percent,
                'subscription_price': subscription_price,
                'fc_id': fc_id,
                'sa_id': sa_id,
                'image': image,
                'avg_rating': avg_rating,
                'rating_count': rating_count,
                'avail_status': avail_status,
                'delivery_eta': delivery_eta,
                'number_sold': product.get('number_of_skus_sold'),
                'absolute_url': product.get('absolute_url'),
                'full_data': product
            }
        except Exception as e:
            return None

    def get_product_by_id(self, product_id: str) -> Optional[Dict]:
        for product in self.product_list:
            if str(product.get('id')) == str(product_id):
                return self._extract_product_data(product)
        return None

    # ============================================
    # CART OPERATIONS
    # ============================================
    def add_to_cart(self, product_id: str, fc_id: str, quantity: int = 1, referrer: str = "search") -> bool:
        self._print(f"\n🛒 Adding to cart: {product_id} x{quantity}")
        self.session.headers.update({
            'x-retry': '0',
            'user-agent': 'BB Android/v8.35.0/os 15',
            'accept-encoding': 'gzip',
            'x-is-debug': 'false',
            'content-type': 'application/json',
            'x-tcp-device-version': 'android_8.35.0_25113510',
            'common-client-static-version': '104',
            'x-device-model': 'Samsung SM-A536E',
            'x-entry-context-id': '100',
            'x-bucket-id': '50',
            'x-channel': 'BB-Android',
            'host': 'www.bigbasket.com',
            'x-tcp-platform': 'native',
            'x-integrated-fc-door-visible': 'true',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-entry-context': 'bb-b2c'
        })
        try:
            fc_id_int = int(fc_id)
        except (ValueError, TypeError):
            fc_id_int = 1
        payload = {
            "prod_id": str(product_id),
            "referrer": referrer,
            "qty": quantity,
            "inv_info": {
                "skus": [{
                    "id": int(product_id),
                    "qty": quantity,
                    "fc_id": fc_id_int
                }]
            }
        }
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/mapi/v4.2.0/c-incr-i/', json=payload)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'OK':
                    self._print(f"   ✅ Added to cart!")
                    return True
                return False
            return False
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    def update_cart_quantity(self, product_id: str, fc_id: str, quantity: int, referrer: str = "cart") -> bool:
        return self.add_to_cart(product_id, fc_id, quantity, referrer)

    def remove_from_cart(self, product_id: str, fc_id: str) -> bool:
        return self.update_cart_quantity(product_id, fc_id, 0, "cart")

    def get_cart_summary(self) -> bool:
        self._print(f"\n📦 Getting cart summary...")
        try:
            response = self._request_with_retry('GET', 'https://www.bigbasket.com/mapi/v4.2.0/cart/summary/')
            if response.status_code == 200:
                data = response.json()
                c_summary = data.get('response', {}).get('c_summary', {})
                self._print(f"   Total Items: {c_summary.get('num_items', 0)}")
                return True
            return False
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    # ============================================
    # CHECKOUT AND SHIPMENT
    # ============================================
    def checkout(self, address_id: str) -> bool:
        self._print(f"\n🛍️ CHECKOUT")
        if self.bb_token:
            self._set_cookie_safe('BBAUTHTOKEN', self.bb_token)
        else:
            return False
        try:
            refresh_resp = self._request_with_retry('GET', 'https://www.bigbasket.com/ui-svc/v2/header/?send_door_info=true')
            self._update_cookies(refresh_resp)
            self.csurftoken = self.session.cookies.get('csurftoken')
        except:
            pass
        self.session.headers.update({
            'content-type': 'application/json',
            'x-retry': '0',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-bucket-id': '50',
            'common-client-static-version': '104',
            'x-tcp-device-version': 'android_8.35.0_25113510',
            'x-device-model': 'Samsung SM-A536E',
            'x-channel': 'BB-Android',
            'x-tcp-platform': 'native',
            'host': 'www.bigbasket.com',
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        payload = {
            "member_address_id": str(address_id),
            "is_split_order_supported": True,
            "offer_communication": True,
            "allow_topup_non_society_add": True,
            "action": "default",
            "freecash_v2_enabled": True,
            "progress_bars": ["delivery-charge", "supersaver"],
            "new_offer_flow": "true"
        }
        try:
            response = self._request_with_retry('POST', 'https://www.bigbasket.com/order/v3/checkout', json=payload)
            self._update_cookies(response)
            if response.status_code == 200:
                data = response.json()
                self.po_id = data.get('po_id')
                self.cart_summary = data.get('c_summary', {})
                self.is_checkout_allowed = data.get('is_checkout_allowed', False)
                self.bb_txn_id = self.generate_tracker()
                self._print(f"   ✅ Checkout successful! PO_ID: {self.po_id}")
                return True
            return False
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    def get_shipment(self, po_id: str) -> Optional[Dict]:
        self._print(f"\n📦 Getting shipment for: {po_id}")
        try:
            response = self._request_with_retry('GET', f'https://www.bigbasket.com/order/v2/potentialorder/{po_id}/shipment')
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return None

    def assign_slot(self, po_id: str, shipment_group_id: int, shipment_id: int, 
                    slot_date: str, slot_definition_id: int, template_slot_id: int) -> bool:
        self._print(f"\n📅 Assigning slot...")
        self.session.headers.update({
            'content-type': 'application/json',
            'x-retry': '0',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-bucket-id': '50',
            'common-client-static-version': '104',
            'x-tcp-device-version': 'android_8.35.0_25113510',
            'x-device-model': 'Samsung SM-A536E',
            'x-channel': 'BB-Android',
            'x-tcp-platform': 'native',
            'host': 'www.bigbasket.com',
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        payload = {
            "assign_slots": {
                "contactless": False,
                "shipment_group_id": shipment_group_id,
                "slots": [{
                    "bb_star_avail": False,
                    "shipment_id": shipment_id,
                    "slot_date": slot_date,
                    "slot_definition_id": slot_definition_id,
                    "template_slot_id": template_slot_id
                }],
                "challan_opt_in": False
            },
            "operation": "assign_slots"
        }
        try:
            response = self._request_with_retry('POST', f'https://www.bigbasket.com/order/v2/potentialorder/{po_id}/shipment', json=payload)
            self._update_cookies(response)
            return response.status_code == 200
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return False

    # ============================================
    # COUPON/VOUCHER METHODS
    # ============================================
    def get_voucher_list(self, po_id: str) -> Optional[Dict]:
        self._print(f"\n🎫 Getting voucher list...")
        if not self.bb_token:
            return None
        self.session.headers.update({
            'content-type': 'application/json',
            'x-retry': '0',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-bucket-id': '50',
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        payload = {
            "context": "checkout",
            "saved_payment_data": {"cards": [], "wallets": []},
            "available_upi_apps_on_device": []
        }
        try:
            response = self._request_with_retry('POST', f'https://www.bigbasket.com/order/v3/potentialorder/{po_id}/voucher-list', json=payload)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return None

    def apply_coupon(self, po_id: str, voucher_code: str, bb_txn_id: str = None) -> Optional[Dict]:
        self._print(f"\n🎫 Applying coupon: {voucher_code}")
        if not self.bb_token:
            return None
        if not bb_txn_id:
            bb_txn_id = self.generate_tracker()
        self.session.headers.update({
            'content-type': 'application/json',
            'x-retry': '0',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-bucket-id': '50',
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        payload = {
            "bb_txn_id": bb_txn_id,
            "voucher": {
                "context": "checkout",
                "voucher_code": voucher_code,
                "operation": "apply"
            },
            "neucoins_wallet_auto_select": True,
            "freecash_v2_enabled": True
        }
        try:
            response = self._request_with_retry('POST', f'https://www.bigbasket.com/order/v2/potentialorder/{po_id}/summary', json=payload)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return None

    def remove_coupon(self, po_id: str, voucher_code: str, bb_txn_id: str = None) -> Optional[Dict]:
        self._print(f"\n🎫 Removing coupon: {voucher_code}")
        if not self.bb_token:
            return None
        if not bb_txn_id:
            bb_txn_id = self.generate_tracker()
        self.session.headers.update({
            'content-type': 'application/json',
            'x-retry': '0',
            'x-entry-context-id': '100',
            'x-integrated-fc-door-visible': 'true',
            'x-entry-context': 'bb-b2c',
            'x-tracker': self.generate_tracker(),
            'x-pharma': 'true',
            'x-bucket-id': '50',
        })
        if self.csurftoken:
            self.session.headers['x-csurftoken'] = self.csurftoken
        payload = {
            "bb_txn_id": bb_txn_id,
            "voucher": {
                "context": "checkout",
                "voucher_code": voucher_code,
                "operation": "remove"
            },
            "neucoins_wallet_auto_select": True,
            "freecash_v2_enabled": True
        }
        try:
            response = self._request_with_retry('POST', f'https://www.bigbasket.com/order/v2/potentialorder/{po_id}/summary', json=payload)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            self._print(f"   ❌ Error: {e}")
            return None

    def generate_bb_txn_id(self):
        return str(uuid.uuid4())

    # ============================================
    # COMPLETE FLOW
    # ============================================
    def complete_flow(self, mobile: str) -> bool:
        self._print("\n" + "="*60)
        self._print("🛒 BIGBASKET COMPLETE FLOW")
        self._print("="*60)
        if not self.register_device():
            return False
        if not self.load_ui_data():
            return False
        if not self.update_device_info():
            return False
        if not self.request_otp(mobile):
            return False
        return True

    def complete_post_login_flow(self) -> bool:
        self._print("\n" + "="*60)
        self._print("🛒 CONTINUING WITH POST-LOGIN FLOW")
        self._print("="*60)
        if not self.refresh_session_cookies():
            self._print("⚠️ Session refresh had issues, but continuing...")
        if self.lat and self.lng and self.area and self.pincode:
            address_success = self.set_address_with_user_details()
            if not address_success:
                self._print("\n⚠️ Address setup failed. Skipping...")
                self.skip_address_and_continue()
        else:
            self._print("⚠️ No location data available. Skipping address...")
            self.skip_address_and_continue()
        self.refresh_ui_after_address()
        self.load_homepage()
        self._print("\n" + "="*60)
        self._print("✅ POST-LOGIN SETUP COMPLETE")
        self._print("="*60)
        return True

# ============================================================
# TEST FUNCTION
# ============================================================
if __name__ == "__main__":
    print("="*60)
    print("🧪 Testing BigBasket Client")
    print("="*60)
    client = BigBasketClient(silent=False)
    print("\n1️⃣ Registering device...")
    client.register_device()
    print(f"   Visitor ID: {client.visitor_id}")
    print("\n2️⃣ Loading UI...")
    client.load_ui_data()
    print("\n3️⃣ Updating device...")
    client.update_device_info()
    phone = input("\n📱 Enter phone number to test: ").strip()
    if not phone:
        phone = "7385412257"
        print(f"Using: {phone}")
    print(f"\n4️⃣ Requesting OTP for {phone}...")
    result = client.request_otp(phone)
    if result:
        print("\n✅ OTP SENT SUCCESSFULLY!")
        print(f"📝 RefID: {client.ref_id}")
        otp = input("\n📱 Enter OTP received: ").strip()
        if otp:
            print(f"\n5️⃣ Verifying OTP: {otp}...")
            if client.verify_otp(phone, otp):
                print("✅ LOGIN SUCCESSFUL!")
                print("\n6️⃣ Getting wallet details...")
                wallet = client.get_wallet_details()
                if wallet:
                    print(f"💰 Wallet Balance: ₹{wallet.get('current_wallet_balance', 0)}")
                print("\n7️⃣ Getting FreeCash...")
                freecash = client.get_free_cash()
                if freecash:
                    print(f"🎯 FreeCash: ₹{freecash.get('total_freecash_amount', 0)}")
            else:
                print("❌ OTP verification failed!")
    else:
        print(f"\n❌ OTP FAILED: {client.last_otp_error}")