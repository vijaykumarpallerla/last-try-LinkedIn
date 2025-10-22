import os
import threading
import time
import tempfile
import logging
from flask import Flask, render_template, jsonify, send_file, request
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
scraper_status = {'is_running': False, 'progress': 'idle', 'last_screenshot': None}


def create_driver(headless=True):
    opts = Options()
    if headless:
        try:
            opts.add_argument('--headless=new')
        except Exception:
            opts.add_argument('--headless')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--window-size=1200,800')
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _save_live_screenshot(driver):
    fd, path = tempfile.mkstemp(prefix='ln-scr-', suffix='.png')
    os.close(fd)
    driver.save_screenshot(path)
    scraper_status['last_screenshot'] = path
    return path


def scraper_task():
    scraper_status['is_running'] = True
    scraper_status['progress'] = 'starting'
    drv = None
    try:
        drv = create_driver(headless=not os.getenv('FORCE_UI'))
        drv.get('https://www.linkedin.com/login')
        time.sleep(2)
        try:
            _save_live_screenshot(drv)
            scraper_status['progress'] = 'screenshot-saved'
        except Exception:
            scraper_status['progress'] = 'screenshot-failed'
    except Exception:
        scraper_status['progress'] = 'error'
    finally:
        try:
            if drv:
                drv.quit()
        except Exception:
            pass
        scraper_status['is_running'] = False


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/start', methods=['POST'])
def api_start():
    if scraper_status['is_running']:
        return jsonify({'ok': False, 'error': 'already-running'})
    t = threading.Thread(target=scraper_task, daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/api/status')
def api_status():
    return jsonify(scraper_status)


@app.route('/api/screenshot')
def api_screenshot():
    p = scraper_status.get('last_screenshot')
    if p and os.path.exists(p):
        return send_file(p, mimetype='image/png')
    return jsonify({'ok': False, 'error': 'no-screenshot'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
