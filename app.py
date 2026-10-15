import os
import sqlite3
import json
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
DB_PATH = Path(__file__).with_name('eduschedule.db')

DEFAULT = {
    'settings': {
        'name': 'State Technical University',
        'start': '09:00',
        'end': '17:00',
        'duration': 60,
        'days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        'breaks': [{'start': '13:00', 'end': '14:00', 'name': 'Lunch Break'}]
    },
    'teachers': [], 'subjects': [], 'classes': [], 'rooms': [],
    'timetable': [], 'conflicts': []
}


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute('''CREATE TABLE IF NOT EXISTS app_state (
            id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
        )''')
        row = con.execute('SELECT data FROM app_state WHERE id=1').fetchone()
        if not row:
            con.execute(
                'INSERT INTO app_state(id, data) VALUES(1, ?)',
                (json.dumps(DEFAULT),)
            )


def load_db():
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute('SELECT data FROM app_state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else json.loads(json.dumps(DEFAULT))


def save_db(data):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            'INSERT OR REPLACE INTO app_state(id, data) VALUES(1, ?)',
            (json.dumps(data),)
        )


init_db()


def parse_time(value):
    try:
        h, m = map(int, value.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h * 60 + m
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f'Invalid time value: {value!r}. Use HH:MM format.')


def fmt_time(value):
    return f'{value // 60:02d}:{value % 60:02d}'


def time_slots(settings):
    start = parse_time(settings['start'])
    end = parse_time(settings['end'])
    duration = int(settings['duration'])
    if duration < 1 or start >= end:
        raise ValueError('Check college start time, end time, and lecture duration.')
    slots = []
    while start + duration <= end:
        slots.append({'start': fmt_time(start), 'end': fmt_time(start + duration)})
        start += duration
    return slots


def overlaps(a_start, a_end, b_start, b_end):
    return parse_time(a_start) < parse_time(b_end) and parse_time(b_start) < parse_time(a_end)


def slot_in_break(slot, breaks):
    return any(
        overlaps(slot['start'], slot['end'], b['start'], b['end'])
        for b in breaks if b.get('start') and b.get('end')
    )


def run_scheduler(data):
    settings = data['settings']
    slots = time_slots(settings)
    days = settings.get('days', [])
    breaks = settings.get('breaks', [])
    timetable = []
    conflicts = []
    requirements = []
    for cls in data['classes']:
        for subject_id in cls.get('subjectIds', []):
            subject = next((s for s in data['subjects'] if s['id'] == subject_id), None)
            if not subject:
                continue
            count = max(0, int(subject.get('weeklyCount', 1)))
            teacher_id = subject.get('teacherId')
            teacher = next((t for t in data['teachers'] if t['id'] == teacher_id), None)
            for lecture_no in range(1, count + 1):
                requirements.append({
                    'classId': cls['id'],
                    'className': cls['name'],
                    'subjectId': subject['id'],
                    'subjectName': subject['name'],
                    'teacherId': teacher_id,
                    'teacherName': teacher['name'] if teacher else 'Unassigned',
                    'type': subject.get('type', 'Theory'),
                    'lectureNo': lecture_no
                })

    requirements.sort(key=lambda r: (0 if r['type'] == 'Practical' else 1,
                                     r['teacherId'] is None))

    for req in requirements:
        teacher = next((t for t in data['teachers'] if t['id'] == req['teacherId']), None)
        if not teacher:
            conflicts.append({**req, 'reason': 'No valid teacher assigned to this subject.'})
            continue

        placed = False
        for day_idx, day in enumerate(days):
            teacher_days = teacher.get('availableDays', days)
            if teacher_days and day not in teacher_days:
                continue
            if any(
                l['dayIndex'] == day_idx
                and l['classId'] == req['classId']
                and l['subjectId'] == req['subjectId']
                for l in timetable
            ):
                continue

            for slot_idx, slot in enumerate(slots):
                if slot_in_break(slot, breaks):
                    continue

                if slot['start'] in teacher.get('unavailableSlots', {}).get(day, []):
                    continue

                if any(
                    l['dayIndex'] == day_idx and l['slotIndex'] == slot_idx
                    and l['teacherId'] == req['teacherId'] for l in timetable
                ):
                    continue

                if any(
                    l['dayIndex'] == day_idx and l['slotIndex'] == slot_idx
                    and l['classId'] == req['classId'] for l in timetable
                ):
                    continue

                eligible_rooms = [
                    r for r in data['rooms']
                    if r.get('type', 'Theory') == req['type']
                    or (req['type'] == 'Theory' and r.get('type') == 'Both')
                ]
                room = next((r for r in eligible_rooms if not any(
                    l['dayIndex'] == day_idx and l['slotIndex'] == slot_idx
                    and l['roomId'] == r['id'] for l in timetable
                )), None)
                if not room:
                    continue

                timetable.append({
                    'id': str(uuid.uuid4()),
                    'dayIndex': day_idx,
                    'day': day,
                    'slotIndex': slot_idx,
                    'start': slot['start'],
                    'end': slot['end'],
                    'subjectId': req['subjectId'],
                    'subjectName': req['subjectName'],
                    'teacherId': req['teacherId'],
                    'teacherName': req['teacherName'],
                    'classId': req['classId'],
                    'className': req['className'],
                    'roomId': room['id'],
                    'roomName': room['name'],
                    'type': req['type']
                })
                placed = True
                break

            if placed:
                break

        if not placed:
            conflicts.append({
                **req,
                'reason': ('Could not place this lecture without breaking the daily subject, '
                           'teacher, class, room, break, or availability rules.')
            })

    data['timetable'] = timetable
    data['conflicts'] = conflicts
    return timetable, conflicts


@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/data')
def get_data():
    return jsonify(load_db())


@app.put('/api/settings')
def update_settings():
    data = load_db()
    incoming = request.get_json(silent=True) or {}
    settings = data['settings']
    try:
        new_start = incoming.get('start', settings['start'])
        new_end = incoming.get('end', settings['end'])
        if parse_time(new_start) >= parse_time(new_end):
            return jsonify({'error': 'End time must be later than start time.'}), 400
        duration = int(incoming.get('duration', settings['duration']))
        if duration <= 0:
            return jsonify({'error': 'Lecture duration must be greater than zero.'}), 400
        for key in ['name', 'start', 'end', 'duration', 'days', 'breaks']:
            if key in incoming:
                settings[key] = duration if key == 'duration' else incoming[key]
        data['timetable'] = []
        data['conflicts'] = []
        save_db(data)
        return jsonify({'status': 'success', 'settings': settings})
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid time or duration format.'}), 400


@app.post('/api/sample')
def sample():
    data = load_db()
    all_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    data.update({
        'teachers': [
            {'id': 't1', 'name': 'Prof. Rahul Sharma', 'code': 'RS',
             'availableDays': all_days, 'unavailableSlots': {}},
            {'id': 't2', 'name': 'Prof. Amit Patil', 'code': 'AP',
             'availableDays': all_days, 'unavailableSlots': {}},
            {'id': 't3', 'name': 'Prof. Neha Joshi', 'code': 'NJ',
             'availableDays': all_days, 'unavailableSlots': {}}
        ],
        'subjects': [
            {'id': 's1', 'name': 'Data Structures', 'teacherId': 't1', 'weeklyCount': 4, 'type': 'Theory'},
            {'id': 's2', 'name': 'DBMS', 'teacherId': 't2', 'weeklyCount': 3, 'type': 'Theory'},
            {'id': 's3', 'name': 'Operating Systems', 'teacherId': 't3', 'weeklyCount': 4, 'type': 'Theory'},
            {'id': 's4', 'name': 'Computer Networks', 'teacherId': 't1', 'weeklyCount': 2, 'type': 'Theory'},
            {'id': 's5', 'name': 'Programming Lab', 'teacherId': 't2', 'weeklyCount': 2, 'type': 'Practical'}
        ],
        'classes': [
            {'id': 'c1', 'name': 'SY CS - A', 'subjectIds': ['s1', 's2', 's3', 's5']},
            {'id': 'c2', 'name': 'SY CS - B', 'subjectIds': ['s1', 's2', 's4']}
        ],
        'rooms': [
            {'id': 'r1', 'name': 'Room 101', 'type': 'Theory'},
            {'id': 'r2', 'name': 'Room 102', 'type': 'Theory'},
            {'id': 'r3', 'name': 'Lab 1', 'type': 'Practical'}
        ],
        'timetable': [], 'conflicts': []
    })
    save_db(data)
    return jsonify({'status': 'success', 'data': data})


@app.post('/api/data/<resource>')
def add_item(resource):
    if resource not in ['teachers', 'subjects', 'classes', 'rooms']:
        return jsonify({'error': 'Invalid resource.'}), 400
    item = request.get_json(silent=True) or {}
    if not item.get('name'):
        return jsonify({'error': 'Name is required.'}), 400

    data = load_db()
    item['id'] = str(uuid.uuid4())
    if resource == 'teachers':
        item.setdefault('code', '')
        item.setdefault('availableDays', data['settings']['days'])
        item.setdefault('unavailableSlots', {})
    elif resource == 'subjects':
        try:
            item['weeklyCount'] = max(1, int(item.get('weeklyCount', 1)))
        except (ValueError, TypeError):
            return jsonify({'error': 'Weekly count must be a valid number.'}), 400
        item.setdefault('type', 'Theory')
        if item.get('teacherId') and not any(t['id'] == item['teacherId'] for t in data['teachers']):
            return jsonify({'error': 'Select a valid teacher.'}), 400
    elif resource == 'classes':
        item.setdefault('subjectIds', [])
    elif resource == 'rooms':
        item.setdefault('type', 'Theory')

    data[resource].append(item)
    data['timetable'] = []
    data['conflicts'] = []
    save_db(data)
    return jsonify({'status': 'success', 'item': item}), 201


@app.put('/api/data/<resource>/<item_id>')
def edit_item(resource, item_id):
    if resource not in ['teachers', 'subjects', 'classes', 'rooms']:
        return jsonify({'error': 'Invalid resource.'}), 400
    incoming = request.get_json(silent=True) or {}
    data = load_db()
    item = next((x for x in data[resource] if x['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Record not found.'}), 404
    item.update(incoming)
    data['timetable'] = []
    data['conflicts'] = []
    save_db(data)
    return jsonify({'status': 'success', 'item': item})


@app.delete('/api/data/<resource>/<item_id>')
def delete_item(resource, item_id):
    if resource not in ['teachers', 'subjects', 'classes', 'rooms']:
        return jsonify({'error': 'Invalid resource.'}), 400
    data = load_db()
    data[resource] = [x for x in data[resource] if x['id'] != item_id]
    if resource == 'subjects':
        for cls in data['classes']:
            cls['subjectIds'] = [sid for sid in cls.get('subjectIds', []) if sid != item_id]
    if resource == 'teachers':
        for sub in data['subjects']:
            if sub.get('teacherId') == item_id:
                sub['teacherId'] = None
    data['timetable'] = []
    data['conflicts'] = []
    save_db(data)
    return jsonify({'status': 'deleted'})


@app.post('/api/generate')
def generate():
    data = load_db()
    try:
        timetable, conflicts = run_scheduler(data)
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({'error': str(exc)}), 400
    save_db(data)
    return jsonify({
        'timetable': timetable,
        'conflicts': conflicts,
        'scheduledCount': len(timetable),
        'conflictCount': len(conflicts)
    })


if __name__ == '__main__':
 
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
