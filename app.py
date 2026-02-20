"""
图像簇评分器 - Render 部署版本（R2 持久化）
=============================================
CSV 自动存储到 Cloudflare R2，Render 重启/重新部署不丢数据。
本地运行时不配置 R2 环境变量则使用本地文件，行为不变。

Render 环境变量（Dashboard → Environment）:
  R2_ACCOUNT_ID   - Cloudflare Account ID
  R2_ACCESS_KEY   - R2 API Access Key ID
  R2_SECRET_KEY   - R2 API Secret Access Key
  R2_BUCKET       - R2 存储桶名称（默认 image-rating）
  R2_CSV_KEY      - R2 中 CSV 的文件名（默认 resultnew.csv）
"""

import os
import json
import socket
import argparse
from flask import Flask, send_from_directory, request, jsonify, Response

app = Flask(__name__, static_folder=None)

# ============ 配置 ============
IMAGE_DIR = os.environ.get('IMAGE_DIR', 'cg+')
CSV_PATH = os.environ.get('CSV_PATH', 'resultnew.csv')
PORT = int(os.environ.get('PORT', 8080))

R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY', '')
R2_SECRET_KEY = os.environ.get('R2_SECRET_KEY', '')
R2_BUCKET = os.environ.get('R2_BUCKET', 'image-rating')
R2_CSV_KEY = os.environ.get('R2_CSV_KEY', 'resultnew.csv')

_s3 = None

def get_s3():
    global _s3
    if _s3 is not None:
        return _s3
    if not R2_ACCOUNT_ID:
        return None
    try:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client(
            's3',
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(retries={'max_attempts': 3, 'mode': 'adaptive'}),
            region_name='auto'
        )
        return _s3
    except Exception as e:
        print(f"[R2] 连接失败: {e}")
        return None

def r2_on():
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY)

# ============ CSV 读写 ============

def read_csv():
    """读 CSV：优先 R2，回退本地"""
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=R2_CSV_KEY)
                content = resp['Body'].read().decode('utf-8')
                with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 CSV ({len(content)} 字节)")
                return content
            except Exception as e:
                print(f"[R2] 读取失败: {e}")
    if os.path.isfile(CSV_PATH):
        with open(CSV_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return None

def write_csv(content):
    """写 CSV：本地 + R2"""
    with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                s3.put_object(Bucket=R2_BUCKET, Key=R2_CSV_KEY,
                              Body=content.encode('utf-8'),
                              ContentType='text/csv; charset=utf-8')
                return True, '已保存（本地 + R2）'
            except Exception as e:
                return True, f'已保存本地，R2失败: {e}'
    return True, '已保存（本地）'

# 启动时从 R2 拉取所有 CSV
def init_csv():
    if r2_on():
        print(f"[R2] bucket={R2_BUCKET}")
        read_csv()
        read_detail_csv()
        read_rating_csv()
    else:
        print("[CSV] 使用本地文件（未配置R2）")

# ============ 路由 ============

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json')

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

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
        'r2_enabled': r2_on(),
    })

@app.route('/api/csv')
def api_csv():
    content = read_csv()
    if content:
        return Response(content, mimetype='text/csv; charset=utf-8')
    return 'CSV not found', 404

@app.route('/api/save-csv', methods=['POST'])
def api_save_csv():
    try:
        data = request.get_json()
        csv_content = data.get('csv', '')
        success, message = write_csv(csv_content)
        return jsonify({'success': success, 'message': message})
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

# ---- detail.csv (评分小项配置) ----
DETAIL_PATH = os.environ.get('DETAIL_PATH', 'detail.csv')
R2_DETAIL_KEY = os.environ.get('R2_DETAIL_KEY', 'detail.csv')

def read_detail_csv():
    """读 detail.csv：优先 R2，回退本地"""
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=R2_DETAIL_KEY)
                content = resp['Body'].read().decode('utf-8')
                with open(DETAIL_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 detail.csv ({len(content)} 字节)")
                return content
            except Exception as e:
                print(f"[R2] detail.csv 读取失败: {e}")
    if os.path.isfile(DETAIL_PATH):
        with open(DETAIL_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return None

@app.route('/api/detail-csv')
def api_detail_csv():
    content = read_detail_csv()
    if content:
        return Response(content, mimetype='text/csv; charset=utf-8')
    return 'detail.csv not found', 404

# ---- rating_result.csv (评分结果) ----
RATING_PATH = os.environ.get('RATING_PATH', 'rating_result.csv')
R2_RATING_KEY = os.environ.get('R2_RATING_KEY', 'rating_result.csv')

def read_rating_csv():
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=R2_RATING_KEY)
                content = resp['Body'].read().decode('utf-8')
                with open(RATING_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 rating_result.csv ({len(content)} 字节)")
                return content
            except: pass
    if os.path.isfile(RATING_PATH):
        with open(RATING_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return None

def write_rating_csv(content):
    with open(RATING_PATH, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                s3.put_object(Bucket=R2_BUCKET, Key=R2_RATING_KEY,
                              Body=content.encode('utf-8'), ContentType='text/csv; charset=utf-8')
                return True, '已保存（本地 + R2）'
            except Exception as e:
                return True, f'已保存本地，R2失败: {e}'
    return True, '已保存（本地）'

@app.route('/api/rating-csv')
def api_rating_csv():
    content = read_rating_csv()
    if content:
        return Response(content, mimetype='text/csv; charset=utf-8')
    return 'rating_result.csv not found', 404

@app.route('/api/save-rating-csv', methods=['POST'])
def api_save_rating_csv():
    try:
        data = request.get_json()
        csv_content = data.get('csv', '')
        success, message = write_rating_csv(csv_content)
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

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

init_csv()

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
    print(f"  图像簇评分器服务器")
    print(f"{'='*50}")
    print(f"  本机: http://localhost:{PORT}")
    print(f"  局域网: http://{local_ip}:{PORT}")
    print(f"  R2持久化: {'✅' if r2_on() else '❌ 未配置'}")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False)
