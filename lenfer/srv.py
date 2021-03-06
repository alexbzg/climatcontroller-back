#!/usr/bin/python3
#coding=utf-8
"""onedif backend"""
import logging
import time
import json
import hashlib
from datetime import datetime

from flask import Flask, request, jsonify
from werkzeug.exceptions import InternalServerError
from hashids import Hashids

from validator import validate, bad_request
from db import DBConn, splice_params
from conf import CONF, APP_NAME, start_logging
from secret import get_secret, create_token
import send_email
from hash import data_hash

APP = Flask(APP_NAME)
APP.config.update(CONF['flask'])
APP.secret_key = get_secret(CONF['files']['secret'])
HASHIDS = Hashids(salt=APP.secret_key.decode('utf-8'), min_length=6)

with APP.app_context():
    start_logging('srv', CONF['logs']['srv_level'])
logging.debug('starting in debug mode')

DB = DBConn(CONF.items('db'))
DB.connect()
DB.verbose = True
APP.db = DB

def _create_token(data):
    return create_token(data, APP.secret_key)

@APP.errorhandler(InternalServerError)
def internal_error(exception):
    'Internal server error interceptor; logs exception'
    response = jsonify({'message': 'Server error'})
    response.status_code = 500
    logging.exception(exception)
    return response

@APP.route('/api/test', methods=['GET', 'POST'])
def test():
    """test if api is up"""
    return "Ok %s" % request.method

@APP.route('/api/register_user', methods=['POST'])
@validate(request_schema='login', recaptcha_field='recaptcha')
def register_user():
    """registers user and returns user data with token"""
    user_data = request.get_json()
    user_exists = DB.get_object('users', {'login': user_data['login']}, create=False)
    if user_exists:
        return bad_request('Пользователь с этим именем уже зарегистрирован.\n' +\
                'This username is already exists.')
    return send_user_data(user_data, create=True)

@APP.route('/api/login', methods=['POST'])
@validate(request_schema='login')
def login():
    """check login data and returns user data with token"""
    return send_user_data(request.get_json())

@APP.route('/api/password_recovery_request', methods=['POST'])
@validate(request_schema='passwordRecoveryRequest', recaptcha_field='recaptcha')
def password_recovery_request():
    """check login data and returns user data with token"""
    req_data = request.get_json()
    user_data = DB.get_object('users', req_data, create=False)
    if not user_data or not user_data['email']:
        return bad_request('Пользователь или email не зарегистрирован.\n' +\
            'The username or email address is not registered.')
    token = _create_token({
        'login': req_data['login'],
        'type': 'passwordRecovery',
        'expires': time.time() + 60 * 60 * 60})
    text = """Пройдите по ссылкe, чтобы сменить пароль на ONEADIF.com: """\
        + CONF.get('web', 'address')\
        + '/#/passwordRecovery?token=' + token + """

Если вы не запрашивали смену пароля на ONEADIF.com, просто игнорируйте это письмо.
Ссылка будет действительна в течение 1 часа.

Follow this link to change your ONEADIF.com password: """ \
        + CONF.get('web', 'address')\
        + '/#/passwordRecovery?token=' + token + """

Ignore this message if you did not request password change


Служба поддержки ONEADIF.com support"""
    send_email.send_email(text=text,\
        fr=CONF.get('email', 'address'),\
        to=user_data['email'],\
        subject="ONEADIF.com - password change")
    return jsonify({'message':\
        'На ваш почтовый адрес было отправлено письмо с инструкциями по ' +\
        'сменен пароля.\n' +\
        'The message with password change instrunctions was sent to your ' +\
        'email address'})

def ok_response():
    return jsonify({'message': 'Ok'})

@APP.route('/api/password_recovery', methods=['POST'])
@validate(request_schema='login', token_schema='passwordRecovery', recaptcha_field='recaptcha',\
        login=True)
def password_recovery():
    """sets new password after successfull recovery"""
    req_data = request.get_json()
    if not DB.param_update('users',\
        {'login': req_data['login']}, {'password': req_data['password']}):
        raise Exception('Password change failed')
    return ok_response()

@APP.route('/api/user/settings', methods=['POST'])
@validate(request_schema='post_user_settings', token_schema='auth', login=True)
def post_user_settings():
    """changes user's settings"""
    req_data = request.get_json()
    if not DB.param_update('users',\
        {'email': req_data['email']}, {'password': req_data['password']}):
        raise Exception('Settings change failed')
    return ok_response()

def update_device_last_contact(device_id):
    DB.execute("""
        update devices 
        set last_contact = now()
        where id = %(device_id)s""", {'device_id': device_id})

@APP.route('/api/device_updates', methods=['POST'])
@validate(request_schema='device_updates', token_schema='device')
def device_updates():
    """checks for update of device schedule/elegible props"""
    req_data = request.get_json()
    update_device_last_contact(req_data['device_id'])
    update_data = {}
    device_data = DB.execute("""
        select device_schedules.hash as schedule_hash, 
            device_schedules.id as schedule_id, 
            devices_types.schedule_params,
            devices.props as props_values,
            devices_types.props as props_headers
        from devices join devices_types
            on devices.device_type_id = devices_types.id
            left join device_schedules
            on devices.schedule_id = device_schedules.id
        where devices.id = %(device_id)s""", req_data, keys=False)

    if 'schedule' in req_data:
        update_data['schedule'] = {'hash': None, 'start': None}
        if device_data and device_data['schedule_id']:
            schedule_start = None
            for idx, prop_header in enumerate(device_data['props_headers']):
                if 'schedule_start' in prop_header and prop_header['schedule_start']:
                    schedule_start = (datetime.strptime(device_data['props_values'][idx],\
                        "%Y-%m-%dT%H:%M:%S.%fZ").timetuple()\
                        if device_data['props_values'] and\
                            len(device_data['props_values']) > idx\
                        else None)
                    break
            if device_data['schedule_hash'] and schedule_start and\
                (device_data['schedule_hash'] != req_data['schedule']['hash']) or\
                (not req_data['schedule']['start'] or\
                [1 for i, j in zip(schedule_start, req_data['schedule']['start'])\
                    if i != j]):

                schedule = {
                    'params': list(device_data['schedule_params']),\
                    'items': [],\
                    'hash': device_data['schedule_hash'],\
                    'start': schedule_start}
                for item in schedule_items(device_data['schedule_id']):
                    schedule_item = [None]*len(schedule['params'])
                    for param, value in item['params'].items():
                        param_entry = None
                        if device_data['schedule_params'][param]['type'] == 'float_delta':
                            param_entry = [float(value['value']), float(value['delta'])]
                        param_idx = schedule['params'].index(param)
                        schedule_item[param_idx] = param_entry
                    schedule['items'].append(schedule_item)

                update_data['schedule'] = schedule

            else:
                del update_data['schedule']

    if 'props' in req_data:
        srv_props = props_list_to_dict(device_data['props_headers'], device_data['props_values'])
        if req_data['device_id'] == 11:
            logging.debug(srv_props)
        update_data['props'] = {id: value for id, value in srv_props.items()\
            if id in req_data['props'] and data_hash(value) != data_hash(req_data['props'][id])}

    return jsonify(update_data)

def props_list_to_dict(headers, values):
    """converts device properties list from db to dictionary"""
    return {header['id']: (([\
        props_list_to_dict(header['items'], item)\
            for item in values[idx]])\
        if 'items' in header else values[idx])\
        for idx, header in enumerate(headers)}


@APP.route('/api/sensors_data', methods=['POST'])
@validate(request_schema='post_sensors_data', token_schema='device')
def post_sensors_data():
    """stores sensors data in db"""
    req_data = request.get_json()
    update_device_last_contact(req_data['device_id'])
    device_sensors = DB.execute("""
        select device_type_sensor_id as id, id as sensor_id 
            from sensors 
            where device_id = %(device_id)s
        """, req_data, keys=True)
    if device_sensors:
        device_rtc = DB.execute("""
            select rtc 
            from devices_types join devices 
                on devices.device_type_id = devices_types.id
            where devices.id = %(device_id)s
            """, req_data)
        for item in req_data['data']:
            if item['sensor_id'] in device_sensors.keys():                
                item['sensor_id'] = device_sensors[item['sensor_id']]['sensor_id']
                if not device_rtc:
                    del item['tstamp']
                DB.get_object('sensors_data', item, create=True)
    else:
        return bad_request('Device sensors not found')
    return ok_response()

@APP.route('/api/devices_log/post', methods=['POST'])
@validate(request_schema='post_devices_log', token_schema='device')
def post_devices_log():
    """stores devices log entries in db"""
    req_data = request.get_json()
    update_device_last_contact(req_data['device_id'])
    for entry in req_data['entries']:
        entry['device_id'] = req_data['device_id']
        DB.get_object('devices_log', entry, create=True)
    return ok_response()

@APP.route('/api/devices_log', methods=['POST'])
def get_devices_log():
    """returns device log for period in json"""
    req_data = request.get_json()
    data = DB.execute("""
        select to_char(log_tstamp, 'YYYY-MM-DD HH24:MI:SS') as log_tstamp,
            to_char(rcvd_tstamp, 'YYYY-MM-DD HH24:MI:SS') as rcvd_tstamp,
            txt
            from devices_log
            where device_id = %(device_id)s and
                log_tstamp between %(begin)s and %(end)s
            order by log_tstamp desc
        """, req_data, keys=False)
    if isinstance(data, dict):
        data = [data,]
    return jsonify(data)

@APP.route('/api/users_devices', methods=['POST'])
@validate(token_schema='auth', login=True)
def users_devices():
    """returns json users devices list
    [{id, title, type_id, type_title}]
    """
    req_data = request.get_json()
    devices_data = DB.execute("""
        select devices.id, device_type_id as type_id, 
            devices_types.title as type_title,
            devices.title as title
            from devices join devices_types 
                on device_type_id = devices_types.id
            where devices.login = %(login)s
            order by devices.id
        """, req_data, keys=False)
    if isinstance(devices_data, dict):
        devices_data = [devices_data,]
    elif not devices_data:
        devices_data = []
    for device in devices_data:
        device['hash'] = HASHIDS.encode(device['id'])
    return jsonify(devices_data)

@APP.route('/api/users_device_schedules', methods=['POST'])
@validate(token_schema='auth', login=True)
def users_device_schedules():
    """returns json users devices_schedules detailed list
    [{id, title, device_type_id, device_type_title,
        items: [{no, params: {}}]}]
    """
    req_data = request.get_json()
    schedules = DB.execute("""
        select device_schedules.id, device_type_id as device_type_id, 
            device_schedules.title, devices_types.title as device_type_title
            from device_schedules join devices_types 
                on device_type_id = devices_types.id
            where device_schedules.login = %(login)s
        """, req_data, keys=False)
    if not schedules:
        schedules = []
    if isinstance(schedules, dict):
        schedules = [schedules,]
    for schedule in schedules:
        schedule['items'] = schedule_items(schedule['id'])
    return jsonify(schedules)

def schedule_items(schedule_id):
    """returns dict of schedule's items by schedule's id"""
    items = DB.execute("""
        select day_no, params
            from device_schedule_items
            where schedule_id = %(id)s
            order by day_no
        """, {'id': schedule_id}, keys=False)
    if isinstance(items, dict):
        items = [items,]
    return items

@APP.route('/api/device/<device_id>', methods=['GET'])
def get_device_info(device_id):
    """returns device info json"""
    device_id = int(device_id)
    device_data = DB.execute("""
        select device_type_id as device_type_id, 
            devices_types.title as device_type,
            devices.title as title, 
            schedule_id,
            devices_types.props as props_titles,
            devices.props as props_values
            from devices join devices_types 
                on device_type_id = devices_types.id
            where devices.id = %(device_id)s
        """, {'device_id': device_id}, keys=False)
    if not device_data:
        return bad_request('Устройство не найдено. Device not found.')
    device_data['sensors'] = DB.execute("""
        select sensors.id, is_master, sensor_type as type,
            sensors.title as title, device_type_sensors.title as default_title,
            sensors.enabled, last_data.value, last_data.tstamp
        from sensors join device_type_sensors on
                device_type_sensors.id = sensors.device_type_sensor_id,
            lateral (select value, 
                to_char(tstamp, 'YYYY-MM-DD HH24:MI:SS') as tstamp
                from sensors_data 
                    where sensor_id = sensors.id
                order by tstamp desc
                limit 1) as last_data
            where device_id = %(device_id)s
        """, {'device_id': device_id}, keys=False)
    return jsonify(device_data)

@APP.route('/api/devices_types', methods=['GET'])
def get_devices_types():
    """returns devices_types info json"""
    devices_types_data = DB.execute("""
        select * from devices_types
        """, keys=False)
    return jsonify(devices_types_data)


@APP.route('/api/device_schedule/<schedule_id>', methods=['GET'])
def get_schedule_data(schedule_id):
    """returns device schedule itesm (days) in json"""
    schedule_id = int(schedule_id)
    schedule_data = DB.execute("""
        select device_schedules.id, device_type_id as device_type_id, 
            device_schedules.title, devices_types.title as device_type_title
            from device_schedules join devices_types 
                on device_type_id = devices_types.id
            where device_schedules.id = %(schedule_id)s
        """, {'schedule_id': schedule_id}, keys=False)
    if not schedule_data:
        return bad_request('Шаблон не найден.')
    schedule_data['items'] = schedule_items(schedule_id)
    return jsonify(schedule_data)


@APP.route('/api/device_schedule/<schedule_id>', methods=['DELETE'])
@validate(token_schema='auth', login=True)
def delete_schedule(schedule_id):
    """deletes device schedule from db"""
    error = None
    req_data = request.get_json()
    schedule_id = int(schedule_id)
    check_schedule = DB.execute("""
        select login 
        from device_schedules
        where id = %(id)s""", {'id': schedule_id}, keys=False)
    if check_schedule:
        if check_schedule == req_data['login']:
            DB.execute("""
                delete from device_schedule_items
                where schedule_id = %(schedule_id)s
                """, {'schedule_id': schedule_id})
            DB.param_delete('device_schedules', {'id': schedule_id})
        else:
            error = 'Шаблон зарегистрирован другим пользователем.'
    else:
        error = 'Шаблон не найден.'
    if error:
        return bad_request(error)
    else:
        return ok_response()

@APP.route('/api/device_schedule/<schedule_id>', methods=['POST'])
@validate(request_schema='post_device_schedule', token_schema='auth', login=True)
def post_schedule_data(schedule_id):
    """saves new/edited device schedule to db"""
    error = None
    req_data = request.get_json()
    req_data['hash'] = hashlib.md5(json.dumps(req_data, sort_keys=True).encode('utf-8')).hexdigest()
    if schedule_id == 'new':
        schedule = DB.get_object('device_schedules',\
            splice_params(req_data, 'login', 'title', 'device_type_id', 'hash'),\
            create=True)
        if schedule:
            schedule_id = schedule['id']
        else:
            raise Exception('Ошибка создания шаблона.')
    else:
        schedule_id = int(schedule_id)
        check_schedule = DB.execute("""
            select login 
            from device_schedules
            where id = %(id)s""", {'id': schedule_id}, keys=False)
        if check_schedule:
            if check_schedule == req_data['login']:
                DB.param_update('device_schedules',\
                    {'id': schedule_id},\
                    splice_params(req_data, 'title', 'device_type_id', 'hash'))
                DB.execute("""
                    delete from device_schedule_items
                    where schedule_id = %(schedule_id)s
                    """, {'schedule_id': schedule_id})
            else:
                error = 'Шаблон зарегистрирован другим пользователем.'
        else:
            error = 'Шаблон не найдено.'
    if error:
        return bad_request(error)
    else:
        DB.execute("""
            insert into device_schedule_items (schedule_id, day_no, params)
            values (%(schedule_id)s, %(day_no)s, %(params)s)""",\
            [{'schedule_id': schedule_id,\
                'day_no': item['day_no'],\
                'params': json.dumps(item['params'])}\
                for item in req_data['items']])
        return jsonify({'id': schedule_id})

@APP.route('/api/device/<device_id>', methods=['POST'])
@validate(request_schema='post_device_props', token_schema='auth', login=True)
def post_device_props(device_id):
    """saves updated device title/props to db"""
    device_id = int(device_id)
    req_data = request.get_json()
    error = None
    check_device = DB.execute("""
        select login 
        from devices
        where id = %(id)s""", {'id': device_id}, keys=False)
    if check_device:
        if check_device == req_data['login']:
            DB.param_update('devices',\
                {'id': device_id},\
                {'title': req_data['title'],\
                    'schedule_id': req_data['schedule_id']\
                        if 'schedule_id' in req_data else None,\
                    'props': json.dumps(req_data['props'])})
        else:
            error = 'Устройство зарегистрировано другим пользователем.'
    else:
        error = 'Устройство не найдено.'
    if error:
        return bad_request(error)
    else:
        return ok_response()

@APP.route('/api/sensor/<sensor_id>', methods=['POST'])
@validate(request_schema='post_sensor_props', token_schema='auth', login=True)
def post_sensor_info(sensor_id):
    """updates sensor title and other settings"""
    sensor_id = int(sensor_id)
    req_data = request.get_json()
    error = None
    check_sensor = DB.execute("""
        select login 
        from sensors join devices on sensors.device_id = devices.id
        where sensors.id = %(id)s""", {'id': sensor_id}, keys=False)
    if check_sensor:
        if check_sensor == req_data['login']:
            DB.param_update('sensors',\
                {'id': sensor_id},\
                {'title': req_data['title'],\
                    'enabled': req_data['enabled'],\
                    'is_master': req_data['is_master']})
        else:
            error = 'Датчик зарегистрирован другим пользователем.'
    else:
        error = 'Датчик не найден.'
    if error:
        return bad_request(error)
    else:
        return ok_response()

@APP.route('/api/sensor/<sensor_id>', methods=['GET'])
def get_sensor_info(sensor_id):
    """returns sensor info json"""
    sensor_id = int(sensor_id)
    sensor_data = DB.execute("""
        select sensors.title as sensor_title, 
                device_type_sensors.title as device_type_title,
                sensor_type
            from sensors join device_type_sensors
                on device_type_sensor_id = device_type_sensors.id
            where sensors.id = %(sensor_id)s
        """, {'sensor_id': sensor_id}, keys=False)
    if not sensor_data:
        return bad_request('Сенсор не найден. Sensor not found.')
    return jsonify(sensor_data)

@APP.route('/api/sensor/data', methods=['POST'])
def get_sensor_data():
    """returns sensors data for period in json"""
    req_data = request.get_json()
    data = DB.execute("""
        select to_char(tstamp, 'YYYY-MM-DD HH24:MI:SS') as tstamp,  value
            from sensors_data 
            where sensor_id = %(sensor_id)s and
                tstamp between %(begin)s and %(end)s
            order by tstamp
        """, req_data, keys=False)
    return jsonify(data)

@APP.route('/api/device/create', methods=['POST'])
@validate(request_schema='register_device', token_schema='auth', login=True)
def create_device():
    """registers device and it's sensors data in db;
    returns json {"device_id": _, "device_token": _}"""
    req_data = request.get_json()
    check_device_type = DB.execute("""
        select id 
            from devices_types 
            where id = %(device_type_id)s
        """, req_data)
    if not check_device_type:
        return bad_request('Неверный тип устройства. Invalid device type.')
    else:
        device_db_data = DB.get_object('devices',\
            splice_request("login", "device_type_id"), create=True)
        DB.execute("""insert into sensors (device_type_sensor_id, device_id)
            select id, %(id)s
            from device_type_sensors
            where device_type_id = %(device_type_id)s""", device_db_data)
        token = _create_token({'device_id': device_db_data['id']})
        return jsonify({'device_id': device_db_data['id'], 'device_token': token})


@APP.route('/api/device/register', methods=['POST'])
@validate(request_schema='register_device', token_schema='auth', login=True)
def register_device():
    """binds device to user's account"""
    req_data = request.get_json()
    device_id = HASHIDS.decode(req_data['device_hash'])
    error = None
    if device_id:
        check_device = DB.execute("""
            select id, login 
                from devices
                where id = %(device_id)s
            """, {'device_id': device_id}, False)
        if check_device:
            if check_device['login']:
                if check_device['login'] == req_data['login']:
                    error = 'Вы уже зарегистрировали это устройство.\n' +\
                            'You had already registered this device.'
                else:
                    error = 'Устройство уже зарегистрировано дркгим пользователем.\n' +\
                            'Another user had already registered this device.'
            else:
                DB.param_update('devices', {'id': device_id}, {'login': req_data['login']})
        else:
            error = 'Устройство не найдено. Device not found.'
    else:
        error = 'Неверный код устройства. Invalid device code.'
    if error:
        return bad_request(error)
    else:
        return ok_response()

def splice_request(*params):
    return splice_params(request.get_json(), *params)

def send_user_data(user_data, create=False):
    """returns user data with auth token as json response"""
    data = DB.get_object('users', user_data, create=create)
    if data:
        token = _create_token({'login': data['login'], 'type': 'auth'})
        del data['password']
        data['token'] = token
        return jsonify(data)
    else:
        if create:
            raise Exception("User creation failed")
        else:
            return bad_request('Неверное имя пользователя или пароль.\n' +\
                    'Wrong username or password')

if __name__ == "__main__":
    APP.run(host='127.0.0.1', port=5001)
