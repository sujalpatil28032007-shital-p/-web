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
    'timetable': [], 'conflicts': [], 'manual': []
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
    data = json.loads(row[0]) if row else json.loads(json.dumps(DEFAULT))
    data.setdefault('manual', [])
    return data


def save_db(data):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            'INSERT OR REPLACE INTO app_state(id, data) VALUES(1, ?)',
            (json.dumps(data),)
        )


# Runs under gunicorn too (the __main__ block below does not), so the
# database table always exists even if eduschedule.db is missing or empty.
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


def find(items, item_id):
    return next((x for x in items if x['id'] == item_id), None)


def type_fits(room, lecture_type):
    room_type = room.get('type', 'Theory')
    return room_type == lecture_type or (lecture_type == 'Theory' and room_type == 'Both')


def validate_ids(data, class_id, subject_id, room_id):
    cls = find(data['classes'], class_id)
    if not cls:
        raise ValueError('Select a valid class.')
    subject = find(data['subjects'], subject_id)
    if not subject or subject_id not in cls.get('subjectIds', []):
        raise ValueError('This subject is not assigned to the selected class.')
    room = find(data['rooms'], room_id)
    if not room:
        raise ValueError('Select a valid room.')
    lecture_type = subject.get('type', 'Theory')
    if not type_fits(room, lecture_type):
        raise ValueError(f"{room['name']} is not a {lecture_type} room.")
    teacher = find(data['teachers'], subject.get('teacherId'))
    if not teacher:
        raise ValueError('This subject has no teacher assigned.')
    return cls, subject, room, teacher


def build_entry(data, class_id, subject_id, room_id, day, slot_idx):
    """Build one timetable entry, or raise ValueError saying why it is not allowed."""
    cls, subject, room, teacher = validate_ids(data, class_id, subject_id, room_id)
    settings = data['settings']
    slots = time_slots(settings)
    days = settings.get('days', [])
    if day not in days:
        raise ValueError('Select a valid day.')
    try:
        slot_idx = int(slot_idx)
    except (TypeError, ValueError):
        raise ValueError('Select a valid time slot.')
    if not 0 <= slot_idx < len(slots):
        raise ValueError('Select a valid time slot.')
    slot = slots[slot_idx]
    if slot_in_break(slot, settings.get('breaks', [])):
        raise ValueError('That time slot falls in a break.')
    teacher_days = teacher.get('availableDays', days)
    if teacher_days and day not in teacher_days:
        raise ValueError(f"{teacher['name']} is not available on {day}.")
    if slot['start'] in teacher.get('unavailableSlots', {}).get(day, []):
        raise ValueError(f"{teacher['name']} is unavailable at {slot['start']} on {day}.")
    return {
        'id': str(uuid.uuid4()),
        'dayIndex': days.index(day),
        'day': day,
        'slotIndex': slot_idx,
        'start': slot['start'],
        'end': slot['end'],
        'subjectId': subject['id'],
        'subjectName': subject['name'],
        'teacherId': teacher['id'],
        'teacherName': teacher['name'],
        'classId': cls['id'],
        'className': cls['name'],
        'roomId': room['id'],
        'roomName': room['name'],
        'type': subject.get('type', 'Theory'),
        'manual': True
    }


def find_clash(entry, placed):
    """Return a message if entry clashes with an already placed lecture, else None."""
    for l in placed:
        if l['day'] != entry['day']:
            continue
        if l['start'] == entry['start']:
            if l['teacherId'] == entry['teacherId']:
                return f"{entry['teacherName']} already teaches {l['className']} at that time."
            if l['classId'] == entry['classId']:
                return f"{entry['className']} already has {l['subjectName']} at that time."
            if l['roomId'] == entry['roomId']:
                return f"{entry['roomName']} is already used by {l['className']} at that time."
        if l['classId'] == entry['classId'] and l['subjectId'] == entry['subjectId']:
            return f"{entry['className']} already has {entry['subjectName']} on {entry['day']}."
    return None


def rebuild_manual(data):
    """Re-validate saved manual assignments against the current data.
    Returns (valid_entries, [(saved_record, reason), ...])."""
    slots = time_slots(data['settings'])
    valid, failed = [], []
    for m in data.get('manual', []):
        idx = next((i for i, s in enumerate(slots) if s['start'] == m.get('start')), None)
        try:
            if idx is None:
                raise ValueError('That time slot no longer exists in the college settings.')
            entry = build_entry(data, m.get('classId'), m.get('subjectId'),
                                m.get('roomId'), m.get('day'), idx)
        except ValueError as exc:
            failed.append((m, str(exc)))
            continue
        entry['id'] = m['id']
        valid.append(entry)
    return valid, failed


def quota_left(data, current, class_id, subject_id):
    subject = find(data['subjects'], subject_id)
    limit = max(0, int(subject.get('weeklyCount', 1))) if subject else 0
    used = sum(1 for m in current
               if m['classId'] == class_id and m['subjectId'] == subject_id)
    return limit - used


def manual_conflict(m, reason):
    return {
        'classId': m.get('classId'), 'className': m.get('className', ''),
        'subjectId': m.get('subjectId'), 'subjectName': m.get('subjectName', ''),
        'teacherId': m.get('teacherId'), 'teacherName': m.get('teacherName', ''),
        'type': m.get('type', 'Theory'), 'lectureNo': 'Manual',
        'reason': f'Manual assignment skipped: {reason}'
    }


def run_scheduler(data):
    settings = data['settings']
    slots = time_slots(settings)
    days = settings.get('days', [])
    breaks = settings.get('breaks', [])
    timetable = []
    conflicts = []
    requirements = []

    placed_manual = {}
    valid_manual, failed_manual = rebuild_manual(data)
    for m, reason in failed_manual:
        conflicts.append(manual_conflict(m, reason))
    for entry in valid_manual:
        clash = find_clash(entry, timetable)
        if clash:
            conflicts.append(manual_conflict(entry, clash))
            continue
        timetable.append(entry)
        key = (entry['classId'], entry['subjectId'])
        placed_manual[key] = placed_manual.get(key, 0) + 1

    for cls in data['classes']:
        for subject_id in cls.get('subjectIds', []):
            subject = next((s for s in data['subjects'] if s['id'] == subject_id), None)
            if not subject:
                continue
            count = max(0, int(subject.get('weeklyCount', 1)))
            teacher_id = subject.get('teacherId')
            teacher = next((t for t in data['teachers'] if t['id'] == teacher_id), None)
            already = placed_manual.get((cls['id'], subject['id']), 0)
            for lecture_no in range(already + 1, count + 1):
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
        'timetable': [], 'conflicts': [], 'manual': []
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
    link = {'teachers': 'teacherId', 'subjects': 'subjectId',
            'classes': 'classId', 'rooms': 'roomId'}[resource]
    data['manual'] = [m for m in data['manual'] if m.get(link) != item_id]
    data['timetable'] = []
    data['conflicts'] = []
    save_db(data)
    return jsonify({'status': 'deleted'})


@app.get('/api/slots')
def get_slots():
    data = load_db()
    try:
        slots = time_slots(data['settings'])
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    breaks = data['settings'].get('breaks', [])
    return jsonify({
        'days': data['settings'].get('days', []),
        'slots': [{'index': i, **s, 'isBreak': slot_in_break(s, breaks)}
                  for i, s in enumerate(slots)]
    })


@app.get('/api/manual')
def list_manual():
    return jsonify(load_db()['manual'])


@app.get('/api/free-slots')
def free_slots():
    """Slots where this class/subject/room combination is allowed right now."""
    data = load_db()
    class_id = request.args.get('classId')
    subject_id = request.args.get('subjectId')
    room_id = request.args.get('roomId')
    try:
        validate_ids(data, class_id, subject_id, room_id)
        slots = time_slots(data['settings'])
        current, _ = rebuild_manual(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if quota_left(data, current, class_id, subject_id) <= 0:
        return jsonify({'free': [], 'message': 'All weekly lectures of this subject are already assigned.'})
    free = []
    for day in data['settings'].get('days', []):
        for idx, slot in enumerate(slots):
            try:
                entry = build_entry(data, class_id, subject_id, room_id, day, idx)
            except ValueError:
                continue
            if find_clash(entry, current) is None:
                free.append({'day': day, 'slotIndex': idx,
                             'start': slot['start'], 'end': slot['end']})
    return jsonify({'free': free})


@app.post('/api/manual')
def add_manual():
    data = load_db()
    body = request.get_json(silent=True) or {}
    try:
        entry = build_entry(data, body.get('classId'), body.get('subjectId'),
                            body.get('roomId'), body.get('day'), body.get('slotIndex'))
        current, _ = rebuild_manual(data)
        if quota_left(data, current, entry['classId'], entry['subjectId']) <= 0:
            raise ValueError('All weekly lectures of this subject are already assigned.')
        clash = find_clash(entry, current)
        if clash:
            raise ValueError(clash)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    data['manual'].append(entry)
    data['timetable'] = []
    data['conflicts'] = []
    save_db(data)
    return jsonify({'status': 'success', 'item': entry}), 201


@app.delete('/api/manual/<manual_id>')
def delete_manual(manual_id):
    data = load_db()
    before = len(data['manual'])
    data['manual'] = [m for m in data['manual'] if m['id'] != manual_id]
    if len(data['manual']) == before:
        return jsonify({'error': 'Record not found.'}), 404
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
