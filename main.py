#!/usr/bin/env python3
"""
Production Proxy API Server
Continuous proxy fetching and validation with REST API
Modified for blacklist support and 20-proxy queue system
"""

import asyncio
import threading
import time
import requests
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn
import re
import os
from typing import List, Dict, Any
from collections import deque
import logging
import urllib3


# Disable SSL warnings for proxy testing
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ProxyManager:
    def __init__(self):
        # Main 20-proxy queue (latest working proxies)
        self.active_proxies_queue = deque(maxlen=20)  # Only keeps latest 20
        
        # Blacklist for non-HTTPS proxies (to avoid re-checking)
        self.blacklisted_proxies = set()
        
        # All fetched proxies for processing
        self.all_fetched_proxies = set()
        
        # Stats
        self.stats = {
            'total_fetched': 0,
            'valid_proxies': 0,
            'invalid_proxies': 0,
            'blacklisted_count': 0,
            'active_queue_size': 0,
            'last_update': None,
            'current_batch_progress': 0,
            'is_updating': False,
            'update_start_time': None
        }
        
        # Configuration
        self.max_workers = 1000  # Low thread count
        self.update_interval = 360  # 10 minutes between updates
        self.validation_timeout = 4
        
        # Lock for thread safety
        self.lock = threading.Lock()
        
        # Proxy APIs (40+ sources)
        self.proxy_apis = [
            # ProxyScrape APIs
            'https://api.proxyscrape.com/v2/?request=get&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all',
            'https://api.proxyscrape.com/v2/?request=get&protocol=https&timeout=10000&country=all&ssl=all&anonymity=all',
            
            # GitHub Raw Lists
            'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
            'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt',
            'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
            'https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt',
            'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt',
            'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt',
            'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
            'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/https.txt',
            'https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt',
            'https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt',
            'https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt',
            'https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt',
            'https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt',
            'https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt',
            'https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt',
            'https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTP_RAW.txt',
            'https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/https.txt',
            'https://raw.githubusercontent.com/UserR3X/proxy-list/main/online/http.txt',
            'https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt',
            'https://raw.githubusercontent.com/zevtyardt/proxy-list/main/https.txt',
            'https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt',
            'https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt',
            'https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/https.txt',
            'https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt',
            'https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/https.txt',
            'https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt',
            'https://raw.githubusercontent.com/prxchk/proxy-list/main/https.txt',
            'https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt',
            'https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt',
            'https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt',
            'https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/https_proxies.txt',
            'https://raw.githubusercontent.com/yemixzy/proxy-list/main/proxy-list/data.txt',
            'https://raw.githubusercontent.com/almroot/proxylist/master/list.txt',
            'https://raw.githubusercontent.com/aslisk/proxyhttps/main/https.txt',
            'https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt',
            'https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt',
            
            # Alternative APIs
            'https://www.proxy-list.download/api/v1/get?type=http',
            'https://www.proxy-list.download/api/v1/get?type=https',
            'https://api.openproxylist.xyz/http.txt',
            'https://api.openproxylist.xyz/https.txt'
        ]
        
        # Test URLs for HTTPS validation
        self.test_urls = [
            'https://httpbin.org/ip',
            'https://api.ipify.org?format=json'
        ]
        
        # Start with empty data (no file loading)
        # Data will be populated as validation runs
        
        logger.info(f"ProxyManager initialized - In-Memory Storage Only")
    
    def is_valid_proxy_format(self, proxy: str) -> bool:
        """Validate proxy format (IP:PORT)"""
        pattern = r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$'
        match = re.match(pattern, proxy)
        if not match:
            return False
        
        ip_parts = match.group(1).split('.')
        port = int(match.group(2))
        
        # Validate IP range
        for part in ip_parts:
            if not (0 <= int(part) <= 255):
                return False
        
        # Validate port range
        if not (1 <= port <= 65535):
            return False
            
        return True
    
    def test_proxy_https(self, proxy: str) -> bool:
        """Test if proxy supports HTTPS"""
        proxy_dict = {
            'http': f'http://{proxy}',
            'https': f'http://{proxy}'
        }
        
        for test_url in self.test_urls:
            try:
                response = requests.get(
                    test_url,
                    proxies=proxy_dict,
                    timeout=self.validation_timeout,
                    verify=False
                )
                
                if response.status_code == 200:
                    # Check if response contains IP info
                    if 'ip' in response.text.lower() or 'origin' in response.text.lower():
                        return True
            except:
                continue
        
        return False
    
    def add_to_blacklist(self, proxy: str):
        """Add proxy to blacklist"""
        with self.lock:
            self.blacklisted_proxies.add(proxy)
            self.stats['blacklisted_count'] = len(self.blacklisted_proxies)
    
    def add_to_active_queue(self, proxy: str):
        """Add valid proxy to active queue (maintains 20 max)"""
        with self.lock:
            # Add to queue (automatically removes oldest if > 20)
            self.active_proxies_queue.append(proxy)
            self.stats['active_queue_size'] = len(self.active_proxies_queue)
            self.stats['valid_proxies'] += 1
        
        logger.info(f"✅ Added to active queue: {proxy} (Queue size: {len(self.active_proxies_queue)})")
    
    def fetch_proxies_from_api(self, api_url: str) -> List[str]:
        """Fetch proxies from single API"""
        try:
            response = requests.get(api_url, timeout=15)
            response.raise_for_status()
            
            content = response.text.strip()
            proxies = content.split('\n')
            
            valid_proxies = []
            for proxy in proxies:
                proxy = proxy.strip().split('#')[0].strip().split(' ')[0].strip()
                if self.is_valid_proxy_format(proxy):
                    valid_proxies.append(proxy)
            
            logger.info(f"Fetched {len(valid_proxies)} valid format proxies from API")
            return valid_proxies
            
        except Exception as e:
            logger.error(f"Error fetching from {api_url}: {str(e)}")
            return []
    
    def fetch_all_proxies(self) -> List[str]:
        """Fetch proxies from all APIs"""
        logger.info("Starting proxy fetch from all APIs...")
        all_proxies = set()
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_api = {executor.submit(self.fetch_proxies_from_api, api): api 
                           for api in self.proxy_apis}
            
            for future in as_completed(future_to_api):
                proxies = future.result()
                all_proxies.update(proxies)
        
        # Filter out blacklisted proxies
        with self.lock:
            filtered_proxies = [p for p in all_proxies if p not in self.blacklisted_proxies]
        
        logger.info(f"Total unique proxies: {len(all_proxies)}, After blacklist filter: {len(filtered_proxies)}")
        
        with self.lock:
            self.stats['total_fetched'] = len(all_proxies)
        
        return filtered_proxies
    
    def validate_proxy_batch(self, proxies: List[str]):
        """Validate batch of proxies and update active queue continuously"""
        total_proxies = len(proxies)
        logger.info(f"Validating {total_proxies} proxies (excluding blacklisted)...")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_proxy = {executor.submit(self.test_proxy_https, proxy): proxy 
                             for proxy in proxies}
            
            completed = 0
            for future in as_completed(future_to_proxy):
                completed += 1
                proxy = future_to_proxy[future]
                
                try:
                    if future.result():
                        # Valid HTTPS proxy - add to active queue
                        self.add_to_active_queue(proxy)
                    else:
                        # Invalid - add to blacklist
                        self.add_to_blacklist(proxy)
                        logger.debug(f"❌ Blacklisted: {proxy}")
                    
                    # Update progress
                    progress = (completed / total_proxies) * 100
                    with self.lock:
                        self.stats['current_batch_progress'] = progress
                        self.stats['invalid_proxies'] = completed - self.stats['valid_proxies']
                        
                except Exception as e:
                    # Error in validation - add to blacklist
                    self.add_to_blacklist(proxy)
                    logger.error(f"Error validating {proxy}: {str(e)}")
        
        logger.info(f"Validation complete. Active queue size: {len(self.active_proxies_queue)}")
    
    def cleanup_blacklist(self):
        """Clean blacklist periodically to prevent unlimited growth"""
        with self.lock:
            # Keep only recent 10000 blacklisted proxies to prevent memory issues
            if len(self.blacklisted_proxies) > 10000:
                # Convert to list, keep last 5000, convert back to set
                blacklist_list = list(self.blacklisted_proxies)
                self.blacklisted_proxies = set(blacklist_list[-5000:])
                self.stats['blacklisted_count'] = len(self.blacklisted_proxies)
                logger.info(f"Cleaned blacklist - kept {len(self.blacklisted_proxies)} recent entries")
    
    def update_proxies(self):
        """Main update cycle - continuously adds to 20-proxy queue"""
        while True:
            try:
                logger.info("Starting new proxy update cycle...")
                
                with self.lock:
                    self.stats['is_updating'] = True
                    self.stats['current_batch_progress'] = 0
                    self.stats['update_start_time'] = datetime.now().isoformat()
                
                # Fetch new proxies (excluding blacklisted)
                fetched_proxies = self.fetch_all_proxies()
                
                if fetched_proxies:
                    # Validate and continuously update active queue
                    self.validate_proxy_batch(fetched_proxies)
                    
                    # Periodic blacklist cleanup to prevent memory bloat
                    if self.stats['valid_proxies'] % 100 == 0:  # Every 100 validations
                        self.cleanup_blacklist()
                    
                    with self.lock:
                        self.stats['last_update'] = datetime.now().isoformat()
                        self.stats['is_updating'] = False
                        self.stats['current_batch_progress'] = 100
                    
                    logger.info(f"Update complete! Active queue: {len(self.active_proxies_queue)} proxies, Blacklisted: {len(self.blacklisted_proxies)}")
                else:
                    logger.warning("No new proxies to validate")
                    with self.lock:
                        self.stats['is_updating'] = False
                
                # Wait before next cycle
                logger.info(f"Waiting {self.update_interval} seconds before next update...")
                time.sleep(self.update_interval)
                
            except Exception as e:
                logger.error(f"Error in update cycle: {str(e)}")
                with self.lock:
                    self.stats['is_updating'] = False
                time.sleep(300)  # Wait 5 minutes on error
    
    def get_active_proxies(self) -> List[str]:
        """Get current 20 active proxies (latest working ones)"""
        with self.lock:
            return list(self.active_proxies_queue)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current status"""
        with self.lock:
            status = self.stats.copy()
            status['active_queue_size'] = len(self.active_proxies_queue)
            status['blacklisted_count'] = len(self.blacklisted_proxies)
            return status

# Initialize proxy manager
proxy_manager = ProxyManager()

# FastAPI app
app = FastAPI(
    title="Proxy API Server with Smart Queue",
    description="High-performance proxy server with blacklist and 20-proxy rotating queue",
    version="2.0.0"
)

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Smart Proxy API Server is running", 
        "features": ["20-proxy rotating queue", "Blacklist system", "Continuous validation"],
        "endpoints": ["/proxies", "/status"]
    }

@app.get("/proxies", response_class=PlainTextResponse)
async def get_proxies():
    """Get latest 20 validated HTTPS proxies"""
    try:
        proxies = proxy_manager.get_active_proxies()
        if not proxies:
            raise HTTPException(status_code=404, detail="No active proxies available")
        
        # Return latest 20 proxies as text format
        return "\n".join(proxies)
        
    except Exception as e:
        logger.error(f"Error serving proxies: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/status")
async def get_status():
    """Get proxy validation status and queue statistics"""
    try:
        status = proxy_manager.get_status()
        return {
            "active_proxies_count": status['active_queue_size'],
            "blacklisted_proxies": status['blacklisted_count'],
            "total_fetched": status['total_fetched'],
            "valid_proxies_found": status['valid_proxies'],
            "invalid_proxies": status['invalid_proxies'],
            "last_update": status['last_update'],
            "is_updating": status['is_updating'],
            "current_batch_progress": f"{status['current_batch_progress']:.1f}%",
            "update_start_time": status['update_start_time'],
            "server_status": "running",
            "queue_info": "Latest 20 validated HTTPS proxies"
        }
    except Exception as e:
        logger.error(f"Error getting status: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")

def start_background_updater():
    """Start background proxy updater"""
    updater_thread = threading.Thread(target=proxy_manager.update_proxies, daemon=True)
    updater_thread.start()
    logger.info("Background smart proxy updater started")

# Start background updater when server starts
@app.on_event("startup")
async def startup_event():
    start_background_updater()

if __name__ == "__main__":
    port_str = os.environ.get("PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        logger.error(f"Invalid PORT value from environment: {port_str}")
        port = 8000  # fallback

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )

