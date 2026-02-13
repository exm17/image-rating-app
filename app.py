"""
图像簇评分器 - Render 部署版本
================================
支持本地运行和 Render 云端部署
"""

import os
import json
import mimetypes
import socket
import argparse
from flask import Flask, send_from_directory, request, jsonify, send_file

app = Flask(__name__, static_folder=None)

# ============ 配置 ============
IMAGE_DIR = os.environ.get('IMAGE_DIR', 'cg+')
CSV_PATH = os.environ.get('CSV_PATH', 'resultnew.csv')
PORT = int(os.environ.get('PORT', 8080))

# ============ 前端页面 ============
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json')

# ============ 图片服务 ============
@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

# ============ API ============
@app.route('/api/info')
def api_info():
    image_count = 0
    if os.path.isdir(IMAGE_DIR):
        exts = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
        image_count = len([f for f in os.listdir(IMAGE_DIR)
                          if os.path.splitext(f)[1].lower() in exts])
    return jsonify({
        'image_dir': os.path.abspath(IMAGE_DIR),
        'csv_path': os.path.abspath(CSV_PATH),
        'image_count': image_count,
        'csv_exists': os.path.isfile(CSV_PATH),
    })

@app.route('/api/csv')
def api_csv():
    if os.path.isfile(CSV_PATH):
        return send_file(CSV_PATH, mimetype='text/csv; charset=utf-8')
    return 'CSV not found', 404

@app.route('/api/save-csv', methods=['POST'])
def api_save_csv():
    try:
        data = request.get_json()
        csv_content = data.get('csv', '')
        with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
            f.write(csv_content)
        return jsonify({'success': True, 'message': '保存成功'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/images')
def api_images():
    images = []
    if os.path.isdir(IMAGE_DIR):
        exts = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
        images = [f for f in os.listdir(IMAGE_DIR)
                  if os.path.splitext(f)[1].lower() in exts]
    return jsonify({'count': len(images), 'images': images})

# ============ 启动 ============
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='图像簇评分器服务器')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--image-dir', default=IMAGE_DIR)
    parser.add_argument('--csv', default=CSV_PATH)
    args = parser.parse_args()

    IMAGE_DIR = args.image_dir
    CSV_PATH = args.csv
    PORT = args.port

    local_ip = get_local_ip()
    print(f"\n{'='*50}")
    print(f"  🚀 图像簇评分器服务器已启动!")
    print(f"{'='*50}")
    print(f"  本机访问: http://localhost:{PORT}")
    print(f"  手机访问: http://{local_ip}:{PORT}")
    print(f"  图片目录: {os.path.abspath(IMAGE_DIR)}")
    print(f"  CSV文件:  {os.path.abspath(CSV_PATH)}")
    print(f"{'='*50}\n")

    app.run(host='0.0.0.0', port=PORT, debug=False)
