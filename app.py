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
import csv
import random
import socket
import argparse
import threading
import uuid
from datetime import datetime, timezone
from flask import Flask, send_from_directory, request, jsonify, Response

app = Flask(__name__, static_folder=None)

def _decode(raw_bytes):
    """尝试 UTF-8，失败则用 GBK 解码"""
    try:
        return raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return raw_bytes.decode('gbk', errors='replace')

# ============ 配置 ============
IMAGE_DIR = os.environ.get('IMAGE_DIR', 'cg+')
CSV_PATH = os.environ.get('CSV_PATH', 'resultnew.csv')
PORT = int(os.environ.get('PORT', 8080))

R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY', '')
R2_SECRET_KEY = os.environ.get('R2_SECRET_KEY', '')
R2_BUCKET = os.environ.get('R2_BUCKET', 'image-rating')
R2_CSV_KEY = os.environ.get('R2_CSV_KEY', 'resultnew.csv')

SYNC_STATE_PATH = os.environ.get('SYNC_STATE_PATH', 'ratings_c.json')
SYNC_SETTINGS_PATH = os.environ.get('SYNC_SETTINGS_PATH', 'settings.json')
R2_SYNC_STATE_KEY = os.environ.get('R2_SYNC_STATE_KEY', 'ratings_c.json')
R2_SYNC_SETTINGS_KEY = os.environ.get('R2_SYNC_SETTINGS_KEY', 'settings.json')

DEFAULT_SYNC_SETTINGS = {
    'scorerange': 1.0,
    'c1_max': [6.0, 4.0, 2.0, 8.0, 4.0],
    'c1_labels': ['方式', '魔物娘表情', '鲁卡表情 高潮脸', '画面完整度', '动作幅度'],
    'c1_scoreranges': [1.0, 1.0, 1.0, 1.0, 1.0],
    'c1_batch_size': 1,
    'c1_selected_items': [0, 1, 2, 3, 4],
}

_s3 = None
_state_lock = threading.RLock()

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


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _read_json_state(path, r2_key):
    """Read shared JSON state from R2 first, then the local fallback."""
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                response = s3.get_object(Bucket=R2_BUCKET, Key=r2_key)
                value = json.loads(_decode(response['Body'].read()))
                with open(path, 'w', encoding='utf-8') as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                return value
            except Exception as exc:
                code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
                if code not in ('NoSuchKey', '404'):
                    print(f"[R2] {r2_key} 读取失败: {exc}")
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[SYNC] {path} 读取失败: {exc}")
    return None


def _write_json_state(path, r2_key, value):
    """Atomically persist shared JSON state locally and to R2."""
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    if r2_on():
        s3 = get_s3()
        if s3:
            s3.put_object(
                Bucket=R2_BUCKET,
                Key=r2_key,
                Body=json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'),
                ContentType='application/json; charset=utf-8',
                CacheControl='no-store',
            )


def _normalized_settings(raw=None):
    settings = {**DEFAULT_SYNC_SETTINGS, **(raw or {})}
    count = len(settings.get('c1_max') or DEFAULT_SYNC_SETTINGS['c1_max'])
    settings['c1_max'] = [float(value) for value in settings['c1_max']]
    labels = list(settings.get('c1_labels') or [])
    settings['c1_labels'] = [labels[i] if i < len(labels) else f'小分{i + 1}' for i in range(count)]
    ranges = list(settings.get('c1_scoreranges') or [])
    settings['c1_scoreranges'] = [
        float(ranges[i]) if i < len(ranges) else float(settings.get('scorerange', 1))
        for i in range(count)
    ]
    selected = [int(i) for i in settings.get('c1_selected_items', range(count)) if 0 <= int(i) < count]
    if 1 in selected or 2 in selected:
        selected = sorted(set(selected) | {1, 2})
    settings['c1_selected_items'] = selected or list(range(count))
    settings['c1_batch_size'] = max(1, min(20, int(settings.get('c1_batch_size', 1))))
    return settings


def _read_shared_state():
    ratings = _read_json_state(SYNC_STATE_PATH, R2_SYNC_STATE_KEY)
    settings = _normalized_settings(_read_json_state(SYNC_SETTINGS_PATH, R2_SYNC_SETTINGS_KEY))
    exists = isinstance(ratings, dict) and isinstance(ratings.get('clusters'), dict)
    return (ratings if exists else {'clusters': {}}, settings, exists)


def _c1_values(rounds, index):
    values = []
    for record in rounds:
        scores = record.get('c1')
        if scores is None:
            continue
        scope = record.get('scope')
        if scope is not None and (index >= len(scope) or not scope[index]):
            continue
        value = scores[index] if index < len(scores) else None
        if value is not None:
            values.append(float(value))
    return values


def _c1_23_values(rounds):
    values = []
    for record in rounds:
        if record.get('c1_23') is not None:
            values.append(float(record['c1_23']))
            continue
        scores, scope = record.get('c1'), record.get('scope')
        if scores is None or len(scores) < 3:
            continue
        if scope is not None and (len(scope) < 3 or not scope[1] or not scope[2]):
            continue
        if scores[1] is not None and scores[2] is not None:
            values.append(float(scores[1]) + float(scores[2]))
    return values


def _item_values(rounds, index):
    return _c1_23_values(rounds) if index in (1, 2) else _c1_values(rounds, index)


def _item_threshold(settings, index):
    ranges = settings['c1_scoreranges']
    if index in (1, 2):
        return float(ranges[1]) + float(ranges[2])
    return float(ranges[index])


def _item_stage(rounds, settings, index):
    values = _item_values(rounds, index)
    if not values:
        return 1
    if len(values) == 1:
        return 2
    if len(values) == 2:
        return 3 if abs(values[0] - values[1]) > _item_threshold(settings, index) else 0
    return 0


def _arbitrate(values, threshold):
    values = [float(value) for value in values[-3:]]
    if not values:
        return None, 'missing'
    if len(values) == 1:
        return values[0], 'partial'
    if len(values) == 2:
        if abs(values[0] - values[1]) <= threshold:
            return sum(values) / 2, 'done'
        return None, 'pending'
    ordered = sorted(values)
    left = abs(ordered[0] - ordered[1])
    right = abs(ordered[1] - ordered[2])
    if left > threshold and right > threshold:
        return round(sum(ordered) / 3 * 2) / 2, 'done'
    if left < right:
        return (ordered[0] + ordered[1]) / 2, 'done'
    if left > right:
        return (ordered[1] + ordered[2]) / 2, 'done'
    return ordered[1], 'done'


def _computed_rating(rounds, settings):
    items = []
    statuses = []
    for index in range(len(settings['c1_max'])):
        if index == 2:
            items.append(None)
            statuses.append(statuses[1] if len(statuses) > 1 else 'missing')
            continue
        value, status = _arbitrate(_item_values(rounds, index), _item_threshold(settings, index))
        items.append(None if value is None else round(value, 2))
        statuses.append(status)
    total = sum(value for value in items if value is not None)
    if len(items) > 2 and items[1] is not None:
        # Item 2+3 is arbitrated as one joint score, matching pngscore.
        total += 0
    status = 'pending' if 'pending' in statuses else ('partial' if 'missing' in statuses or 'partial' in statuses else 'done')
    return {'items': items, 'total': round(total, 2) if status == 'done' else None,
            'status': status, 'stages': [_item_stage(rounds, settings, i) for i in range(len(items))]}


def _computed_ratings(ratings, settings):
    return {
        name: _computed_rating(entry.get('rounds', []), settings)
        for name, entry in ratings.get('clusters', {}).items()
    }

# ============ CSV 读写 ============

def read_csv():
    """读 CSV：优先 R2，回退本地"""
    if r2_on():
        s3 = get_s3()
        if s3:
            try:
                resp = s3.get_object(Bucket=R2_BUCKET, Key=R2_CSV_KEY)
                raw = resp['Body'].read()
                content = _decode(raw)
                with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 CSV ({len(content)} 字节)")
                return content
            except Exception as e:
                print(f"[R2] 读取失败: {e}")
    if os.path.isfile(CSV_PATH):
        raw = open(CSV_PATH, 'rb').read()
        return _decode(raw)
    return None
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
        _read_shared_state()
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
                raw = resp['Body'].read()
                content = _decode(raw)
                with open(DETAIL_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 detail.csv ({len(content)} 字节)")
                return content
            except Exception as e:
                print(f"[R2] detail.csv 读取失败: {e}")
    if os.path.isfile(DETAIL_PATH):
        raw = open(DETAIL_PATH, 'rb').read()
        return _decode(raw)
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
                raw = resp['Body'].read()
                content = _decode(raw)
                with open(RATING_PATH, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                print(f"[R2] 已加载 rating_result.csv ({len(content)} 字节)")
                return content
            except: pass
    if os.path.isfile(RATING_PATH):
        raw = open(RATING_PATH, 'rb').read()
        return _decode(raw)
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


# ---- 与 pngscore 共用的 C1 评分状态 ----

def _cluster_names():
    content = read_csv() or ''
    rows = list(csv.reader(content.splitlines()))
    return [row[0].strip() for row in rows[1:] if row and row[0].strip()]


def _selected_items(settings, raw):
    try:
        items = [int(value) for value in raw.split(',') if value.strip()]
    except (AttributeError, ValueError):
        items = []
    valid = set(range(len(settings['c1_max'])))
    items = [index for index in items if index in valid]
    if 1 in items or 2 in items:
        items = sorted(set(items) | {1, 2})
    return items or list(settings['c1_selected_items'])


def _progress_payload(ratings, settings, names):
    item_progress = []
    for index in range(len(settings['c1_max'])):
        stages = [
            _item_stage(ratings.get('clusters', {}).get(name, {}).get('rounds', []), settings, index)
            for name in names
        ]
        item_progress.append({
            'r1': stages.count(1), 'r2': stages.count(2), 'r3': stages.count(3),
            'done': stages.count(0), 'remaining': sum(stage != 0 for stage in stages),
        })
    completed = sum(
        all(_item_stage(ratings.get('clusters', {}).get(name, {}).get('rounds', []), settings, i) == 0
            for i in range(len(settings['c1_max'])))
        for name in names
    )
    return {'total': len(names), 'done': completed, 'items': item_progress}


@app.route('/api/sync-state', methods=['GET', 'POST'])
def api_sync_state():
    """Canonical state shared by the hosted UI and the local pngscore server."""
    with _state_lock:
        if request.method == 'POST':
            data = request.get_json(force=True) or {}
            ratings = data.get('ratings_c')
            settings = data.get('settings')
            if not isinstance(ratings, dict) or not isinstance(ratings.get('clusters'), dict):
                return jsonify({'error': 'invalid ratings_c'}), 400
            settings = _normalized_settings(settings)
            _write_json_state(SYNC_STATE_PATH, R2_SYNC_STATE_KEY, ratings)
            _write_json_state(SYNC_SETTINGS_PATH, R2_SYNC_SETTINGS_KEY, settings)
        ratings, settings, exists = _read_shared_state()
        names = _cluster_names()
        return jsonify({
            'exists': exists,
            'ratings_c': ratings,
            'settings': settings,
            'computed': _computed_ratings(ratings, settings),
            'progress': _progress_payload(ratings, settings, names),
            'updated_at': _utc_now(),
        })


@app.route('/api/rating-settings', methods=['POST'])
def api_rating_settings():
    with _state_lock:
        ratings, settings, _ = _read_shared_state()
        data = request.get_json(force=True) or {}
        if 'selected_items' in data:
            raw = ','.join(str(value) for value in data['selected_items'])
            settings['c1_selected_items'] = _selected_items(settings, raw)
        if 'batch_size' in data:
            settings['c1_batch_size'] = max(1, min(20, int(data['batch_size'])))
        _write_json_state(SYNC_SETTINGS_PATH, R2_SYNC_SETTINGS_KEY, settings)
        return jsonify({'settings': settings, 'computed': _computed_ratings(ratings, settings)})


@app.route('/api/rating-next')
def api_rating_next():
    with _state_lock:
        ratings, settings, _ = _read_shared_state()
        items = _selected_items(settings, request.args.get('items', ''))
        names = _cluster_names()
        for stage in (1, 2, 3):
            candidates = []
            for name in names:
                rounds = ratings.get('clusters', {}).get(name, {}).get('rounds', [])
                if all(_item_stage(rounds, settings, index) == stage for index in items):
                    candidates.append(name)
            if candidates:
                return jsonify({'cluster': random.choice(candidates), 'stage': stage, 'items': items})
        return jsonify({'cluster': None, 'stage': 0, 'items': items, 'batch_exhausted': True})


@app.route('/api/rating-submit', methods=['POST'])
def api_rating_submit():
    with _state_lock:
        ratings, settings, _ = _read_shared_state()
        data = request.get_json(force=True) or {}
        name = str(data.get('cluster', '')).strip()
        if name not in set(_cluster_names()):
            return jsonify({'error': 'unknown cluster'}), 400
        items = _selected_items(settings, ','.join(str(value) for value in data.get('items', [])))
        raw_scores = list(data.get('scores') or [])
        count = len(settings['c1_max'])
        scores = [None] * count
        scope = [False] * count
        for index in items:
            if index >= len(raw_scores) or raw_scores[index] is None:
                return jsonify({'error': f'missing score item {index}'}), 400
            value = float(raw_scores[index])
            if value < 0 or value > settings['c1_max'][index]:
                return jsonify({'error': f'invalid score item {index}'}), 400
            scores[index] = value
            scope[index] = True

        entry = ratings.setdefault('clusters', {}).setdefault(name, {'rounds': []})
        expected_stages = {_item_stage(entry['rounds'], settings, index) for index in items}
        if len(expected_stages) != 1 or 0 in expected_stages:
            return jsonify({'error': 'selected items are not in a common pending stage'}), 409
        stage = expected_stages.pop()
        submission_id = uuid.uuid4().hex
        record = {'c1': scores, 'c2': None, 'scope': scope,
                  'submission_id': submission_id, 'source': 'image-rating-app'}
        entry['rounds'].append(record)

        sync_applied = False
        if bool(data.get('sync_round2')) and stage == 1:
            synced = {**record, 'synced_round2': True}
            entry['rounds'].append(synced)
            sync_applied = True

        _write_json_state(SYNC_STATE_PATH, R2_SYNC_STATE_KEY, ratings)
        computed = _computed_rating(entry['rounds'], settings)
        return jsonify({
            'ok': True, 'cluster': name, 'stage': stage, 'items': items,
            'submission_id': submission_id, 'sync_round2_applied': sync_applied,
            'computed': computed,
            'progress': _progress_payload(ratings, settings, _cluster_names()),
        })

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
